"""SQLite state store: the single source of truth for the agent.

WHY (AUDIT C1, H5, M1): processed Message-IDs, reply cooldowns, follow-ups and
the dispatch queue used to live in separate txt/JSON files that were saved
only at the end of a cycle. A crash mid-cycle lost them, causing duplicate
replies and dropped leads. Leads, service requests, drafts and tickets were
also written to CSVs *and* SQLite, and the two drifted apart.

Now everything lives in one SQLite database. Each email is handled inside a
single transaction, so the database either has all of an email's effects or
none of them. CSV files are regenerated from the database as read-only
exports (see ``export_csvs``).

All functions take an open connection so the caller decides the transaction
boundaries. Dry-run passes an in-memory copy of the database, so nothing it
does can touch real state (AUDIT C2).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import database
from .fileutil import atomic_write_text

OPEN_JOB_STATUSES = ("new", "scheduled", "in progress")
UNKNOWN_VALUES = {"", "unknown", "there"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Connections and schema
# ---------------------------------------------------------------------------

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- One row per inbound email. status: processing | done | failed
-- 'processing' is only ever committed when an outgoing email is about to be
-- sent; finding it on startup means we crashed mid-send (see recover_*).
CREATE TABLE IF NOT EXISTS processed_messages (
    message_key TEXT PRIMARY KEY,
    message_id TEXT,
    mailbox TEXT,
    uidvalidity TEXT,
    uid TEXT,
    sender_email TEXT,
    subject TEXT,
    classification TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    error TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_uid ON processed_messages(mailbox, uidvalidity, uid);

CREATE TABLE IF NOT EXISTS sender_cooldown (
    sender_email TEXT PRIMARY KEY,
    last_auto_email_at TEXT NOT NULL
);

-- Every email/SMS we send or intend to send. status: pending | sent | failed | unknown | dry_run
-- 'pending' is written in the same transaction as the decision to send, so a
-- crash can never produce a send without a record (at-most-once delivery).
CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    recipient TEXT NOT NULL,
    message_key TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbound_created ON outbound_messages(created_at);

-- status: pending | sending | sent | cancelled
CREATE TABLE IF NOT EXISTS follow_ups (
    message_key TEXT PRIMARY KEY,
    sender_email TEXT NOT NULL,
    recipient_name TEXT,
    subject TEXT,
    job_id INTEGER,
    lead_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    outbound_id INTEGER,
    updated_at TEXT NOT NULL
);

-- status: pending | sending | sent
CREATE TABLE IF NOT EXISTS dispatch_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at TEXT,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS lead_log (
    message_key TEXT PRIMARY KEY,
    timestamp TEXT,
    sender_email TEXT,
    detected_name TEXT,
    service_detected TEXT,
    subject TEXT,
    classification TEXT
);

CREATE TABLE IF NOT EXISTS service_requests (
    message_key TEXT PRIMARY KEY,
    timestamp TEXT,
    sender_email TEXT,
    customer_name TEXT,
    first_name TEXT,
    last_name TEXT,
    phone TEXT,
    address TEXT,
    maps_link TEXT,
    issue TEXT,
    priority TEXT,
    request_type TEXT,
    service_detected TEXT,
    subject TEXT
);

CREATE TABLE IF NOT EXISTS draft_replies (
    message_key TEXT PRIMARY KEY,
    timestamp TEXT,
    sender_email TEXT,
    draft_subject TEXT,
    draft_body TEXT,
    customer_name TEXT,
    phone TEXT,
    address TEXT,
    maps_link TEXT,
    service_detected TEXT,
    priority TEXT
);
"""


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = database.dict_factory
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_state_schema(conn: sqlite3.Connection) -> None:
    database.init_schema(conn)
    conn.executescript(STATE_SCHEMA)
    conn.commit()


