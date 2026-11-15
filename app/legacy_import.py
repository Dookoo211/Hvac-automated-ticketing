"""One-time import of pre-Phase-1 state files into SQLite.

Older versions kept dedup/cooldown/follow-up/dispatch state in txt/JSON files
and leads/requests/drafts in CSVs. On the first run of this version we:

1. back up every legacy file into ``backups/legacy-import-<timestamp>/``;
2. import them in ONE transaction (all or nothing);
3. record ``legacy_state_imported`` in the meta table so it never runs twice.

WHY fail loudly on a corrupt file instead of skipping it: a corrupt cooldown
or dedup file silently treated as empty is exactly the bug that caused
duplicate replies (AUDIT H5). The error message tells the operator which file
to fix or move aside.

Legacy follow-ups are imported as *cancelled*: re-arming them could email
customers about requests from long ago (AUDIT H2). Legacy job_tickets.csv is
not imported because every CSV ticket was already mirrored as a SQLite job.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import store
from .fileutil import backup_paths

META_KEY = "legacy_state_imported"


class LegacyImportError(RuntimeError):
    pass


def _legacy_paths(config: dict) -> dict[str, Path]:
    keys = [
        "processed_ids_file",
        "sender_cooldown_file",
        "follow_up_state_file",
        "dispatch_summary_state_file",
        "leads_csv_file",
        "service_requests_file",
        "draft_replies_file",
    ]
    return {key: Path(str(config[key])) for key in keys if str(config.get(key, "")).strip()}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise LegacyImportError(
            f"Legacy state file {path} is corrupt ({err}). Move it out of the data folder "
            "(a backup copy was already made) and start the agent again."
        ) from err
    if not isinstance(data, dict):
        raise LegacyImportError(f"Legacy state file {path} does not contain a JSON object.")
    return data


def _load_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _row_key(prefix: str, index: int, row: dict) -> str:
    digest = hashlib.sha1(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"legacy:{prefix}:{index}:{digest}"


def import_legacy_state_once(conn: sqlite3.Connection, config: dict, backup_root: Path, now: datetime) -> str | None:
    """Import legacy files if not done yet. Returns the backup folder used, or None."""
    if store.get_meta(conn, META_KEY):
        return None

    paths = _legacy_paths(config)
    existing = {key: path for key, path in paths.items() if path.exists()}
    if not existing:
        with store.transaction(conn):
            store.set_meta(conn, META_KEY, store.iso(now))
        return None

    backup_dir = backup_paths(existing.values(), backup_root, "legacy-import")

    with store.transaction(conn):
        path = existing.get("processed_ids_file")
        if path:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                message_id = line.strip()
                if message_id:
                    store.upsert_processed(
                        conn, message_id, status="done", now=now, message_id=message_id, note="legacy import"
                    )

        path = existing.get("sender_cooldown_file")
        if path:
            for sender, stamp in _load_json(path).items():
                parsed = store.parse_iso(stamp)
                if sender and parsed:
                    store.set_cooldown(conn, str(sender), parsed)

        path = existing.get("follow_up_state_file")
        if path:
            for message_key, record in _load_json(path).items():
                if not isinstance(record, dict) or not record.get("sender_email"):
                    continue
                lead_at = store.parse_iso(record.get("lead_timestamp")) or now
                store.add_follow_up(
                    conn, str(message_key), str(record["sender_email"]), str(record.get("detected_name", "")),
                    str(record.get("subject", "")), None, lead_at, 0,
                )
                store.set_follow_up_status(conn, str(message_key), "cancelled", now, "legacy import (not re-armed)")

        path = existing.get("dispatch_summary_state_file")
        if path:
            state = _load_json(path)
            for item in state.get("pending", []) or []:
                if isinstance(item, dict):
                    store.enqueue_dispatch(conn, item, now)
            if state.get("last_summary_at"):
                store.set_meta(conn, "dispatch_last_summary_at", str(state["last_summary_at"]))

        csv_imports = [
            ("leads_csv_file", "lead", store.insert_lead_log, store.LEAD_LOG_COLUMNS),
            ("service_requests_file", "request", store.insert_service_request, store.SERVICE_REQUEST_COLUMNS),
            ("draft_replies_file", "draft", store.insert_draft_reply, store.DRAFT_COLUMNS),
        ]
        for key, prefix, insert, columns in csv_imports:
            path = existing.get(key)
            if not path:
                continue
            for index, row in enumerate(_load_csv(path)):
                cleaned = {column: row.get(column, "") or "" for column in columns}
                insert(conn, {"message_key": _row_key(prefix, index, cleaned), **cleaned})

        store.set_meta(conn, META_KEY, store.iso(now))
        store.set_meta(conn, "legacy_import_backup", str(backup_dir))

    return str(backup_dir)
