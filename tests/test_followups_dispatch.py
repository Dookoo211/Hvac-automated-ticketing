"""Follow-up and dispatch-summary behaviour (AUDIT H2, C1, C4)."""

from __future__ import annotations

from datetime import timedelta

from app.mailbox import SendFailed
from fakes import make_email


def _lead(h, **kwargs):
    h.mailbox.deliver(make_email(**kwargs))
    h.cycle()


def test_follow_up_sent_once_when_no_manual_reply(harness):
    h = harness(follow_up_enabled=True, follow_up_delay_hours=24)
    _lead(h)
    h.clock.advance(hours=25)
    h.cycle()
    h.cycle()
    follow_ups = h.smtp.of_kind("follow_up")
    assert len(follow_ups) == 1
    assert follow_ups[0]["Auto-Submitted"] == "auto-replied"
    assert h.rows("SELECT followup_sent_at FROM jobs")[0]["followup_sent_at"]


def test_manual_reply_cancels_follow_up(harness):
    h = harness(follow_up_enabled=True)
    _lead(h)
    h.mailbox.sent_folder.append(("jane@customer-mail.com", h.clock() + timedelta(hours=1)))
    h.clock.advance(hours=25)
    h.cycle()
    assert h.smtp.of_kind("follow_up") == []
    assert h.rows("SELECT status, status_reason FROM follow_ups")[0]["status_reason"] == "manual reply detected"


def test_job_progress_cancels_follow_up(harness):
    h = harness(follow_up_enabled=True)
    _lead(h)
    conn = h.db()
    conn.execute("UPDATE jobs SET status = 'scheduled'")
    conn.commit()
    conn.close()
    h.clock.advance(hours=25)
    h.cycle()
    assert h.smtp.of_kind("follow_up") == []


def test_unreadable_sent_folder_holds_follow_ups(harness):
    h = harness(follow_up_enabled=True)
    _lead(h)
    h.mailbox.sent_unavailable = True
    h.clock.advance(hours=25)
    h.cycle()
    assert h.smtp.of_kind("follow_up") == []
    assert h.rows("SELECT status FROM follow_ups")[0]["status"] == "pending"


def test_disabling_cancels_queue_so_reenabling_cannot_burst(harness, make_config):
    """H2: switching follow-ups on must never email old leads in one go."""
    h = harness(follow_up_enabled=True)
    for index in range(3):
        _lead(h, sender=f"p{index}@customer-mail.com", display_name=f"Pat {chr(65 + index)}")
    h.config["follow_up_enabled"] = False
    h.cycle()
    h.config["follow_up_enabled"] = True
    h.clock.advance(days=3)
    h.cycle()
    assert h.smtp.of_kind("follow_up") == []


def test_leads_while_disabled_never_get_follow_ups(harness):
    h = harness(follow_up_enabled=False)
    _lead(h)
    h.config["follow_up_enabled"] = True
    h.clock.advance(hours=25)
    h.cycle()
    assert h.smtp.of_kind("follow_up") == []


def test_overdue_follow_up_after_downtime_is_cancelled(harness):
    h = harness(follow_up_enabled=True)
    _lead(h)
    h.clock.advance(days=4)  # agent was off for days
    h.cycle()
    assert h.smtp.of_kind("follow_up") == []
    assert "overdue" in h.rows("SELECT status_reason FROM follow_ups")[0]["status_reason"]


def test_follow_up_not_registered_for_unsafe_sender(harness):
    h = harness(follow_up_enabled=True)
    _lead(h, headers={"List-Id": "<list.example.com>"})
    h.clock.advance(hours=25)
    h.cycle()
    assert h.smtp.sent == []


def test_dispatch_summary_batches_and_respects_interval(harness):
    h = harness(dispatch_summary_enabled=True, dispatch_summary_interval_minutes=60, auto_reply_enabled=False)
    _lead(h)
    assert len(h.smtp.of_kind("dispatch")) == 1
    _lead(h, sender="second@customer-mail.com", display_name="Sam Second", subject="AC not cooling")
    assert len(h.smtp.of_kind("dispatch")) == 1  # interval not elapsed
    h.clock.advance(minutes=61)
    h.cycle()
    summaries = h.smtp.of_kind("dispatch")
    assert len(summaries) == 2
    assert "1." in summaries[1].get_content()


def test_dispatch_send_failure_requeues(harness):
    h = harness(dispatch_summary_enabled=True, auto_reply_enabled=False)
    h.smtp.fail_send = SendFailed("refused", ambiguous=False)
    _lead(h)
    assert h.rows("SELECT status FROM dispatch_queue")[0]["status"] == "pending"
    h.smtp.fail_send = None
    h.cycle()
    assert len(h.smtp.of_kind("dispatch")) == 1
    assert h.rows("SELECT status FROM dispatch_queue")[0]["status"] == "sent"