def open_state_db(db_path: Path) -> sqlite3.Connection:
    """Open the real database for the agent.

    isolation_level=None = we issue BEGIN/COMMIT ourselves (see transaction()).
    WAL lets the UI read while the agent writes; timeout waits for a lock
    instead of failing immediately.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _configure(sqlite3.connect(db_path, timeout=30, isolation_level=None))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    init_state_schema(conn)
    return conn


def open_dry_run_copy(db_path: Path) -> sqlite3.Connection:
    """In-memory copy of the real database for --dry-run.

    WHY (AUDIT C2): dry-run used to write dedup, cooldowns, tickets and CSVs
    for real, which consumed leads before going live. Working on a throwaway
    copy means dry-run sees real history (so it reports what live would
    really do) but cannot change it.
    """
    memory = _configure(sqlite3.connect(":memory:", isolation_level=None))
    db_path = Path(db_path)
    if db_path.exists():
        source = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            source.backup(memory)
        finally:
            source.close()
    init_state_schema(memory)
    return memory


def open_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Read-only connection for dashboards; None if the database does not exist yet."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    return _configure(sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception.

    IMMEDIATE takes the write lock up front, so two writers wait for each
    other instead of failing half-way through.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# processed messages (dedup)
# ---------------------------------------------------------------------------


def get_processed(conn: sqlite3.Connection, message_key: str) -> dict | None:
    return conn.execute("SELECT * FROM processed_messages WHERE message_key = ?", (message_key,)).fetchone()


def find_processed_by_uid(conn: sqlite3.Connection, mailbox: str, uidvalidity: str, uid: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM processed_messages WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
        (mailbox, uidvalidity, uid),
    ).fetchone()


def upsert_processed(
    conn: sqlite3.Connection,
    message_key: str,
    *,
    status: str,
    now: datetime,
    message_id: str = "",
    mailbox: str = "",
    uidvalidity: str = "",
    uid: str = "",
    sender_email: str = "",
    subject: str = "",
    classification: str = "",
    note: str = "",
    error: str = "",
    attempts_increment: int = 0,
) -> None:
    stamp = iso(now)
    conn.execute(
        """
        INSERT INTO processed_messages (
            message_key, message_id, mailbox, uidvalidity, uid, sender_email, subject,
            classification, status, attempts, note, error, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_key) DO UPDATE SET
            message_id = COALESCE(NULLIF(excluded.message_id, ''), processed_messages.message_id),
            mailbox = COALESCE(NULLIF(excluded.mailbox, ''), processed_messages.mailbox),
            uidvalidity = COALESCE(NULLIF(excluded.uidvalidity, ''), processed_messages.uidvalidity),
            uid = COALESCE(NULLIF(excluded.uid, ''), processed_messages.uid),
            sender_email = COALESCE(NULLIF(excluded.sender_email, ''), processed_messages.sender_email),
            subject = COALESCE(NULLIF(excluded.subject, ''), processed_messages.subject),
            classification = COALESCE(NULLIF(excluded.classification, ''), processed_messages.classification),
            status = excluded.status,
            attempts = processed_messages.attempts + ?,
            note = COALESCE(NULLIF(excluded.note, ''), processed_messages.note),
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (
            message_key, message_id, mailbox, uidvalidity, uid, sender_email, subject,
            classification, status, attempts_increment, note, error, stamp, stamp,
            attempts_increment,
        ),
    )


# ---------------------------------------------------------------------------
# cooldown + outbound
# ---------------------------------------------------------------------------


def in_cooldown(conn: sqlite3.Connection, sender_email: str, now: datetime, hours: float) -> bool:
    row = conn.execute(
        "SELECT last_auto_email_at FROM sender_cooldown WHERE sender_email = ?", (sender_email.lower(),)
    ).fetchone()
    last = parse_iso(row["last_auto_email_at"]) if row else None
    return bool(last and now < last + timedelta(hours=hours))


def set_cooldown(conn: sqlite3.Connection, sender_email: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO sender_cooldown (sender_email, last_auto_email_at) VALUES (?, ?)
        ON CONFLICT(sender_email) DO UPDATE SET last_auto_email_at = excluded.last_auto_email_at
        """,
        (sender_email.lower(), iso(now)),
    )


def add_outbound(conn: sqlite3.Connection, kind: str, recipient: str, message_key: str, now: datetime) -> int:
    cursor = conn.execute(
        """
        INSERT INTO outbound_messages (kind, recipient, message_key, status, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (kind, recipient, message_key, iso(now), iso(now)),
    )
    return int(cursor.lastrowid)


def set_outbound_status(conn: sqlite3.Connection, outbound_id: int, status: str, now: datetime, error: str = "") -> None:
    conn.execute(
        "UPDATE outbound_messages SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, iso(now), outbound_id),
    )


def count_outbound_since(conn: sqlite3.Connection, kinds: tuple[str, ...], since: datetime) -> int:
    """Count sends that may have reached a recipient (pending/sent/unknown) for the daily caps."""
    placeholders = ",".join("?" for _ in kinds)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM outbound_messages
        WHERE kind IN ({placeholders}) AND status IN ('pending', 'sent', 'unknown') AND created_at >= ?
        """,
        (*kinds, iso(since)),
    ).fetchone()
    return int(row["n"])


def recover_interrupted_message(conn: sqlite3.Connection, message_key: str, now: datetime) -> None:
    """A previous run committed an email as 'processing' and then died mid-send.

    We cannot know whether the reply left the building, so we do NOT resend
    (a duplicate is worse than a missed acknowledgment; the ticket and draft
    already exist for staff). Mark the send 'unknown' and the email done.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE outbound_messages SET status = 'unknown', updated_at = ? WHERE message_key = ? AND status = 'pending'",
            (iso(now), message_key),
        )
        upsert_processed(
            conn, message_key, status="done", now=now,
            note="recovered after interruption; reply outcome unknown, not resent",
        )


