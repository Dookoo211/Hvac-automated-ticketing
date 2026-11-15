"""End-to-end safety tests for one inbox cycle (fake IMAP/SMTP, no network).

Each test names the AUDIT.md finding it guards against.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

import hvac_email_agent as agent
from app.mailbox import MailUnavailable, SendFailed
from fakes import OWN, Crash, make_email


def test_lead_gets_exactly_one_ack_and_a_ticket(harness):
    h = harness()
    uid = h.mailbox.deliver(make_email())
    h.cycle()

    acks = h.smtp.of_kind("ack")
    assert len(acks) == 1
    ack = acks[0]
    assert ack["To"] == "jane@customer-mail.com"
    assert ack["Auto-Submitted"] == "auto-replied"          # H1: identifies itself as automatic
    assert ack["X-Auto-Response-Suppress"] == "All"
    assert ack["In-Reply-To"]                                # M11: threads with the customer's email
    assert ack["Subject"] == "Re: Furnace not working"
    body = ack.get_content()
    assert "Hi Jane Smith," in body
    assert "12 Main Street" not in body                      # H9: no echo of sender-supplied text
    assert "TKT-" in body
    assert uid in h.mailbox.seen                             # marked read only after processing

    jobs = h.rows("SELECT * FROM jobs")
    assert len(jobs) == 1 and jobs[0]["ticket_id"].startswith("TKT-")

    # Second cycle (even if the email is still listed): no duplicate reply.
    h.mailbox.seen.clear()
    h.cycle()
    assert len(h.smtp.of_kind("ack")) == 1
    assert len(h.rows("SELECT * FROM jobs")) == 1


def test_dry_run_changes_nothing(harness, tmp_path):
    """C2: dry-run used to mark mail read, record dedup/cooldown and create tickets."""
    h = harness()
    h.mailbox.deliver(make_email())
    db_path = tmp_path / h.config["db_path"]
    assert not db_path.exists()

    h.cycle(dry_run=True)

    assert h.smtp.connects == 0 and h.smtp.sent == []
    assert h.mailbox.seen == set()
    assert h.mailbox.selects == [("INBOX", True)]  # read-only selection
    assert not db_path.exists()
    assert not (tmp_path / "data" / "hvac_leads.csv").exists()

    # Going live afterwards still acknowledges the lead.
    h.cycle(dry_run=False)
    assert len(h.smtp.of_kind("ack")) == 1


def test_dry_run_leaves_existing_database_untouched(harness, tmp_path):
    h = harness()
    h.mailbox.deliver(make_email())
    h.cycle()
    db_path = tmp_path / h.config["db_path"]
    before = db_path.read_bytes()
    h.mailbox.deliver(make_email(sender="bob@other-mail.com", display_name="Bob Ray"))
    h.mailbox.seen.clear()
    h.cycle(dry_run=True)
    assert db_path.read_bytes() == before
    assert len(h.smtp.sent) == 1  # only the live ack from the first cycle


def test_crash_after_commit_never_resends(harness):
    """C1: a crash between 'decided to send' and 'send finished' must not cause a duplicate."""
    h = harness()
    uid = h.mailbox.deliver(make_email())
    h.smtp.fail_send = Crash()
    with pytest.raises(Crash):
        h.cycle()
    assert uid not in h.mailbox.seen

    h.smtp.fail_send = None
    h.cycle()
    assert h.smtp.of_kind("ack") == []  # outcome unknown -> not resent
    outbound = h.rows("SELECT status FROM outbound_messages WHERE kind = 'ack'")
    assert [row["status"] for row in outbound] == ["unknown"]
    assert h.rows("SELECT status FROM processed_messages")[0]["status"] == "done"
    assert len(h.rows("SELECT * FROM jobs")) == 1  # ticket still exists for staff


def test_smtp_down_leaves_message_untouched_for_retry(harness):
    """C1: if we can't send, nothing is committed and the email stays unread."""
    h = harness()
    uid = h.mailbox.deliver(make_email())
    h.smtp.fail_connect = True
    with pytest.raises(MailUnavailable):
        h.cycle()
    assert uid not in h.mailbox.seen
    assert h.rows("SELECT * FROM processed_messages") == []
    assert h.rows("SELECT * FROM jobs") == []

    h.smtp.fail_connect = False
    h.cycle()
    assert len(h.smtp.of_kind("ack")) == 1
    assert len(h.rows("SELECT * FROM jobs")) == 1


