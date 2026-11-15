"""Unit tests: classifier, extraction, config, storage helpers, lock, legacy import."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import hvac_email_agent as agent
from app import legacy_import, store
from app.instance_lock import InstanceLock
from app.fileutil import atomic_write_text, backup_paths
from app.settings import ConfigError
from conftest import write_config
from fakes import make_email


# --- classifier (cases verified failing in AUDIT H3/H4) ------------------------------

@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("Furnace issue", "My furnace stopped working this morning and we have no heat.", "lead"),
        ("AC question", "Water is leaking from my air conditioner inside the house.", "lead"),
        ("Help", "My AC is blowing warm air. Can someone come out?", "lead"),
        ("Boiler", "Our boiler won't fire up, radiators are cold.", "lead"),
        ("Quote", "Can you confirm availability for a quote on a new heat pump? Any update would be great.", "lead"),
        ("Specialist", "Looking for an HVAC specialist, is there a deal on duct cleaning?", "lead"),
        ("Mailer", "Deadline for the theater event is next week, contact me back.", "existing"),
        ("Plumbing", "My kitchen sink is clogged and the toilet runs.", "existing"),
        ("Invoice follow up", "Following up on the invoice sent last week.", "existing"),
        ("Grow your traffic", "Boost your SEO traffic with backlinks and marketing. Unsubscribe | Privacy policy", "spam"),
        ("Hello", "I need someone to look at my HVAC system.", "review"),
    ],
)
def test_classification(make_config, subject, body, expected):
    config = make_config()
    msg = agent.message_from_bytes(make_email(subject=subject, body=body), policy=agent.default)
    assert agent.analyze_message(msg, config)["classification"] == expected


def test_keyword_hits_whole_words_only():
    assert agent.keyword_hits("please contact me", "ac") == 0
    assert agent.keyword_hits("the theater event", "heat") == 0
    assert agent.keyword_hits("my a/c is broken", "a/c") == 1


def test_ac_repair_is_not_relabelled_installation(make_config):
    config = make_config()
    assert agent.detect_service("AC broken", "My AC is broken and not cooling", config) == "AC Repair"


@pytest.mark.parametrize(
    "body,display,expected",
    [
        ("Hi, I am having trouble with my furnace.", "", "Jane"),
        ("This is urgent, the furnace stopped.", "", "Jane"),
        ("Name: john doe\nPhone: 555", "", "John Doe"),
        ("My name is Sarah Lee and my AC broke.", "", "Sarah Lee"),
        ("Furnace broke.\n\nThanks,\nMaria Gomez", "", "Maria Gomez"),
        ("Furnace broke.", "Pat Jones", "Pat Jones"),
    ],
)
def test_customer_name(body, display, expected):
    assert agent.extract_customer_name(body, display, "jane@customer-mail.com") == expected


def test_greeting_never_uses_email_local_part():
    request = {"customer_name": "Jsmith1987", "sender_email": "jsmith1987@example.com"}
    assert agent.greeting_name(request, "") == "there"


def test_address_and_phone_false_positives():
    assert agent.extract_address("20 minutes ago it stopped working completely") == "Unknown"
    assert agent.extract_address("We are at 45 Oak Avenue, near the park") == "45 Oak Avenue"
    assert agent.extract_phone("Order 123456789012 shipped") == ""
    assert agent.extract_phone("Call me at (905) 555-1234") == "(905) 555-1234"


def test_priority_uses_whole_words(make_config):
    config = make_config()
    assert agent.infer_priority("Deadline", "The deadline is Friday", config) == "Normal"
    assert agent.infer_priority("Help", "Furnace is dead, emergency", config) == "High"


# --- config --------------------------------------------------------------------------

def test_flag_disagreement_fails_safe(tmp_path, capsys):
    path = write_config(tmp_path, auto_reply_enabled=True, feature_auto_reply=False)
    config = agent.load_config(path)
    assert config["auto_reply_enabled"] is False and config["feature_auto_reply"] is False
    assert "treating the feature as OFF" in capsys.readouterr().out


def test_legacy_flag_alone_still_works(tmp_path):
    raw_path = write_config(tmp_path)
    raw = json.loads(raw_path.read_text())
    raw.pop("auto_reply_enabled")
    raw["feature_auto_reply"] = True
    raw_path.write_text(json.dumps(raw))
    assert agent.load_config(raw_path)["auto_reply_enabled"] is True


def test_config_validation_lists_every_problem(tmp_path):
    path = write_config(tmp_path, imap_port="993", office_timezone="Mars/Base", ticket_duplicate_mode="nuke",
                        email_address="not-an-email", poll_interval_seconds=1)
    with pytest.raises(ConfigError) as info:
        agent.load_config(path)
    text = str(info.value)
    for fragment in ("imap_port", "office_timezone", "ticket_duplicate_mode", "email_address", "poll_interval_seconds"):
        assert fragment in text


def test_delete_mode_is_alias_for_ignore(tmp_path):
    assert agent.load_config(write_config(tmp_path, ticket_duplicate_mode="delete"))["ticket_duplicate_mode"] == "ignore"


def test_config_written_without_password(tmp_path, make_config):
    config = make_config()
    agent.write_config_files(config, tmp_path / "config.json")
    saved = json.loads((tmp_path / "config.json").read_text())
    assert "email_password" not in saved and "config_path" not in saved


def test_setup_moves_plaintext_password_to_credential_store(monkeypatch):
    stored = {}
    monkeypatch.setattr(agent.credentials, "set_password", lambda email, pw: stored.update({email: pw}))
    config = {"email_address": "a@b.com", "email_password": "legacy-secret"}
    agent.store_mail_password(config, "")  # user pressed Enter: keep the old one
    assert stored == {"a@b.com": "legacy-secret"} and "email_password" not in config


# --- storage helpers ------------------------------------------------------------------

def _memory_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = agent.database.dict_factory
    store.init_state_schema(conn)
    return conn


def test_customer_merge_never_blanks_known_values():
    """H8: a later email without phone/address must not wipe them."""
    conn = _memory_db()
    cid = store.upsert_customer(conn, "Jane", "Smith", "jane@x.com", "555-1234", "12 Main Street")
    store.upsert_customer(conn, "Jane", "", "JANE@x.com", "", "Unknown")
    row = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (cid,)).fetchone()
    assert (row["phone"], row["address"], row["last_name"]) == ("555-1234", "12 Main Street", "Smith")


@pytest.mark.parametrize("mode,expected_issue", [("update", "new words"), ("replace", "new words"), ("ignore", "old words")])
def test_duplicate_modes_keep_ticket_status_and_technician(mode, expected_issue):
    """M2: 'replace' used to drop the ticket id, status and assigned technician."""
    conn = _memory_db()
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    cid = store.upsert_customer(conn, "Jane", "Smith", "jane@x.com", "", "")
    job_id, ticket = store.create_job(conn, cid, {"issue": "old words", "priority": "Normal"}, "k1", now, False)
    conn.execute("UPDATE jobs SET status = 'scheduled', technician_assigned = 'Ray' WHERE job_id = ?", (job_id,))
    job = store.get_job(conn, job_id)
    store.apply_duplicate(conn, job, {"issue": "new words", "priority": "", "address": ""}, mode, now)
    after = store.get_job(conn, job_id)
    assert (after["ticket_id"], after["status"], after["technician_assigned"]) == (ticket, "scheduled", "Ray")
    assert after["issue_description"] == expected_issue
    assert int(after["occurrence_count"]) == 2
    if mode == "update":
        assert after["priority"] == "Normal"  # blank incoming value ignored


def test_ticket_ids_unique_within_same_second():
    conn = _memory_db()
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    cid = store.upsert_customer(conn, "A", "", "a@x.com", "", "")
    ids = {store.create_job(conn, cid, {"issue": f"i{n}"}, f"k{n}", now, False)[1] for n in range(5)}
    assert len(ids) == 5


def test_atomic_write_replaces_whole_file(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")
    assert target.read_text() == "second"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp files left


def test_backup_keeps_relative_layout(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "data" / "x.log").write_text("d")
    (tmp_path / "logs" / "x.log").write_text("l")
    dest = backup_paths([tmp_path / "data" / "x.log", tmp_path / "logs" / "x.log"], tmp_path / "backups", "t")
    assert (dest / "data" / "x.log").read_text() == "d" and (dest / "logs" / "x.log").read_text() == "l"


def test_instance_lock_is_exclusive(tmp_path):
    first, second = InstanceLock(tmp_path / "agent.lock"), InstanceLock(tmp_path / "agent.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_live_cycle_refuses_to_start_twice(harness, tmp_path):
    h = harness()
    lock = InstanceLock(agent.lock_path_for(h.config))
    assert lock.acquire()
    try:
        with pytest.raises(agent.AlreadyRunningError):
            agent.run_agent(h.config)
    finally:
        lock.release()


# --- legacy import ---------------------------------------------------------------------

def test_legacy_state_is_imported_once_with_backup(harness, tmp_path):
    h = harness()
    raw = make_email(message_id="<already-handled@customer-mail.com>")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "processed_ids.txt").write_text("<already-handled@customer-mail.com>\n")
    (tmp_path / "data" / "sender_cooldown.json").write_text(json.dumps({"bob@x.com": "2026-11-16T14:00:00+00:00"}))
    (tmp_path / "data" / "hvac_leads.csv").write_text(
        "timestamp,sender_email,detected_name,service_detected,subject,classification\n"
        "2026-01-01T00:00:00,old@x.com,Old,Furnace Repair,old,lead\n"
    )
    h.mailbox.deliver(raw)
    h.cycle()
    assert h.smtp.sent == []  # already handled before the upgrade
    backups = list((tmp_path / "backups").glob("legacy-import-*"))
    assert len(backups) == 1 and (backups[0] / "data" / "processed_ids.txt").exists()
    assert len(h.rows("SELECT * FROM lead_log")) == 1
    assert h.rows("SELECT * FROM sender_cooldown")[0]["sender_email"] == "bob@x.com"
    h.cycle()
    assert len(list((tmp_path / "backups").glob("legacy-import-*"))) == 1  # only once


def test_corrupt_legacy_file_stops_startup_loudly(harness, tmp_path):
    h = harness()
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sender_cooldown.json").write_text("{not json")
    with pytest.raises(legacy_import.LegacyImportError):
        h.cycle()