# ---------------------------------------------------------------------------
# follow-ups
# ---------------------------------------------------------------------------


def add_follow_up(
    conn: sqlite3.Connection,
    message_key: str,
    sender_email: str,
    recipient_name: str,
    subject: str,
    job_id: int | None,
    lead_at: datetime,
    delay_hours: float,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO follow_ups (
            message_key, sender_email, recipient_name, subject, job_id, lead_at, due_at, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            message_key, sender_email.lower(), recipient_name, subject, job_id,
            iso(lead_at), iso(lead_at + timedelta(hours=delay_hours)), iso(lead_at),
        ),
    )


def set_follow_up_status(
    conn: sqlite3.Connection, message_key: str, status: str, now: datetime, reason: str = "", outbound_id: int | None = None
) -> None:
    conn.execute(
        """
        UPDATE follow_ups SET status = ?, status_reason = ?, updated_at = ?,
            outbound_id = COALESCE(?, outbound_id)
        WHERE message_key = ?
        """,
        (status, reason, iso(now), outbound_id, message_key),
    )


def cancel_all_pending_follow_ups(conn: sqlite3.Connection, now: datetime, reason: str) -> int:
    cursor = conn.execute(
        "UPDATE follow_ups SET status = 'cancelled', status_reason = ?, updated_at = ? WHERE status = 'pending'",
        (reason, iso(now)),
    )
    return cursor.rowcount


def recover_interrupted_follow_ups(conn: sqlite3.Connection, now: datetime) -> int:
    """'sending' rows mean we crashed mid-send: never resend (at-most-once)."""
    rows = conn.execute("SELECT message_key, outbound_id FROM follow_ups WHERE status = 'sending'").fetchall()
    for row in rows:
        if row["outbound_id"]:
            set_outbound_status(conn, int(row["outbound_id"]), "unknown", now)
        set_follow_up_status(conn, row["message_key"], "cancelled", now, "interrupted mid-send; outcome unknown")
    return len(rows)