def test_ambiguous_send_failure_is_recorded_not_retried(harness):
    h = harness()
    h.mailbox.deliver(make_email())
    h.smtp.fail_send = SendFailed("connection dropped after DATA", ambiguous=True)
    h.cycle()
    h.smtp.fail_send = None
    h.mailbox.seen.clear()
    h.cycle()
    assert h.smtp.of_kind("ack") == []
    assert h.rows("SELECT status FROM outbound_messages")[0]["status"] == "failed"


def test_one_bad_email_does_not_block_the_rest(harness, monkeypatch):
    """H6: an exception on one email is logged; the others are processed."""
    h = harness()
    real_analyze = agent.analyze_message

    def flaky(msg, config):
        if "POISON" in str(msg["Subject"]):
            raise ValueError("boom")
        return real_analyze(msg, config)

    monkeypatch.setattr(agent, "analyze_message", flaky)
    bad = h.mailbox.deliver(make_email(subject="POISON furnace", sender="bad@customer-mail.com"))
    h.mailbox.deliver(make_email(sender="good@customer-mail.com", display_name="Good Person"))
    stats = h.cycle()

    assert stats.failed == 1 and stats.processed == 1
    assert [m["To"] for m in h.smtp.of_kind("ack")] == ["good@customer-mail.com"]
    assert bad not in h.mailbox.seen  # left unread so staff notice it
    for _ in range(5):
        h.cycle()
    failed = h.rows("SELECT attempts, status FROM processed_messages WHERE sender_email = '' OR status = 'failed'")
    assert failed[0]["status"] == "failed" and failed[0]["attempts"] == agent.MAX_MESSAGE_ATTEMPTS


def test_own_mail_and_dispatch_summary_are_never_ingested(harness):
    """C4: the dispatch summary sent to our own inbox used to become a new lead every hour."""
    h = harness(dispatch_summary_enabled=True, dispatch_summary_interval_minutes=60)
    h.mailbox.deliver(make_email())
    h.cycle()
    summaries = h.smtp.of_kind("dispatch")
    assert len(summaries) == 1
    assert summaries[0]["Auto-Submitted"] == "auto-generated"

    # Gmail drops the summary back into our inbox.
    h.mailbox.deliver(summaries[0].as_bytes())
    # And an alias of ours writes in, too.
    h.mailbox.deliver(make_email(sender="office+alerts@hvac-test.com", display_name=""))
    h.clock.advance(hours=2)
    stats = h.cycle()
    assert stats.ignored_own == 2
    assert len(h.rows("SELECT * FROM jobs")) == 1
    assert len(h.smtp.of_kind("dispatch")) == 1  # nothing new to summarize


@pytest.mark.parametrize(
    "kwargs",
    [
        {"headers": {"List-Id": "<hvac.lists.example.com>"}},
        {"headers": {"List-Unsubscribe": "<mailto:u@example.com>"}},
        {"headers": {"Precedence": "bulk"}},
        {"headers": {"Precedence": "auto_reply"}},
        {"headers": {"Auto-Submitted": "auto-replied"}},
        {"headers": {"X-Autoreply": "yes"}},
        {"headers": {"Return-Path": "<>"}},
        {"headers": {"X-HVAC-Auto": "true"}},
        {"sender": "noreply@customer-mail.com"},
        {"sender": "mailer-daemon@customer-mail.com"},
        {"authenticated": False},
    ],
)
def test_reply_suppression(harness, kwargs):
    """H1/H9: never auto-reply to lists, bounces, autoresponders, no-reply or unauthenticated senders."""
    h = harness()
    h.mailbox.deliver(make_email(**kwargs))
    h.cycle()
    assert h.smtp.of_kind("ack") == []


def test_forged_authentication_header_is_not_trusted(harness):
    """H9: only the top-most Authentication-Results (added by Gmail) counts."""
    h = harness()
    raw = make_email(authenticated=False, headers={
        "Authentication-Results": "attacker.example; dkim=pass header.d=customer-mail.com",
    })
    h.mailbox.deliver(raw)
    h.cycle()
    assert h.smtp.of_kind("ack") == []


def test_dmarc_fail_blocks_even_with_dkim_pass(harness):
    h = harness()
    raw = make_email(authenticated=False, headers={
        "Authentication-Results": "mx.google.com; dkim=pass header.i=@customer-mail.com; dmarc=fail header.from=customer-mail.com",
    })
    h.mailbox.deliver(raw)
    h.cycle()
    assert h.smtp.of_kind("ack") == []
    assert len(h.rows("SELECT * FROM jobs")) == 1  # still ticketed for staff


def test_cooldown_survives_restart(harness):
    """Cooldown is in SQLite now, so a new process still honours it."""
    h = harness()
    h.mailbox.deliver(make_email(subject="Furnace not working"))
    h.cycle()
    h.clock.advance(hours=2)
    h.mailbox.deliver(make_email(subject="Still no heat, furnace broken", date=h.clock() - timedelta(minutes=5)),
                      received=h.clock() - timedelta(minutes=5))
    h.cycle()
    assert len(h.smtp.of_kind("ack")) == 1
    h.clock.advance(hours=23)
    h.mailbox.deliver(make_email(subject="furnace not working again", date=h.clock()), received=h.clock())
    h.cycle()
    assert len(h.smtp.of_kind("ack")) == 2


def test_daily_cap(harness):
    """M10: a flood of inbound mail can't make us send unlimited email."""
    h = harness(max_auto_emails_per_day=2)
    for index in range(4):
        h.mailbox.deliver(make_email(sender=f"person{index}@customer-mail.com", display_name=f"Person {chr(65 + index)}"))
    h.cycle()
    assert len(h.smtp.of_kind("ack")) == 2
    assert len(h.rows("SELECT * FROM jobs")) == 4


def test_old_backlog_is_ticketed_but_not_acknowledged(harness):
    """First run on a mailbox with old unread mail must not mail everyone."""
    h = harness()
    old = h.clock() - timedelta(days=5)
    h.mailbox.deliver(make_email(date=old), received=old)
    h.cycle()
    assert h.smtp.of_kind("ack") == []
    assert len(h.rows("SELECT * FROM jobs")) == 1


def test_uid_fallback_when_message_id_missing(harness):
    """M5: messages without Message-ID are keyed by UIDVALIDITY+UID, not the reusable sequence number."""
    h = harness()
    first = make_email(sender="a@customer-mail.com", display_name="Ann Lee").replace(b"Message-ID", b"X-Old-Id")
    second = make_email(sender="b@customer-mail.com", display_name="Ben Lee").replace(b"Message-ID", b"X-Old-Id")
    h.mailbox.deliver(first)
    h.cycle()
    h.mailbox.deliver(second)
    h.cycle()
    assert len(h.smtp.of_kind("ack")) == 2


def test_auto_reply_disabled_still_tickets(harness):
    h = harness(auto_reply_enabled=False)
    h.mailbox.deliver(make_email())
    h.cycle()
    assert h.smtp.sent == []
    assert len(h.rows("SELECT * FROM jobs")) == 1


def test_review_class_creates_ticket_without_reply(harness):
    """H4: HVAC-related but unclear mail becomes a 'review' ticket instead of vanishing."""
    h = harness()
    h.mailbox.deliver(make_email(subject="Hello", body="I need someone to look at my HVAC system.\n\nThanks,\nSarah Lee"))
    h.cycle()
    assert h.smtp.of_kind("ack") == []
    jobs = h.rows("SELECT needs_review FROM jobs")
    assert [row["needs_review"] for row in jobs] == ["1"]


def test_csv_exports_written_from_database(harness, tmp_path):
    h = harness()
    h.mailbox.deliver(make_email())
    h.cycle()
    tickets = (tmp_path / "data" / "job_tickets.csv").read_text(encoding="utf-8").splitlines()
    assert tickets[0].startswith("ticket_id,created_at,last_updated_at,occurrence_count,status")
    assert len(tickets) == 2 and "TKT-" in tickets[1]
    leads = (tmp_path / "data" / "hvac_leads.csv").read_text(encoding="utf-8").splitlines()
    assert leads[0] == "timestamp,sender_email,detected_name,service_detected,subject,classification"


def test_imap_down_raises_without_side_effects(harness):
    h = harness()
    h.mailbox.fail_connect = True
    with pytest.raises(MailUnavailable):
        h.cycle()
    assert h.smtp.sent == []