def due_follow_ups(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    return conn.execute(
        "SELECT * FROM follow_ups WHERE status = 'pending' AND due_at <= ? ORDER BY due_at",
        (iso(now),),
    ).fetchall()


# ---------------------------------------------------------------------------
# dispatch queue
# ---------------------------------------------------------------------------


def enqueue_dispatch(conn: sqlite3.Connection, payload: dict, now: datetime) -> None:
    conn.execute(
        "INSERT INTO dispatch_queue (created_at, payload, status) VALUES (?, ?, 'pending')",
        (iso(now), json.dumps(payload)),
    )


def release_stale_dispatch_claims(conn: sqlite3.Connection, now: datetime, stale_minutes: int = 10) -> int:
    """Return items stuck in 'sending' (crash mid-send) to the queue.

    Unlike customer emails, a repeated *internal* summary is harmless and a
    lost one is not, so these are retried.
    """
    cutoff = iso(now - timedelta(minutes=stale_minutes))
    cursor = conn.execute(
        "UPDATE dispatch_queue SET status = 'pending', claimed_at = NULL WHERE status = 'sending' AND claimed_at < ?",
        (cutoff,),
    )
    return cursor.rowcount


def claim_pending_dispatch(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    rows = conn.execute("SELECT * FROM dispatch_queue WHERE status = 'pending' ORDER BY id").fetchall()
    if rows:
        conn.execute(
            f"UPDATE dispatch_queue SET status = 'sending', claimed_at = ? WHERE id IN ({','.join('?' for _ in rows)})",
            (iso(now), *[row["id"] for row in rows]),
        )
    return rows


def finish_dispatch(conn: sqlite3.Connection, ids: list[int], now: datetime, sent: bool) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    if sent:
        conn.execute(
            f"UPDATE dispatch_queue SET status = 'sent', sent_at = ? WHERE id IN ({placeholders})", (iso(now), *ids)
        )
    else:
        conn.execute(
            f"UPDATE dispatch_queue SET status = 'pending', claimed_at = NULL WHERE id IN ({placeholders})", tuple(ids)
        )


# ---------------------------------------------------------------------------
# lead log / requests / drafts
# ---------------------------------------------------------------------------


def _insert_row(conn: sqlite3.Connection, table: str, row: dict) -> None:
    columns = list(row.keys())
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )


def insert_lead_log(conn: sqlite3.Connection, row: dict) -> None:
    _insert_row(conn, "lead_log", row)


def insert_service_request(conn: sqlite3.Connection, row: dict) -> None:
    _insert_row(conn, "service_requests", row)


def insert_draft_reply(conn: sqlite3.Connection, row: dict) -> None:
    _insert_row(conn, "draft_replies", row)


# ---------------------------------------------------------------------------
# customers and jobs
# ---------------------------------------------------------------------------


def _is_known(value: object) -> bool:
    return str(value or "").strip().lower() not in UNKNOWN_VALUES


def upsert_customer(conn: sqlite3.Connection, first_name: str, last_name: str, email: str, phone: str, address: str) -> int:
    """Insert or merge a customer. Never overwrites a known value with a blank.

    WHY (AUDIT H8): a customer's follow-up "any update?" email has no phone or
    address, and the old upsert replaced their saved details with blanks.
    """
    email = str(email or "").strip().lower()
    existing = conn.execute("SELECT * FROM customers WHERE email = ?", (email,)).fetchone() if email else None
    incoming = {"first_name": first_name, "last_name": last_name, "phone": phone, "address": address}
    if existing:
        merged = {
            key: (value if _is_known(value) else existing.get(key) or "")
            for key, value in incoming.items()
        }
        conn.execute(
            "UPDATE customers SET first_name = ?, last_name = ?, phone = ?, address = ? WHERE customer_id = ?",
            (merged["first_name"], merged["last_name"], merged["phone"], merged["address"], existing["customer_id"]),
        )
        return int(existing["customer_id"])
    cleaned = {key: (value if _is_known(value) else "") for key, value in incoming.items()}
    cursor = conn.execute(
        "INSERT INTO customers (first_name, last_name, email, phone, address, notes) VALUES (?, ?, ?, ?, ?, '')",
        (cleaned["first_name"], cleaned["last_name"], email or None, cleaned["phone"], cleaned["address"]),
    )
    return int(cursor.lastrowid)


def find_duplicate_job(conn: sqlite3.Connection, customer_id: int, issue: str, address: str, service_type: str) -> dict | None:
    return conn.execute(
        """
        SELECT * FROM jobs
        WHERE customer_id = ?
          AND LOWER(IFNULL(issue_description, '')) = LOWER(?)
          AND LOWER(IFNULL(address, '')) = LOWER(?)
          AND LOWER(IFNULL(service_type, '')) = LOWER(?)
          AND status IN ('new', 'scheduled', 'in progress')
        ORDER BY job_id DESC
        LIMIT 1
        """,
        (customer_id, issue, address, service_type),
    ).fetchone()


def find_job_by_message(conn: sqlite3.Connection, message_key: str) -> dict | None:
    return conn.execute("SELECT * FROM jobs WHERE source_message_key = ?", (message_key,)).fetchone()


def make_ticket_id(job_id: int, created: datetime) -> str:
    """Ticket IDs are derived from the unique job row id.

    WHY (AUDIT M6): the old ID was time-to-the-second plus the tail of the
    Message-ID, which is always "AILCOM" for Gmail, so two leads in the same
    second got the same ticket number.
    """
    return f"TKT-{created.astimezone().strftime('%Y%m%d')}-{job_id:05d}"


def create_job(conn: sqlite3.Connection, customer_id: int, request: dict, message_key: str, now: datetime, needs_review: bool) -> tuple[int, str]:
    created = iso(now)
    cursor = conn.execute(
        """
        INSERT INTO jobs (
            customer_id, first_name, last_name, phone, service_type, issue_description, priority, status,
            created_at, scheduled_time, technician_assigned, last_updated_at, occurrence_count, address,
            maps_link, request_type, source_message_key, needs_review
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, '', '', ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            customer_id,
            request.get("first_name", ""),
            request.get("last_name", ""),
            request.get("phone", ""),
            request.get("service_detected", ""),
            request.get("issue", ""),
            request.get("priority", "Normal"),
            created,
            created,
            request.get("address", ""),
            request.get("maps_link", ""),
            request.get("request_type", "service"),
            message_key,
            "1" if needs_review else "0",
        ),
    )
    job_id = int(cursor.lastrowid)
    ticket_id = make_ticket_id(job_id, now)
    conn.execute("UPDATE jobs SET ticket_id = ? WHERE job_id = ?", (ticket_id, job_id))
    return job_id, ticket_id


def apply_duplicate(conn: sqlite3.Connection, job: dict, request: dict, mode: str, now: datetime) -> str:
    """Apply ticket_duplicate_mode to an existing open job.

    Every mode KEEPS the job's ticket ID, creation time, status, technician
    and schedule. WHY (AUDIT M2): the old CSV "replace" swapped in a brand-new
    row and lost the ticket number, status and assigned technician.

    * update  - refresh details from the new email, skipping blank/"Unknown" values
    * replace - overwrite the descriptive details with the new email's values
    * ignore  - (legacy name "delete") leave details alone, only count the repeat
    Returns the action name for logging.
    """
    fields = {
        "issue_description": request.get("issue", ""),
        "priority": request.get("priority", ""),
        "service_type": request.get("service_detected", ""),
        "address": request.get("address", ""),
        "maps_link": request.get("maps_link", ""),
        "request_type": request.get("request_type", ""),
        "first_name": request.get("first_name", ""),
        "last_name": request.get("last_name", ""),
        "phone": request.get("phone", ""),
    }
    if mode == "replace":
        updates = fields
        action = "replaced"
    elif mode == "ignore":
        updates = {}
        action = "ignored-duplicate"
    else:
        updates = {key: value for key, value in fields.items() if _is_known(value)}
        action = "updated"

    assignments = [f"{column} = ?" for column in updates]
    assignments += ["last_updated_at = ?", "occurrence_count = COALESCE(occurrence_count, 1) + 1"]
    conn.execute(
        f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
        (*updates.values(), iso(now), job["job_id"]),
    )
    return action


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def mark_job_followup_sent(conn: sqlite3.Connection, job_id: int, now: datetime) -> None:
    conn.execute("UPDATE jobs SET followup_sent_at = ? WHERE job_id = ?", (iso(now), job_id))


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

LEAD_LOG_COLUMNS = ["timestamp", "sender_email", "detected_name", "service_detected", "subject", "classification"]
SERVICE_REQUEST_COLUMNS = [
    "timestamp", "sender_email", "customer_name", "first_name", "last_name", "phone", "address",
    "maps_link", "issue", "priority", "request_type", "service_detected", "subject",
]
DRAFT_COLUMNS = [
    "timestamp", "sender_email", "draft_subject", "draft_body", "customer_name", "phone", "address",
    "maps_link", "service_detected", "priority",
]
TICKET_COLUMNS = [
    "ticket_id", "created_at", "last_updated_at", "occurrence_count", "status", "customer_name", "first_name",
    "last_name", "sender_email", "address", "maps_link", "issue", "priority", "service_detected",
    "request_type", "assigned_technician",
]


def _csv_text(columns: list[str], rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: "" if row.get(column) is None else row.get(column) for column in columns})
    return buffer.getvalue()


def export_csvs(conn: sqlite3.Connection, paths: dict[str, Path]) -> list[str]:
    """Regenerate every CSV from the database. Returns a list of failures.

    WHY exports instead of the old appends: the CSVs used to be a second copy
    of the data that drifted from SQLite (AUDIT M1). They keep the same file
    names and columns so any spreadsheet the client uses still works, but the
    database is the only thing the agent reads. A failed export (for example,
    the file is open in Excel) is reported but never blocks mail processing.
    """
    queries = {
        "leads_csv_file": (LEAD_LOG_COLUMNS, "SELECT * FROM lead_log ORDER BY timestamp"),
        "service_requests_file": (SERVICE_REQUEST_COLUMNS, "SELECT * FROM service_requests ORDER BY timestamp"),
        "draft_replies_file": (DRAFT_COLUMNS, "SELECT * FROM draft_replies ORDER BY timestamp"),
        "job_tickets_file": (
            TICKET_COLUMNS,
            """
            SELECT j.ticket_id, j.created_at, j.last_updated_at, j.occurrence_count, j.status,
                   TRIM(IFNULL(j.first_name, '') || ' ' || IFNULL(j.last_name, '')) AS customer_name,
                   j.first_name, j.last_name, c.email AS sender_email, j.address, j.maps_link,
                   j.issue_description AS issue, j.priority, j.service_type AS service_detected,
                   j.request_type, j.technician_assigned AS assigned_technician
            FROM jobs j LEFT JOIN customers c ON c.customer_id = j.customer_id
            ORDER BY j.job_id
            """,
        ),
    }
    failures: list[str] = []
    for key, (columns, query) in queries.items():
        path = paths.get(key)
        if not path:
            continue
        try:
            atomic_write_text(path, _csv_text(columns, conn.execute(query).fetchall()))
        except OSError as err:
            failures.append(f"{path}: {err}")
    return failures
