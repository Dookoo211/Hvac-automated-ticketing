#!/usr/bin/env python3
"""HVAC Automatic Inquiry Response System (CLI MVP)."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import threading
import time as time_module
import traceback
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import crm, credentials, database, legacy_import, safety, scheduler, settings, store, technicians
from app.instance_lock import AlreadyRunningError, InstanceLock
from app.mailbox import ImapMailbox, MailAuthError, MailUnavailable, SendFailed, SmtpSender
from app.safety import mark_automatic
from app.fileutil import atomic_write_json, backup_paths
from app.settings import ConfigError


WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_OFFICE_SCHEDULE = {
    "mon": {"start": "08:00", "end": "17:00"},
    "tue": {"start": "08:00", "end": "17:00"},
    "wed": {"start": "08:00", "end": "17:00"},
    "thu": {"start": "08:00", "end": "17:00"},
    "fri": {"start": "08:00", "end": "17:00"},
    "sat": None,
    "sun": None,
}

DEFAULT_SERVICE_KEYWORD_MAP = {
    "Furnace Repair": [
        "furnace",
        "no heat",
        "heat not working",
        "heater not working",
        "heater dead",
        "heat not work",
        "furnace stopped working",
        "pilot light out",
    ],
    "AC Installation": [
        "ac installation",
        "ac install",
        "new ac",
        "replace ac",
        "quote for ac",
    ],
    "AC Repair": [
        "ac not cooling",
        "air conditioner not cooling",
        "no ac",
        "ac broken",
        "ac repair",
        "system smells weird",
        "air smells weird",
    ],
    "Heat Pump Repair": [
        "heat pump",
        "heat pump not working",
        "heat pump repair",
    ],
    "Thermostat Service": [
        "thermostat",
        "house is freezing",
        "shows 72",
        "temperature shows",
        "thermostat issue",
    ],
    "Duct Cleaning": [
        "duct cleaning",
        "clean ducts",
        "dirty ducts",
    ],
}

DEFAULT_CONFIG = {
    "business_name": "Example HVAC",
    "default_service_area": "",
    "dispatcher_phone": "",
    "services": [
        "Furnace Repair",
        "AC Installation",
        "Heat Pump Repair",
        "Thermostat Service",
        "Duct Cleaning",
    ],
    "office_hours": "Mon-Fri 8am-5pm",
    "office_timezone": "America/Toronto",
    "office_schedule": deepcopy(DEFAULT_OFFICE_SCHEDULE),
    "phone_number": "555-123-4567",
    "lead_keywords": [
        "quote",
        "estimate",
        "repair",
        "install",
        "not working",
        "broken",
        "no heat",
        "no ac",
        "service",
        "emergency",
    ],
    "existing_keywords": [
        "invoice",
        "follow up",
        "scheduled",
        "warranty",
        "technician",
        "appointment",
    ],
    "spam_keywords": [
        "seo",
        "marketing",
        "backlinks",
        "unsubscribe",
        "crypto",
        "mailer-daemon",
        "mail delivery subsystem",
        "delivery status notification",
        "undelivered",
        "returned mail",
    ],
    "service_keyword_map": deepcopy(DEFAULT_SERVICE_KEYWORD_MAP),
    "imap_server": "imap.example.com",
    "imap_port": 993,
    "smtp_server": "smtp.example.com",
    "smtp_port": 587,
    "email_address": "you@example.com",
    # NOTE: no "email_password" default on purpose. The password lives in
    # Windows Credential Manager (app/credentials.py); see AUDIT C5.
    "mailbox": "INBOX",
    "sent_mailbox": "Sent",
    "read_unseen_only": True,
    "processed_ids_file": "data/processed_ids.txt",
    "leads_csv_file": "data/hvac_leads.csv",
    "service_requests_file": "data/service_requests.csv",
    "draft_replies_file": "data/draft_replies.csv",
    "job_tickets_file": "data/job_tickets.csv",
    "db_path": "data/hvac_agent.db",
    "sender_cooldown_file": "data/sender_cooldown.json",
    "auto_reply_cooldown_hours": 24,
    "auto_reply_enabled": False,
    "follow_up_enabled": False,
    "follow_up_delay_hours": 24,
    "follow_up_state_file": "data/follow_up_state.json",
    "dispatch_summary_enabled": True,
    "dispatch_summary_interval_minutes": 60,
    "dispatch_summary_recipient": "dispatch@example.com",
    "dispatch_summary_state_file": "data/dispatch_summary_state.json",
    "dispatch_summary_log_file": "logs/dispatch_summary.log",
    "high_priority_keywords": [
        "emergency",
        "urgent",
        "asap",
        "no heat",
        "no ac",
        "not working",
        "furnace stopped",
        "dead",
        "freezing",
    ],
    "ticket_duplicate_mode": "update",
    "license_key": "DEMO-TRIAL",
    "license_keys_file": "valid_licenses.json",
    "license_binding_file": "data/license_binding.json",
    "license_enforcement": True,
    "sms_enabled": False,
    "sms_gateway_email": "",
    "feature_auto_reply": False,
    "feature_ticketing": True,
    "feature_dispatch_summary": True,
    "feature_sms_notifications": False,
    "feature_follow_up": False,
    "ignore_promotions": True,
    "poll_interval_seconds": 30,
    "heartbeat_interval_minutes": 30,
    "ui_refresh_seconds": 5,
    "error_log_file": "logs/error.log",
    "agent_log_file": "logs/hvac_agent.log",
    "dry_run": False,
    # --- Safety settings added in Phase 1 (see AUDIT.md) ---
    # Extra addresses that belong to this business (aliases). Mail from these
    # is never processed or replied to (AUDIT C4).
    "own_addresses": [],
    # Keep the original behaviour of marking handled mail as read, but only
    # AFTER it has been fully processed (AUDIT C1).
    "mark_processed_as_read": True,
    # Only auto-reply when the mail server vouches for the sender
    # (aligned DKIM/SPF or DMARC pass), so forged From: addresses cannot use
    # this mailbox as a relay (AUDIT H9).
    "require_sender_auth": True,
    "trusted_authserv_ids": ["mx.google.com"],
    # Hard caps so a flood of inbound mail cannot get the account suspended (AUDIT M10).
    "max_auto_emails_per_day": 50,
    "max_sms_per_day": 30,
    # Never acknowledge mail older than this (protects the first run against
    # a backlog of old unread mail, and long outages).
    "max_reply_age_hours": 48,
    "network_timeout_seconds": 30,
}

# Keys that exist only at runtime and must never be written to config.json.
RUNTIME_ONLY_KEYS = {"config_path", "email_password"}

ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+?(?:\s(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Terrace|Ter|Place|Pl|Crescent|Cres|Ridge|Trail|Trl|Circle|Cir))(?:\s+(?:Apt|Unit|#)\s*[A-Za-z0-9-]+)?\b\.?",
    flags=re.IGNORECASE,
)

DEFAULT_HVAC_CONTEXT_KEYWORDS = [
    "hvac",
    "furnace",
    "heater",
    "heat",
    "no heat",
    "heating",
    "heating system",
    "ac",
    "air conditioner",
    "thermostat",
    "heat pump",
    "duct",
    "vent",
    "house is cold",
    "home is cold",
    "house never gets warm",
    "home never gets warm",
    "never gets warm",
    "not warm",
    "air in the house",
    "system turns on",
    "system running",
    "everything seems to be running",
    # Added in Phase 1 (AUDIT H4): common HVAC equipment the old list missed,
    # e.g. "boiler won't fire" and "AC blowing warm air" scored zero.
    "boiler",
    "radiator",
    "radiators",
    "mini split",
    "mini-split",
    "ductless",
    "air conditioning",
    "central air",
    "a/c",
    "ac unit",
    "compressor",
    "condenser",
    "water heater",
    "carbon monoxide",
]

DEFAULT_HVAC_PROBLEM_KEYWORDS = [
    "not working",
    "stopped working",
    "dead",
    "freezing",
    "broken",
    "weird smell",
    "smells weird",
    "repair",
    "service",
    "never gets warm",
    "not warm",
    "not heating",
    "not getting warm",
    "house is cold",
    "home is cold",
    "heat isnt working",
    "heat isn't working",
    # Added in Phase 1 (AUDIT H4)
    "not cooling",
    "blowing warm",
    "warm air",
    "leak",
    "leaking",
    "noise",
    "noisy",
    "banging",
    "rattling",
    "won't turn on",
    "wont turn on",
    "not turning on",
    "won't fire",
    "wont fire",
    "no hot air",
    "no cold air",
    "short cycling",
    "frozen",
    "cold",
]

STRONG_HVAC_PROBLEM_KEYWORDS = [
    "not working",
    "stopped working",
    "dead",
    "freezing",
    "broken",
    "no heat",
    "no ac",
    "never gets warm",
    "not warm",
    "not heating",
    "not getting warm",
    "house is cold",
    "home is cold",
    "heat isnt working",
    "heat isn't working",
    "smells weird",
    "weird smell",
    "not cooling",
    "blowing warm",
    "leaking",
    "won't turn on",
    "not turning on",
    "won't fire",
]

DEFAULT_NON_HVAC_KEYWORDS = [
    "kitchen sink",
    "sink",
    "toilet",
    "plumbing",
    "oven",
    "breaker",
    "dishwasher",
    "roof leak",
]

# Phrases that only bulk/marketing mail uses. WHY trimmed in Phase 1
# (AUDIT H4): the old list contained everyday words real customers write
# ("confirm", "update", "availability", "deal", "special", "account",
# "feedback", "calendar") and a single hit turned a genuine quote request into
# spam. Keep this list to multi-word phrases a homeowner wouldn't type.
DEFAULT_MARKETING_KEYWORDS = [
    "unsubscribe",
    "manage preferences",
    "manage your preferences",
    "email preferences",
    "view in browser",
    "view this email in your browser",
    "newsletter",
    "security alert",
    "verify your email",
    "confirm your email",
    "thanks for signing up",
    "get started",
    "privacy policy",
    "terms of service",
    "special offer",
    "limited time offer",
    "promo code",
    "coupon code",
    "free trial",
    "you're receiving this email",
    "you are receiving this email",
]

# Sales/research outreach ("can we grab 20 minutes?"). Also phrases only.
DEFAULT_OUTREACH_KEYWORDS = [
    "product manager",
    "user research",
    "customer interview",
    "quick chat",
    "schedule a call",
    "schedule a chat",
    "book a time",
    "grab a time",
    "time that works for you",
    "we'd love to learn",
    "we would love to learn",
    "learn more about your experience",
    "calendly.com",
]

# (?<!\d)/(?!\d): don't pick 10 digits out of the middle of a longer
# order/account number (AUDIT M9).
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?1[\s\-\.]?)?(?:\(?\d{3}\)?[\s\-\.]?)\d{3}[\s\-\.]?\d{4}(?!\d)",
    flags=re.IGNORECASE,
)


@dataclass
class ClassificationResult:
    """Classification outcome and raw per-class scores for diagnostics."""
    classification: str
    lead_score: int
    existing_score: int
    spam_score: int


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override values into a deep-copied base dictionary."""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_office_schedule(schedule: object) -> dict:
    """Normalize office hours into a 7-day map with HH:MM start/end entries or null days."""
    normalized = deepcopy(DEFAULT_OFFICE_SCHEDULE)
    if not isinstance(schedule, dict):
        return normalized

    for day in WEEKDAY_KEYS:
        value = schedule.get(day)
        if value in (None, "", False):
            normalized[day] = None
            continue

        if isinstance(value, dict):
            start = str(value.get("start", "")).strip()
            end = str(value.get("end", "")).strip()
            if start and end:
                normalized[day] = {"start": start, "end": end}
                continue

        if isinstance(value, list) and len(value) == 2:
            start = str(value[0]).strip()
            end = str(value[1]).strip()
            if start and end:
                normalized[day] = {"start": start, "end": end}

    return normalized


def load_config(config_path: Path) -> dict:
    """Load config.json, merge defaults, collapse legacy flags and validate.

    Raises ConfigError listing *every* problem at once (AUDIT M3), so a client
    fixing their config does not have to discover issues one restart at a time.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    # utf-8-sig: tolerate the byte-order mark Windows Notepad adds when saving.
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as err:
        raise ConfigError([f"config.json is not valid JSON (line {err.lineno}, column {err.colno}): {err.msg}"]) from err
    if not isinstance(raw_config, dict):
        raise ConfigError(["config.json must contain a JSON object ({ ... })."])

    config = deep_merge(DEFAULT_CONFIG, raw_config)
    warnings = settings.resolve_flag_pairs(raw_config, config)
    config["office_schedule"] = normalize_office_schedule(config.get("office_schedule"))

    problems = settings.validate_config(config)
    if problems:
        raise ConfigError(problems)

    config["ticket_duplicate_mode"] = settings.normalize_duplicate_mode(config.get("ticket_duplicate_mode"))
    for warning in warnings + settings.config_warnings(config):
        print(f"CONFIG WARNING: {warning}")
    return config


def has_email_credentials(config: dict) -> bool:
    """Return True if an address is set and a password is available
    (Credential Manager, or a legacy plaintext value in config.json)."""
    return bool(str(config.get("email_address", "")).strip()) and credentials.has_credentials(config)


def system_now() -> datetime:
    """Return current system-local datetime with timezone info."""
    return datetime.now().astimezone()


def utc_now_iso() -> str:
    """Return current system-local timestamp as ISO-8601 string."""
    return system_now().isoformat()


def append_error_log(config: dict, context: str, err: Exception) -> None:
    """Append structured error context and traceback to the configured error log."""
    error_log_path = Path(str(config.get("error_log_file", "error.log")))
    error_log_path.parent.mkdir(parents=True, exist_ok=True)
    stack = traceback.format_exc()
    entry = (
        f"[{utc_now_iso()}] {context}\n"
        f"{type(err).__name__}: {err}\n"
        f"{stack}\n"
    )
    with error_log_path.open("a", encoding="utf-8") as f:
        f.write(entry)


def log_event(config: dict, event: str, **fields: str) -> None:
    """Append a structured event record to the agent log file."""
    log_path = Path(str(config.get("agent_log_file", "logs/hvac_agent.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": utc_now_iso(), "event": event}
    payload.update(fields)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def get_recent_activity(config: dict, limit: int = 6) -> list[str]:
    """Return recent activity lines from the agent log."""
    log_path = Path(str(config.get("agent_log_file", "logs/hvac_agent.log")))
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []

    items: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
            timestamp = entry.get("timestamp", "")
            event = entry.get("event", "activity")
            sender = entry.get("sender_email", "")
            subject = entry.get("subject", "")
            detail = f"{event}"
            if sender:
                detail += f" | {sender}"
            if subject:
                detail += f" | {subject}"
            if timestamp:
                detail = f"{timestamp} - {detail}"
            items.append(detail)
        except json.JSONDecodeError:
            items.append(line)
    return items


def load_or_initialize_license_keys(path: Path) -> set[str]:
    """Load local valid license keys; create a starter file when missing."""
    if not path.exists():
        starter = {"valid_keys": [f"HVAC-TRIAL-{idx:03d}" for idx in range(1, 101)]}
        path.write_text(json.dumps(starter, indent=2), encoding="utf-8")
        return set(starter["valid_keys"])

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()

    if isinstance(data, dict) and isinstance(data.get("valid_keys"), list):
        return {str(item).strip() for item in data["valid_keys"] if str(item).strip()}

    if isinstance(data, list):
        return {str(item).strip() for item in data if str(item).strip()}

    return set()


def validate_license(config: dict) -> None:
    """Validate configured license key against local license registry."""
    if not bool(config.get("license_enforcement", True)):
        return

    license_key = str(config.get("license_key", "")).strip()
    if not license_key:
        raise PermissionError("Missing license_key in config.json")

    key_path = Path(str(config.get("license_keys_file", "valid_licenses.json")))
    valid_keys = load_or_initialize_license_keys(key_path)
    if license_key not in valid_keys:
        match = re.match(r"^HVAC-TRIAL-(\d{3})$", license_key)
        if match:
            try:
                trial_num = int(match.group(1))
            except ValueError:
                trial_num = 0
            if 1 <= trial_num <= 100:
                return
        raise PermissionError("Invalid license_key. Contact support for activation.")

    enforce_license_binding(config)


def enforce_license_binding(config: dict) -> None:
    """Bind a license key to a single email address on first run."""
    if not bool(config.get("license_enforcement", True)):
        return

    license_key = str(config.get("license_key", "")).strip()
    email_address = str(config.get("email_address", "")).strip().lower()
    if not license_key or not email_address:
        raise PermissionError("Missing license_key or email_address for license binding.")

    binding_path = Path(str(config.get("license_binding_file", "data/license_binding.json")))
    if binding_path.exists():
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            binding = {}
        bound_key = str(binding.get("license_key", "")).strip()
        bound_email = str(binding.get("email", "")).strip().lower()
        if bound_key and bound_key != license_key:
            raise PermissionError("License key mismatch for this installation.")
        if bound_email and bound_email != email_address:
            raise PermissionError("License key already bound to a different email.")
        return

    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(
        json.dumps(
            {
                "license_key": license_key,
                "email": email_address,
                "bound_at": utc_now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def write_license_binding(config: dict) -> None:
    """Write/update the license binding file to match current config."""
    if not bool(config.get("license_enforcement", True)):
        return
    license_key = str(config.get("license_key", "")).strip()
    email_address = str(config.get("email_address", "")).strip().lower()
    if not license_key or not email_address:
        return
    binding_path = Path(str(config.get("license_binding_file", "data/license_binding.json")))
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(
        json.dumps(
            {
                "license_key": license_key,
                "email": email_address,
                "bound_at": utc_now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def config_for_disk(config: dict) -> dict:
    """Return a copy of config that is safe to write to config.json.

    WHY: setup and the UI used to dump the whole in-memory config, which put
    the mailbox password (and runtime-only keys) into config.json (AUDIT C5).
    """
    return {key: value for key, value in config.items() if key not in RUNTIME_ONLY_KEYS and not key.startswith("_")}


def write_config_files(config: dict, config_path: Path) -> list[Path]:
    """Write config.json (atomically, without secrets) and keep root/release configs in sync."""
    targets: list[Path] = [config_path.resolve()]
    project_root = Path(__file__).resolve().parent
    root_config = project_root / "config.json"
    release_config = project_root / "release" / "config.json"

    if config_path.resolve() == root_config.resolve():
        if release_config.exists():
            targets.append(release_config.resolve())
    elif config_path.resolve() == release_config.resolve():
        if root_config.exists():
            targets.append(root_config.resolve())

    safe = config_for_disk(config)
    for path in targets:
        atomic_write_json(path, safe)
        ensure_runtime_paths(config, path.parent)
    return targets


def patch_config_file(config_path: Path, updates: dict) -> None:
    """Change only the given keys in config.json, leaving everything else as the user wrote it.

    WHY: the UI used to rewrite config.json from the merged in-memory dict on
    every checkbox click (AUDIT M12), which added every default and the password.
    """
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    raw.update(updates)
    # Drop runtime-only keys an older version may have written. We do NOT
    # drop a legacy plaintext email_password here: on an old install it may be
    # the only copy, and a checkbox click must never lose the login.
    # scripts/migrate_config.py moves it to Credential Manager safely.
    for key in RUNTIME_ONLY_KEYS - {"email_password"}:
        raw.pop(key, None)
    atomic_write_json(config_path, raw)


def store_mail_password(config: dict, new_password: str) -> None:
    """Save the mailbox password to Windows Credential Manager (AUDIT C5).

    Must run BEFORE config.json is written, because config.json never holds
    the password any more. If no new password was typed but an old install
    still has one in plaintext, that one is moved over, so pressing Enter in
    setup can never lose the login. Raises credentials.CredentialError.
    """
    password = (new_password or "").replace(" ", "").strip() or credentials.plaintext_password(config)
    if password:
        credentials.set_password(config["email_address"], password)
    config.pop("email_password", None)


def ensure_runtime_paths(config: dict, base_dir: Path) -> None:
    """Create runtime folders/files directories relative to the config location."""
    path_fields = [
        "processed_ids_file",
        "leads_csv_file",
        "service_requests_file",
        "draft_replies_file",
        "job_tickets_file",
        "db_path",
        "sender_cooldown_file",
        "follow_up_state_file",
        "dispatch_summary_state_file",
        "dispatch_summary_log_file",
        "error_log_file",
        "agent_log_file",
        "license_binding_file",
    ]
    for key in path_fields:
        raw = str(config.get(key, "")).strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)


def append_dispatch_log(path: Path, content: str) -> None:
    """Append dispatch summary snapshots to a plain text operation log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{utc_now_iso()}]\n{content}\n\n")




def clean_subject(raw_subject: str | None) -> str:
    """Decode MIME/encoded-word email subjects into plain text."""
    if not raw_subject:
        return ""
    try:
        return str(make_header(decode_header(raw_subject))).strip()
    except Exception:
        return raw_subject.strip()


def extract_plain_text(msg: Message) -> str:
    """Extract plain-text message body, falling back to stripped HTML."""
    if msg.is_multipart():
        parts: list[str] = []
        html_parts: list[str] = []

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue

            try:
                payload = part.get_content()
            except Exception:
                payload = None

            if not isinstance(payload, str):
                continue

            if content_type == "text/plain":
                parts.append(payload)
            elif content_type == "text/html":
                html_parts.append(payload)

        if parts:
            return "\n".join(parts).strip()
        if html_parts:
            return strip_html("\n".join(html_parts))
        return ""

    try:
        content = msg.get_content()
        if isinstance(content, str):
            if msg.get_content_type() == "text/html":
                return strip_html(content)
            return content.strip()
    except Exception:
        pass

    return ""


def strip_html(html_text: str) -> str:
    """Remove script/style/tags and collapse whitespace from HTML body content."""
    no_script = re.sub(r"<script.*?>.*?</script>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    no_style = re.sub(r"<style.*?>.*?</style>", " ", no_script, flags=re.IGNORECASE | re.DOTALL)
    no_tags = re.sub(r"<[^>]+>", " ", no_style)
    return re.sub(r"\s+", " ", no_tags).strip()


def normalize_text(text: str) -> str:
    """Lower-case and normalize all whitespace for keyword matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


def keyword_hits(text: str, keyword: str) -> int:
    """Count whole-word/phrase occurrences of keyword in (normalized) text.

    WHY no substring fallback any more (AUDIT H3): the old version fell back
    to ``str.count`` when the word-boundary search found nothing, so "ac"
    matched "contact", "heat" matched "theater", "dead" matched "deadline" and
    "vent" matched "event". A note about a theater-event deadline was
    classified as a furnace lead.
    """
    key = keyword.lower().strip()
    if not key:
        return 0
    # (?<!\w) / (?!\w) instead of \b so keywords that start/end with
    # punctuation (e.g. "a/c") still match as whole tokens.
    pattern = r"(?<!\w)" + re.escape(key) + r"(?!\w)"
    return len(re.findall(pattern, text))


def hvac_intent_boost(text: str) -> tuple[int, int]:
    """Return (lead_boost, existing_boost) from HVAC/non-HVAC context heuristics."""
    hvac_context_hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_HVAC_CONTEXT_KEYWORDS)
    hvac_problem_hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_HVAC_PROBLEM_KEYWORDS)
    non_hvac_hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_NON_HVAC_KEYWORDS)

    lead_boost = 0
    existing_boost = 0
    if hvac_context_hits > 0 and hvac_problem_hits > 0:
        lead_boost += 4
    elif hvac_context_hits >= 2:
        lead_boost += 2
    elif hvac_context_hits == 1 and hvac_problem_hits > 0:
        lead_boost += 2

    # If clearly non-HVAC and no HVAC context, avoid accidental lead classification.
    if non_hvac_hits > 0 and hvac_context_hits == 0:
        existing_boost += 3

    return lead_boost, existing_boost


def is_hvac_related(subject: str, body: str, service_detected: str = "") -> bool:
    """Return True if message appears HVAC-related."""
    text = normalize_text(f"{subject}\n{body}")
    hvac_context_hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_HVAC_CONTEXT_KEYWORDS)
    hvac_problem_hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_HVAC_PROBLEM_KEYWORDS)
    if service_detected and service_detected != "Unknown":
        return True
    if hvac_context_hits > 0:
        return True
    if hvac_problem_hits > 0:
        intent_terms = ("house", "home", "heat", "heating", "warm", "cold", "furnace", "ac", "thermostat")
        if any(keyword_hits(text, term) for term in intent_terms):
            return True
    return False


def is_marketing_email(sender_email: str, subject: str, body: str, msg: Message | None = None) -> bool:
    """Detect marketing/newsletter/notification/outreach emails.

    Signals: no-reply style sender, mailing-list headers, any outreach phrase,
    or at least two marketing phrases. WHY two (AUDIT H4): one stray phrase
    (a customer forwarding a promo, a footer) shouldn't turn a lead into spam.
    """
    text = normalize_text(f"{subject}\n{body}")
    sender = (sender_email or "").lower()
    if any(tag in sender for tag in ("no-reply", "noreply", "donotreply", "do-not-reply", "mailer-daemon")):
        return True
    if msg is not None:
        try:
            if msg.get("List-Unsubscribe") or msg.get("List-Id"):
                return True
        except Exception:
            pass
    if sum(keyword_hits(text, keyword) for keyword in DEFAULT_OUTREACH_KEYWORDS) > 0:
        return True
    return sum(keyword_hits(text, keyword) for keyword in DEFAULT_MARKETING_KEYWORDS) >= 2


def has_customer_intent(subject: str, body: str, structured_fields: bool) -> bool:
    """Return True when the message reads like a real customer HVAC request.

    WHY phone/address alone no longer count (AUDIT M9): nearly every marketing
    footer contains a phone number and street address, which used to exempt
    newsletters from the spam rule. (The old version also had two branches
    that both returned True.)
    """
    if structured_fields:
        return True
    text = normalize_text(f"{subject}\n{body}")
    return any(keyword_hits(text, keyword) for keyword in STRONG_HVAC_PROBLEM_KEYWORDS)


def classify(subject: str, body: str, config: dict) -> ClassificationResult:
    """Compute weighted class scores and return final lead/existing/spam decision."""
    haystack = normalize_text(f"{subject}\n{body}")

    lead_keywords = set(str(kw).strip().lower() for kw in config.get("lead_keywords", []))
    lead_keywords.update(
        {
            "heater",
            "furnace",
            "heat not work",
            "stopped working",
            "dead",
            "freezing",
            "thermostat",
            "smells weird",
            "weird smell",
            "never gets warm",
            "not warm",
            "not heating",
            "not getting warm",
            "house is cold",
            "home is cold",
            "heat isnt working",
            "heat isn't working",
            # Added in Phase 1 (AUDIT H4)
            "boiler",
            "radiator",
            "not cooling",
            "blowing warm",
            "warm air",
            "leaking",
            "won't turn on",
            "not turning on",
            "no hot air",
            "no cold air",
            "heat pump",
            "air conditioner",
            "mini split",
            "ac unit",
        }
    )
    lead_score = sum(keyword_hits(haystack, kw) * 2 for kw in lead_keywords)
    existing_score = sum(keyword_hits(haystack, kw) * 2 for kw in config.get("existing_keywords", []))
    spam_score = sum(keyword_hits(haystack, kw) * 3 for kw in config.get("spam_keywords", []))
    spam_keywords = {
        "mailer-daemon",
        "mail delivery subsystem",
        "delivery status notification",
        "undelivered",
        "returned mail",
    }
    spam_score += sum(keyword_hits(haystack, kw) * 5 for kw in spam_keywords)
    spam_score += sum(keyword_hits(haystack, kw) * 2 for kw in DEFAULT_MARKETING_KEYWORDS)
    lead_boost, existing_boost = hvac_intent_boost(haystack)
    lead_score += lead_boost
    existing_score += existing_boost

    scores = {
        "lead": lead_score,
        "existing": existing_score,
        "spam": spam_score,
    }

    # WHY "unclear" (AUDIT H4): ties and all-zero scores used to become
    # "existing", which means no ticket at all, so real leads with unusual
    # wording vanished. The caller turns "unclear" into "review" (ticket for
    # staff, NO auto-reply) when the mail is HVAC-related, else "existing".
    max_score = max(scores.values())
    if max_score == 0:
        classification = "unclear"
    else:
        winners = [label for label, score in scores.items() if score == max_score]
        classification = winners[0] if len(winners) == 1 else "unclear"

    return ClassificationResult(
        classification=classification,
        lead_score=lead_score,
        existing_score=existing_score,
        spam_score=spam_score,
    )


def resolve_service_name(service: str, configured_services: list[str]) -> str:
    """Return the detected service unchanged.

    WHY (AUDIT M7): this used to rename "AC Repair" to "AC Installation" when
    the catalog had no AC Repair entry, which told a customer with a broken AC
    that we'd logged an installation request. A service outside the catalog is
    still the more accurate label for staff.
    """
    return service


def detect_service(subject: str, body: str, config: dict) -> str:
    """Infer the likely HVAC service from configured mappings and safety fallbacks."""
    text = normalize_text(f"{subject}\n{body}")
    services = config.get("services", [])

    # Whole-word matching throughout (AUDIT H3): "heater" must not match "theater".
    for service in services:
        if keyword_hits(text, service):
            return service

    service_map = config.get("service_keyword_map", {})
    best_service = ""
    best_score = 0

    for service_name, keywords in service_map.items():
        if not isinstance(keywords, list):
            continue
        score = sum(keyword_hits(text, str(keyword)) for keyword in keywords)
        if score > best_score:
            best_service = str(service_name)
            best_score = score

    if best_score > 0:
        return resolve_service_name(best_service, services)

    def has_any(*phrases: str) -> bool:
        return any(keyword_hits(text, phrase) for phrase in phrases)

    if has_any("no heat", "furnace", "heater", "heat not work"):
        return "Furnace Repair"

    if has_any("ac not cooling", "air conditioner not cooling"):
        return "AC Repair"

    if has_any("thermostat", "house is freezing", "shows 72"):
        return "Thermostat Service"

    if has_any("heat pump"):
        return "Heat Pump Repair"

    if has_any("duct", "ducts", "duct cleaning"):
        return "Duct Cleaning"

    return "Unknown"


def infer_request_type(subject: str, body: str) -> str:
    """Infer whether a message is a quote inquiry or service request."""
    text = normalize_text(f"{subject}\n{body}")
    if keyword_hits(text, "quote") or keyword_hits(text, "estimate"):
        return "quote"
    if any(keyword_hits(text, phrase) for phrase in ("maintenance", "tune up", "tune-up")):
        return "maintenance"
    return "service"


def fallback_name_from_email(sender_email: str) -> str:
    """Create a readable fallback name from an email local-part."""
    local_part = sender_email.split("@")[0].strip()
    if not local_part:
        return "there"
    cleaned = re.sub(r"[._-]+", " ", local_part)
    tokens = [token for token in cleaned.split() if token]
    return " ".join(token.capitalize() for token in tokens) if tokens else "there"


NAME_TOKEN = r"[A-Z][a-zA-Z'\-]+"
PERSON_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,39}$")


def looks_like_person_name(value: str) -> bool:
    """1-4 words of letters only, max 40 chars: safe to put after "Hi"."""
    value = " ".join(str(value or "").split())
    return bool(value) and bool(PERSON_NAME_RE.match(value)) and len(value.split()) <= 4


def extract_customer_name(body: str, sender_name: str, sender_email: str) -> str:
    """Best guess at the customer's name for the ticket.

    Order: explicit "Name: X" line, "my name is X" (capitalised words only),
    the From: display name, a capitalised signature after "Thanks,", and
    finally the email's local part.

    WHY (AUDIT M8): the old patterns "i am ..." and "this is ..." produced
    names like "Having Trouble With My" and "Urgent", which then appeared in
    the customer's greeting.
    """
    body_for_search = body.replace("\r", "\n")

    for raw_line in body_for_search.splitlines():
        line = raw_line.strip()
        line_match = re.match(r"^name\s*(?:is)?\s*[:\-]\s*([a-z][a-z .'-]{0,40})$", line, flags=re.IGNORECASE)
        if line_match:
            candidate = " ".join(line_match.group(1).split())
            return " ".join(part.capitalize() for part in candidate.split())

    match = re.search(rf"(?i:\bmy name is)\s+({NAME_TOKEN}(?:[ \t]+{NAME_TOKEN}){{0,2}})", body_for_search)
    if match:
        return match.group(1)

    if looks_like_person_name(sender_name):
        return " ".join(sender_name.split())

    signature = re.search(
        rf"(?i:\b(?:regards|thanks|thank you|sincerely|best|cheers)),?[ \t]*\n+[ \t]*({NAME_TOKEN}(?:[ \t]+{NAME_TOKEN}){{0,2}})[ \t]*$",
        body_for_search,
        flags=re.MULTILINE,
    )
    if signature:
        return signature.group(1)

    if sender_email:
        return fallback_name_from_email(sender_email)

    return "there"


def greeting_name(request: dict | None, sender_display_name: str) -> str:
    """Name used after "Hi" in customer emails: only something that clearly is a name.

    The email local-part fallback is deliberately excluded ("Hi Jsmith1987"
    reads as a bot); "there" is the safe default.
    """
    candidates = [sender_display_name]
    if request:
        candidates.insert(0, str(request.get("customer_name", "")))
    local_fallback = ""
    if request and request.get("sender_email"):
        local_fallback = fallback_name_from_email(str(request["sender_email"]))
    for candidate in candidates:
        candidate = " ".join(str(candidate or "").split())
        if candidate and candidate != local_fallback and looks_like_person_name(candidate):
            return candidate
    return "there"


def extract_address(body: str) -> str:
    """Extract a likely street address from message body text."""
    label_match = re.search(r"^\s*address\s*[:\-]\s*(.+)$", body, flags=re.IGNORECASE | re.MULTILINE)
    if label_match:
        # Capped: this value comes straight from the sender (AUDIT H9).
        candidate = label_match.group(1).strip()[:120]
        if candidate:
            return candidate

    match = ADDRESS_PATTERN.search(body)
    if match:
        return match.group(0).strip()

    # WHY the old "any line starting with a number" fallback is gone (AUDIT M9):
    # it extracted "20 minutes ago it stopped working" as an address.
    return "Unknown"


def extract_phone(body: str) -> str:
    """Extract first phone number from text."""
    match = PHONE_PATTERN.search(body)
    return match.group(0).strip() if match else ""


def has_structured_fields(body: str) -> bool:
    """Return True when the message contains labeled customer fields."""
    label_hits = 0
    for raw_line in body.replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^(name|phone|address)\s*[:\-]\s*.+", line, flags=re.IGNORECASE):
            label_hits += 1
    # WHY labels only (AUDIT M9): "has a phone number and a street address"
    # matched almost every marketing footer and forced newsletters to "lead".
    # Web-form style "Name: / Phone: / Address:" lines are the real signal.
    return label_hits >= 2


def make_google_maps_link(address: str) -> str:
    """Convert a detected address into a Google Maps search URL."""
    normalized = str(address or "").strip()
    if not normalized or normalized == "Unknown":
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(normalized)}"


def extract_issue_summary(subject: str, body: str, service_detected: str) -> str:
    """Build a concise issue summary from message content."""
    greeting_pattern = re.compile(r"^(hi|hello|hey|good (morning|afternoon|evening))\b[!,.]*$", re.IGNORECASE)
    cleaned_lines = []
    for raw_line in body.replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if greeting_pattern.match(line):
            continue
        cleaned_lines.append(line)
    cleaned_body = " ".join(cleaned_lines)
    text = f"{subject}. {cleaned_body}".strip()
    sentences = [part.strip() for part in re.split(r"[.!?]\s+", text) if part.strip()]
    issue_keywords = [
        "not working",
        "broken",
        "no heat",
        "no ac",
        "emergency",
        "repair",
        "quote",
        "maintenance",
        "never gets warm",
        "not warm",
        "not heating",
        "house is cold",
        "home is cold",
        "cold",
    ]
    for sentence in sentences:
        if greeting_pattern.match(sentence.strip()):
            continue
        candidate = sentence.lower()
        if any(keyword in candidate for keyword in issue_keywords):
            return sentence[:180]

    if service_detected and service_detected != "Unknown":
        return f"{service_detected} request"

    return subject[:180] if subject else "HVAC service inquiry"


def infer_priority(subject: str, body: str, config: dict) -> str:
    """Infer lead priority using configurable urgent keyword list."""
    text = normalize_text(f"{subject}\n{body}")
    high_priority_keywords = [normalize_text(str(item)) for item in config.get("high_priority_keywords", [])]
    # Whole words only: "dead" must not make a "deadline" email High priority.
    if any(keyword and keyword_hits(text, keyword) for keyword in high_priority_keywords):
        return "High"
    return "Normal"


def extract_service_request(
    subject: str,
    body: str,
    sender_name: str,
    sender_email: str,
    service_detected: str,
    config: dict,
) -> dict:
    """Extract structured HVAC request data from a lead message."""
    detected_address = extract_address(body)
    customer_name = extract_customer_name(body, sender_name, sender_email)
    first_name, last_name = split_first_last_name(customer_name)
    phone = extract_phone(body)
    return {
        "timestamp": utc_now_iso(),
        "customer_name": customer_name,
        "first_name": first_name,
        "last_name": last_name,
        "sender_email": sender_email or "Unknown",
        "phone": phone,
        "address": detected_address,
        "issue": extract_issue_summary(subject, body, service_detected),
        "priority": infer_priority(subject, body, config),
        "request_type": infer_request_type(subject, body),
        "service_detected": service_detected,
        "subject": subject,
        "maps_link": make_google_maps_link(detected_address),
    }


def generate_draft_reply(config: dict, request: dict, in_office_hours: bool) -> tuple[str, str]:
    """Generate a dynamic office-facing draft response for operational use."""
    business_name = str(config.get("business_name", "HVAC Team"))
    office_hours = str(config.get("office_hours", "business hours"))
    name = request.get("customer_name") or "there"
    issue = request.get("issue") or "your HVAC request"
    address = request.get("address") or "your location"
    maps_link = request.get("maps_link") or ""
    ticket_id = request.get("ticket_id") or ""

    subject = "Service Request Received"
    timing_line = (
        f"We have received your request and our team will review it during business hours ({office_hours})."
        if in_office_hours
        else f"We have received your request and our team will respond during the next business day ({office_hours})."
    )
    body = (
        f"Hi {name},\n\n"
        "Thanks for contacting us.\n\n"
        f"We've logged your request: {issue}\n"
        f"Service location: {address}\n\n"
        f"{('Ticket ID: ' + ticket_id + chr(10)) if ticket_id else ''}"
        f"{('Map: ' + maps_link + chr(10) + chr(10)) if maps_link else ''}"
        f"{timing_line}\n\n"
        f"Best regards,\n{business_name}\n"
    )
    return subject, body


def split_first_last_name(customer_name: str) -> tuple[str, str]:
    """Split free-form customer name into first and last components."""
    tokens = [token for token in str(customer_name).strip().split() if token]
    if not tokens:
        return ("", "")
    if len(tokens) == 1:
        return (tokens[0], "")
    return (tokens[0], " ".join(tokens[1:]))


def build_dispatch_summary_body(pending_items: list[dict]) -> str:
    """Build an hourly dispatch summary body from queued request items."""
    lines = ["New Service Requests:", ""]
    for idx, item in enumerate(pending_items, start=1):
        issue = item.get("issue", "HVAC request")
        address = item.get("address", "Unknown")
        priority = item.get("priority", "Normal")
        request_type = item.get("request_type", "service")
        ticket_id = item.get("ticket_id", "")
        maps_link = item.get("maps_link", "")
        ticket_segment = f"{ticket_id} | " if ticket_id else ""
        # "review" = HVAC-related but unclear; staff should read it (no auto-reply was sent).
        review_segment = "[REVIEW] " if item.get("review") else ""
        lines.append(f"{idx}. {ticket_segment}{review_segment}[{priority}] {issue} - {address} ({request_type})")
        if maps_link:
            lines.append(f"   Map: {maps_link}")
    return "\n".join(lines)


def extract_sender(msg: Message) -> tuple[str, str]:
    """Extract sender display name and normalized email address."""
    name, sender_email = parseaddr(msg.get("From", ""))
    return (name.strip(), sender_email.strip().lower())


def safe_recipient_name(display_name: str) -> str:
    """Return a polite fallback name when sender display name is missing."""
    return display_name if display_name else "there"


def parse_hhmm(value: str) -> time | None:
    """Parse a HH:MM string into a time object."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def office_timezone(config: dict):
    """Resolve configured IANA timezone; fallback to UTC if unavailable."""
    tz_name = str(config.get("office_timezone", "America/Toronto")).strip() or "America/Toronto"
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def is_within_office_hours(config: dict, now_utc: datetime | None = None) -> bool:
    """Check whether current time is within the configured local office schedule."""
    current_utc = now_utc or datetime.now(timezone.utc)
    local_now = current_utc.astimezone(office_timezone(config))

    day_key = WEEKDAY_KEYS[local_now.weekday()]
    day_schedule = config.get("office_schedule", {}).get(day_key)
    if not isinstance(day_schedule, dict):
        return False

    start = parse_hhmm(str(day_schedule.get("start", "")))
    end = parse_hhmm(str(day_schedule.get("end", "")))
    if not start or not end:
        return False

    current_time = local_now.time()
    if start <= end:
        return start <= current_time <= end

    return current_time >= start or current_time <= end


def _safe_header(msg: Message, name: str) -> str:
    """Read a header without letting a malformed one crash the cycle (AUDIT H6)."""
    try:
        return str(msg.get(name, "") or "")
    except Exception:
        return ""


def _one_line(value: str, limit: int = 998) -> str:
    """Collapse whitespace/newlines so a value is safe to put in a header."""
    return " ".join(str(value or "").split())[:limit]


def _reply_subject(original_subject: str) -> str:
    subject = _one_line(original_subject, 80)
    if not subject:
        return "Re: Your service request"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build_ack_message(
    config: dict,
    recipient_email: str,
    recipient_name: str,
    in_office_hours: bool,
    ticket_id: str = "",
    original: Message | None = None,
) -> EmailMessage:
    """Build the lead acknowledgment (in-hours / after-hours wording).

    WHY fixed wording (AUDIT H9): the body used to repeat the sender's issue
    text, address and a Maps link built from it. Combined with a forged From:
    address, that let anyone make this mailbox send their text to a third
    party. The only variable parts now are a letters-only greeting name, the
    ticket number, and the subject of the email we answer (so the reply
    threads in the customer's mail app). Staff see full details in the ticket
    and the draft reply. With require_sender_auth on, the subject can only
    come from the genuine owner of the address we reply to.
    """
    business_name = config["business_name"]
    office_hours = config["office_hours"]
    phone_number = config["phone_number"]

    reply = EmailMessage()
    reply["Subject"] = (
        _reply_subject(clean_subject(_safe_header(original, "Subject")))
        if original is not None
        else f"Thanks for contacting {business_name}"
    )
    reply["From"] = config["email_address"]
    reply["To"] = recipient_email
    if original is not None:
        # Threading headers (AUDIT M11) so the reply lands in the same conversation.
        original_id = _one_line(_safe_header(original, "Message-ID"))
        if original_id:
            reply["In-Reply-To"] = original_id
            references = _one_line(_safe_header(original, "References"))
            reply["References"] = f"{references} {original_id}".strip()
    mark_automatic(reply, "ack")

    if in_office_hours:
        timing_line = f"We've received your message and a member of our team will review it during business hours ({office_hours})."
    else:
        timing_line = (
            "We've received your message and our team will respond during the next business day "
            f"({office_hours})."
        )
    reference_line = f"Your reference number is {ticket_id}.\n\n" if ticket_id else ""
    body = (
        f"Hi {recipient_name or 'there'},\n\n"
        "Thank you for reaching out regarding your HVAC inquiry.\n\n"
        f"{reference_line}"
        f"{timing_line}\n\n"
        f"If this is urgent, please call us directly at {phone_number}.\n\n"
        "We look forward to assisting you.\n\n"
        f"Best regards,\n{business_name}\n"
    )
    reply.set_content(body)
    return reply


def build_follow_up_message(config: dict, recipient_email: str, recipient_name: str) -> EmailMessage:
    """Build follow-up reminder message for pending leads."""
    business_name = config["business_name"]
    office_hours = config["office_hours"]
    phone_number = config["phone_number"]

    reply = EmailMessage()
    reply["Subject"] = "Re: Following up on your HVAC inquiry"
    reply["From"] = config["email_address"]
    reply["To"] = recipient_email
    mark_automatic(reply, "follow_up")

    body = (
        f"Hi {safe_recipient_name(recipient_name)},\n\n"
        "Just following up on your HVAC inquiry.\n\n"
        "If you still need help, you can reply to this email or call us directly.\n"
        f"Our office hours are {office_hours}.\n\n"
        f"Phone: {phone_number}\n\n"
        f"Best regards,\n{business_name}\n"
    )
    reply.set_content(body)
    return reply


def message_datetime_utc(msg: Message) -> datetime | None:
    """Read and normalize message Date header to UTC."""
    raw_date = _safe_header(msg, "Date")
    if not raw_date:
        return None
    try:
        sent_dt = parsedate_to_datetime(raw_date)
    except Exception:
        return None
    if sent_dt.tzinfo is None:
        sent_dt = sent_dt.replace(tzinfo=timezone.utc)
    return sent_dt.astimezone(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse ISO timestamp and normalize to UTC."""
    return store.parse_iso(value)


# ---------------------------------------------------------------------------
# Inbox cycle
# ---------------------------------------------------------------------------

SENT_MAILBOX_FALLBACKS = ["Sent", "Sent Items", "INBOX.Sent", "[Gmail]/Sent Mail"]
MAX_MESSAGE_ATTEMPTS = 3          # a message that keeps failing is left unread for staff
FOLLOW_UP_GRACE_HOURS = 48        # never send a follow-up this late (agent was offline)
AUTO_EMAIL_KINDS = ("ack", "follow_up")


def resolve_dry_run(config: dict, dry_run_override: bool | None) -> bool:
    return bool(config.get("dry_run", False)) if dry_run_override is None else bool(dry_run_override)


def lock_path_for(config: dict) -> Path:
    return Path(str(config.get("db_path", "data/hvac_agent.db"))).parent / "agent.lock"


def csv_export_paths(config: dict) -> dict[str, Path]:
    keys = ["leads_csv_file", "service_requests_file", "draft_replies_file", "job_tickets_file"]
    return {key: Path(str(config[key])) for key in keys if str(config.get(key, "")).strip()}


def default_mailbox_factory(config: dict, password: str) -> ImapMailbox:
    return ImapMailbox(
        config["imap_server"], int(config["imap_port"]), config["email_address"], password,
        timeout=float(config.get("network_timeout_seconds", 30)),
    )


def default_smtp_factory(config: dict, password: str) -> SmtpSender:
    return SmtpSender(
        config["smtp_server"], int(config["smtp_port"]), config["email_address"], password,
        timeout=float(config.get("network_timeout_seconds", 30)),
    )


@dataclass
class CycleStats:
    dry_run: bool = False
    inspected: int = 0
    processed: int = 0
    skipped_known: int = 0
    ignored_own: int = 0
    failed: int = 0
    auto_replies: int = 0
    follow_ups: int = 0
    dispatch_items: int = 0
    by_class: dict = field(default_factory=dict)


class Outbox:
    """Sends mail and records the outcome in SQLite.

    The caller writes the 'pending' outbound row in the same transaction as
    the decision to send, so a crash can never send without a record
    (at-most-once delivery). In dry-run nothing leaves the machine.
    """

    def __init__(self, conn, smtp: SmtpSender | None, dry_run: bool):
        self.conn = conn
        self.smtp = smtp
        self.dry_run = dry_run

    def ensure_ready(self) -> None:
        """Connect + log in BEFORE we commit an intent to send (raises MailUnavailable)."""
        if not self.dry_run:
            self.smtp.connect()

    def deliver(self, outbound_id: int, message: EmailMessage, now: datetime) -> bool:
        if self.dry_run:
            print(f"[DRY RUN] Would send {message.get('X-HVAC-Auto-Type', 'email')} to {message['To']} | {message['Subject']}")
            with store.transaction(self.conn):
                store.set_outbound_status(self.conn, outbound_id, "dry_run", now)
            return True
        try:
            self.smtp.send(message)
        except (SendFailed, MailUnavailable) as err:
            # Never retried automatically: if ambiguous it may have arrived,
            # and a duplicate is worse than one missed acknowledgment.
            with store.transaction(self.conn):
                store.set_outbound_status(self.conn, outbound_id, "failed", now, str(err)[:500])
            print(f"Send failed to {message['To']}: {err}")
            return False
        with store.transaction(self.conn):
            store.set_outbound_status(self.conn, outbound_id, "sent", now)
        return True


@dataclass
class CycleContext:
    config: dict
    conn: object
    mbox: object
    outbox: Outbox
    dry_run: bool
    mailbox_name: str
    uidvalidity: str
    own_addresses: set
    stats: CycleStats
    now: object  # callable returning an aware UTC datetime

    def finish(self, uid: str) -> None:
        """Mark a fully handled message as read (old behaviour), never in dry-run.

        Done only AFTER the database commit (AUDIT C1). A failure here is
        harmless: the database, not the \\Seen flag, is what prevents
        re-processing.
        """
        if self.dry_run or not self.config.get("mark_processed_as_read", True):
            return
        try:
            self.mbox.mark_seen(uid)
        except Exception as err:
            append_error_log(self.config, f"mark_seen uid={uid}", err)


def analyze_message(msg: Message, config: dict) -> dict:
    """Pure classification/extraction step (no I/O)."""
    subject = clean_subject(_safe_header(msg, "Subject"))
    body = extract_plain_text(msg)
    sender_name, sender_email = parseaddr(_safe_header(msg, "From"))
    sender_name, sender_email = sender_name.strip(), sender_email.strip().lower()

    result = classify(subject, body, config)
    service = detect_service(subject, body, config)
    hvac_related = is_hvac_related(subject, body, service)
    structured_fields = has_structured_fields(body)
    marketing = is_marketing_email(sender_email, subject, body, msg)
    customer_intent = has_customer_intent(subject, body, structured_fields)

    classification = result.classification
    if marketing and not customer_intent:
        classification = "spam"
    elif not hvac_related:
        classification = "spam" if classification == "spam" else "existing"
    elif classification not in ("lead", "spam") and structured_fields and not marketing:
        classification = "lead"
    elif classification == "unclear":
        # HVAC-related but no clear signal: ticket for a human, no auto-reply.
        classification = "review"
    if classification == "unclear":
        classification = "existing"

    return {
        "subject": subject,
        "body": body,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "result": result,
        "classification": classification,
        "service": service,
        "hvac_related": hvac_related,
        "structured_fields": structured_fields,
        "marketing": marketing,
        "customer_intent": customer_intent,
    }


def decide_auto_reply(ctx: CycleContext, analysis: dict, block_reason: str | None, received_at: datetime | None, now: datetime) -> tuple[bool, str]:
    """Return (send?, reason-if-not). Every rule here can only PREVENT a reply."""
    config = ctx.config
    if not config.get("auto_reply_enabled", False):
        return False, "auto-reply disabled"
    if analysis["classification"] != "lead":
        return False, f"classified as {analysis['classification']}"
    if block_reason:
        return False, block_reason
    max_age = float(config.get("max_reply_age_hours", 48))
    reference_time = received_at or analysis.get("date_header")
    if max_age > 0 and reference_time and now - reference_time > timedelta(hours=max_age):
        return False, f"message older than {max_age:g}h"
    if store.in_cooldown(ctx.conn, analysis["sender_email"], now, float(config.get("auto_reply_cooldown_hours", 24))):
        return False, "sender cooldown active"
    cap = int(config.get("max_auto_emails_per_day", 50))
    if store.count_outbound_since(ctx.conn, AUTO_EMAIL_KINDS, now - timedelta(hours=24)) >= cap:
        return False, f"daily auto-email cap ({cap}) reached"
    return True, ""


def record_message(ctx: CycleContext, key: str, analysis: dict, now: datetime) -> dict:
    """Write all database effects of one inbound email (caller holds the transaction)."""
    conn, config = ctx.conn, ctx.config
    classification = analysis["classification"]
    sender = analysis["sender_email"]
    store.insert_lead_log(
        conn,
        {
            "message_key": key,
            "timestamp": now.astimezone().isoformat(),
            "sender_email": sender,
            "detected_name": analysis["sender_name"],
            "service_detected": analysis["service"],
            "subject": analysis["subject"],
            "classification": classification,
        },
    )
    outcome = {"request": None, "ticket_id": "", "job_id": None, "job_action": ""}
    if classification not in ("lead", "review"):
        return outcome

    request = extract_service_request(
        analysis["subject"], analysis["body"], analysis["sender_name"], sender, analysis["service"], config
    )
    request["timestamp"] = now.astimezone().isoformat()
    outcome["request"] = request

    if config.get("feature_ticketing", True):
        store.insert_service_request(
            conn, {"message_key": key, **{column: request.get(column, "") for column in store.SERVICE_REQUEST_COLUMNS}}
        )
        existing_customer = conn.execute("SELECT * FROM customers WHERE email = ?", (sender,)).fetchone() if sender else None
        incoming_name = normalize_text(f"{request.get('first_name', '')} {request.get('last_name', '')}")
        existing_name = ""
        if existing_customer:
            existing_name = normalize_text(f"{existing_customer.get('first_name') or ''} {existing_customer.get('last_name') or ''}")
        # Same address but clearly a different person (shared family email): don't merge their jobs.
        name_matches = not (existing_name and incoming_name and existing_name != incoming_name)

        customer_id = store.upsert_customer(
            conn, request.get("first_name", ""), request.get("last_name", ""), sender,
            request.get("phone", ""), request.get("address", ""),
        )
        duplicate = (
            store.find_duplicate_job(
                conn, customer_id, request.get("issue", ""), request.get("address", ""), request.get("service_detected", "")
            )
            if name_matches
            else None
        )
        if duplicate:
            outcome["job_action"] = store.apply_duplicate(conn, duplicate, request, config["ticket_duplicate_mode"], now)
            outcome["job_id"] = int(duplicate["job_id"])
            ticket_id = duplicate.get("ticket_id") or ""
            if not ticket_id:  # job created before tickets moved to SQLite
                created = store.parse_iso(duplicate.get("created_at")) or now
                ticket_id = store.make_ticket_id(int(duplicate["job_id"]), created)
                conn.execute("UPDATE jobs SET ticket_id = ? WHERE job_id = ?", (ticket_id, duplicate["job_id"]))
            outcome["ticket_id"] = ticket_id
        else:
            job_id, ticket_id = store.create_job(conn, customer_id, request, key, now, needs_review=classification == "review")
            outcome.update(job_id=job_id, ticket_id=ticket_id, job_action="created")
        request["ticket_id"] = outcome["ticket_id"]

    in_hours = is_within_office_hours(config, now)
    draft_subject, draft_body = generate_draft_reply(config, request, in_hours)
    store.insert_draft_reply(
        conn,
        {
            "message_key": key,
            "timestamp": request["timestamp"],
            "sender_email": sender,
            "draft_subject": draft_subject,
            "draft_body": draft_body,
            "customer_name": request.get("customer_name", ""),
            "phone": request.get("phone", ""),
            "address": request.get("address", ""),
            "maps_link": request.get("maps_link", ""),
            "service_detected": request.get("service_detected", ""),
            "priority": request.get("priority", ""),
        },
    )

    if config.get("dispatch_summary_enabled", False):
        if not config.get("feature_ticketing", True) or outcome["job_action"] in {"created", "replaced"}:
            store.enqueue_dispatch(
                conn,
                {
                    "timestamp": request["timestamp"],
                    "issue": request.get("issue", "HVAC request"),
                    "address": request.get("address", "Unknown"),
                    "priority": request.get("priority", "Normal"),
                    "request_type": request.get("request_type", "service"),
                    "service_detected": request.get("service_detected", "Unknown"),
                    "ticket_id": outcome["ticket_id"],
                    "maps_link": request.get("maps_link", ""),
                    "review": classification == "review",
                },
                now,
            )
    return outcome


def _sms_planned(ctx: CycleContext, analysis: dict, now: datetime) -> str:
    """Return the gateway address if an SMS alert should go out for this email."""
    config = ctx.config
    if analysis["classification"] not in ("lead", "review") or not config.get("sms_enabled", False):
        return ""
    gateway = str(config.get("sms_gateway_email", "")).strip()
    if not gateway:
        return ""
    cap = int(config.get("max_sms_per_day", 30))
    if store.count_outbound_since(ctx.conn, ("sms",), now - timedelta(hours=24)) >= cap:
        log_event(config, "sms_skipped", reason=f"daily SMS cap ({cap}) reached")
        return ""
    return gateway


def process_message(ctx: CycleContext, uid: str) -> None:
    """Handle one inbound email end to end. Never lets one bad email stop the cycle (AUDIT H6).

    Ordering is what makes this crash-safe (AUDIT C1):
      1. decide everything in memory;
      2. if we will send, make sure SMTP is connected (else abort: nothing committed);
      3. ONE transaction writes every database effect plus the 'pending' send
         record and the cooldown;
      4. send; 5. mark done; 6. mark the email read.
    A crash after step 3 is detected next run ('processing') and never resent.
    """
    conn, config, stats = ctx.conn, ctx.config, ctx.stats
    stats.inspected += 1
    known = store.find_processed_by_uid(conn, ctx.mailbox_name, ctx.uidvalidity, uid)
    if known and known["status"] == "done":
        stats.skipped_known += 1
        return

    raw, received_at = ctx.mbox.fetch(uid)
    now = ctx.now()
    msg = message_from_bytes(raw, policy=default)
    message_id = _one_line(_safe_header(msg, "Message-ID"), 500)
    # WHY UID+UIDVALIDITY fallback (AUDIT M5): the old fallback was the IMAP
    # sequence number, which is reused, so later messages were silently skipped.
    key = message_id or f"uid:{ctx.mailbox_name}:{ctx.uidvalidity}:{uid}"
    ids = {"message_id": message_id, "mailbox": ctx.mailbox_name, "uidvalidity": ctx.uidvalidity, "uid": uid}

    existing = store.get_processed(conn, key)
    if existing:
        if existing["status"] == "done":
            with store.transaction(conn):
                store.upsert_processed(conn, key, status="done", now=now, **ids)
            stats.skipped_known += 1
            ctx.finish(uid)
            return
        if existing["status"] == "processing":
            store.recover_interrupted_message(conn, key, now)
            log_event(config, "recovered_interrupted", message_key=key)
            print(f"Recovered interrupted message {key}: reply outcome unknown, NOT resent.")
            ctx.finish(uid)
            return
        if existing["status"] == "failed" and int(existing.get("attempts") or 0) >= MAX_MESSAGE_ATTEMPTS:
            stats.skipped_known += 1
            return

    try:
        analysis = analyze_message(msg, config)
        analysis["date_header"] = message_datetime_utc(msg)
        sender = analysis["sender_email"]
        base = {**ids, "sender_email": sender, "subject": analysis["subject"][:300]}

        if safety.is_own_message(msg, sender, ctx.own_addresses):
            # AUDIT C4: our own dispatch summaries/aliases must never become tickets.
            with store.transaction(conn):
                store.upsert_processed(conn, key, status="done", now=now, classification="own", note="own/agent mail ignored", **base)
            stats.ignored_own += 1
            ctx.finish(uid)
            return

        block_reason = safety.reply_block_reason(
            msg, sender, ctx.own_addresses,
            require_auth=bool(config.get("require_sender_auth", True)),
            trusted_authserv_ids=list(config.get("trusted_authserv_ids", [])),
        )
        send_ack, skip_reason = decide_auto_reply(ctx, analysis, block_reason, received_at, now)
        sms_gateway = _sms_planned(ctx, analysis, now)
        if send_ack:
            ctx.outbox.ensure_ready()  # MailUnavailable propagates: nothing committed yet

        classification = analysis["classification"]
        reference_time = received_at or analysis["date_header"]
        max_age = float(config.get("max_reply_age_hours", 48))
        is_fresh = not (max_age > 0 and reference_time and now - reference_time > timedelta(hours=max_age))

        with store.transaction(conn):
            outcome = record_message(ctx, key, analysis, now)
            request = outcome["request"]
            name = greeting_name(request, analysis["sender_name"])
            if (
                classification == "lead"
                and config.get("follow_up_enabled", False)
                and block_reason is None
                and is_fresh
            ):
                store.add_follow_up(
                    conn, key, sender, name, analysis["subject"], outcome["job_id"], now,
                    float(config.get("follow_up_delay_hours", 24)),
                )
            ack_id = sms_id = None
            if send_ack:
                store.set_cooldown(conn, sender, now)
                ack_id = store.add_outbound(conn, "ack", sender, key, now)
            if sms_gateway:
                sms_id = store.add_outbound(conn, "sms", sms_gateway, key, now)
            store.upsert_processed(
                conn, key, status="processing" if (ack_id or sms_id) else "done", now=now,
                classification=classification, attempts_increment=1, error="", **base,
            )

        stats.processed += 1
        stats.by_class[classification] = stats.by_class.get(classification, 0) + 1
        log_event(
            config, "email_processed", sender_email=sender, subject=analysis["subject"], classification=classification,
            ticket_id=outcome["ticket_id"], job_action=outcome["job_action"],
            auto_reply="planned" if send_ack else f"skipped: {skip_reason}",
        )

        if ack_id:
            ack = build_ack_message(
                config, sender, name, is_within_office_hours(config, now), outcome["ticket_id"], original=msg
            )
            if ctx.outbox.deliver(ack_id, ack, now):
                stats.auto_replies += 1
                log_event(config, "auto_reply_sent", sender_email=sender, dry_run=str(ctx.dry_run))
        if sms_id:
            sms = EmailMessage()
            sms["Subject"] = ""
            sms["From"] = config["email_address"]
            sms["To"] = sms_gateway
            mark_automatic(sms, "sms")
            sms.set_content(
                ("REVIEW " if classification == "review" else "") + "NEW HVAC REQUEST\n"
                f"Ticket: {outcome['ticket_id']}\n"
                f"Priority: {(request or {}).get('priority', '')}\n"
                f"Service: {(request or {}).get('service_detected', '')}"
            )
            ctx.outbox.deliver(sms_id, sms, now)
        if ack_id or sms_id:
            with store.transaction(conn):
                store.upsert_processed(conn, key, status="done", now=now, **ids)
        print(
            f"Processed {sender or '<unknown>'} | class={classification} "
            f"(lead={analysis['result'].lead_score}, existing={analysis['result'].existing_score}, "
            f"spam={analysis['result'].spam_score}) | reply={'yes' if send_ack else 'no: ' + skip_reason}"
        )
        ctx.finish(uid)
    except MailUnavailable:
        raise
    except Exception as err:
        append_error_log(config, f"message failed (key={key})", err)
        with store.transaction(conn):
            store.upsert_processed(
                conn, key, status="failed", now=now, error=f"{type(err).__name__}: {err}"[:500],
                attempts_increment=1, **ids,
            )
        stats.failed += 1
        print(f"Error processing message {key}; details in {config.get('error_log_file')}. Continuing with the next one.")


def process_follow_ups(ctx: CycleContext) -> int:
    """Send due follow-ups, with the same safety rules as acknowledgments (AUDIT H2).

    A follow-up only exists for a lead that passed the reply-safety checks
    when it arrived, while follow-ups were enabled. Before sending we cancel it
    if staff replied manually (Sent folder), the job moved past 'new', or it
    is more than 48h overdue (agent was offline: no burst of stale emails).
    If the Sent folder can't be read we hold everything rather than guess.
    """
    conn, config = ctx.conn, ctx.config
    now = ctx.now()
    with store.transaction(conn):
        store.recover_interrupted_follow_ups(conn, now)
        if not config.get("follow_up_enabled", False):
            # Turning the feature off cancels the queue, so turning it back on
            # later can never mail old leads in one burst.
            store.cancel_all_pending_follow_ups(conn, now, "follow-ups disabled")
            return 0
    due = store.due_follow_ups(conn, now)
    if not due:
        return 0

    earliest = min(store.parse_iso(row["lead_at"]) or now for row in due)
    candidates = [str(config.get("sent_mailbox", ""))] + SENT_MAILBOX_FALLBACKS
    try:
        manual = ctx.mbox.sent_recipients_since(candidates, earliest, safety.own_raw_addresses(config))
    except Exception as err:
        append_error_log(config, "reading Sent folder for follow-ups", err)
        manual = None
    if manual is None:
        print("Follow-ups paused: could not read the Sent folder to check for manual replies.")
        log_event(config, "follow_ups_paused", reason="sent folder unavailable")
        return 0

    sent = 0
    cap = int(config.get("max_auto_emails_per_day", 50))
    for row in due:
        key, sender = row["message_key"], row["sender_email"]
        lead_at = store.parse_iso(row["lead_at"]) or now
        due_at = store.parse_iso(row["due_at"]) or now

        def cancel(reason: str) -> None:
            with store.transaction(conn):
                store.set_follow_up_status(conn, key, "cancelled", now, reason)

        manual_at = manual.get(sender)
        if manual_at and manual_at >= lead_at:
            cancel("manual reply detected")
            continue
        if row.get("job_id") and config.get("feature_ticketing", True):
            job = store.get_job(conn, int(row["job_id"]))
            if job is None or str(job.get("status", "")).lower() != "new":
                cancel("job already being handled")
                continue
        if now - due_at > timedelta(hours=FOLLOW_UP_GRACE_HOURS):
            cancel(f"more than {FOLLOW_UP_GRACE_HOURS}h overdue")
            continue
        if store.in_cooldown(conn, sender, now, float(config.get("auto_reply_cooldown_hours", 24))):
            continue  # stays pending; retried next cycle
        if store.count_outbound_since(conn, AUTO_EMAIL_KINDS, now - timedelta(hours=24)) >= cap:
            print(f"Follow-ups held: daily auto-email cap ({cap}) reached.")
            break
        try:
            ctx.outbox.ensure_ready()
        except MailUnavailable as err:
            print(f"Follow-ups held: {err}")
            break
        with store.transaction(conn):
            outbound_id = store.add_outbound(conn, "follow_up", sender, key, now)
            store.set_cooldown(conn, sender, now)
            store.set_follow_up_status(conn, key, "sending", now, outbound_id=outbound_id)
        delivered = ctx.outbox.deliver(outbound_id, build_follow_up_message(config, sender, row.get("recipient_name") or ""), now)
        with store.transaction(conn):
            store.set_follow_up_status(conn, key, "sent" if delivered else "cancelled", now, "" if delivered else "send failed")
            if delivered and row.get("job_id"):
                store.mark_job_followup_sent(conn, int(row["job_id"]), now)
        if delivered:
            sent += 1
            log_event(config, "follow_up_sent", sender_email=sender, dry_run=str(ctx.dry_run))
    return sent


def process_dispatch_summary(ctx: CycleContext) -> int:
    """Send the periodic dispatch summary to staff.

    Unlike customer email, an internal summary that might be duplicated after
    a crash is retried rather than dropped: staff missing a job is worse.
    """
    conn, config = ctx.conn, ctx.config
    if not config.get("dispatch_summary_enabled", False):
        return 0
    now = ctx.now()
    with store.transaction(conn):
        store.release_stale_dispatch_claims(conn, now)
    last = store.parse_iso(store.get_meta(conn, "dispatch_last_summary_at"))
    interval = timedelta(minutes=int(config.get("dispatch_summary_interval_minutes", 60)))
    if last and now < last + interval:
        return 0
    if not conn.execute("SELECT 1 FROM dispatch_queue WHERE status = 'pending' LIMIT 1").fetchone():
        return 0
    recipient = str(config.get("dispatch_summary_recipient") or config.get("email_address", "")).strip()
    if not recipient:
        return 0
    try:
        ctx.outbox.ensure_ready()
    except MailUnavailable as err:
        print(f"Dispatch summary held: {err}")
        return 0

    with store.transaction(conn):
        rows = store.claim_pending_dispatch(conn, now)
        outbound_id = store.add_outbound(conn, "dispatch", recipient, "", now)
    items = [json.loads(row["payload"]) for row in rows]
    body = build_dispatch_summary_body(items)
    message = EmailMessage()
    message["Subject"] = f"Dispatch Summary - {now.astimezone().strftime('%Y-%m-%d %H:%M %z')}"
    message["From"] = config["email_address"]
    message["To"] = recipient
    mark_automatic(message, "dispatch")
    message.set_content(body)

    delivered = ctx.outbox.deliver(outbound_id, message, now)
    with store.transaction(conn):
        store.finish_dispatch(conn, [row["id"] for row in rows], now, sent=delivered)
        if delivered:
            store.set_meta(conn, "dispatch_last_summary_at", store.iso(now))
    if delivered and not ctx.dry_run:
        append_dispatch_log(Path(str(config.get("dispatch_summary_log_file", "logs/dispatch_summary.log"))), body)
    return len(items) if delivered else 0


def process_inbox(
    config: dict,
    max_emails: int | None = None,
    dry_run_override: bool | None = None,
    *,
    mailbox_factory=None,
    smtp_factory=None,
    now_fn=None,
) -> CycleStats:
    """Run one full cycle: new mail, follow-ups, dispatch summary, CSV export.

    mailbox_factory / smtp_factory / now_fn exist so tests can run the real
    logic against fake mail servers and a fake clock (no network).
    """
    dry_run = resolve_dry_run(config, dry_run_override)
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    db_path = Path(str(config["db_path"]))
    conn = store.open_dry_run_copy(db_path) if dry_run else store.open_state_db(db_path)
    smtp = None
    try:
        if dry_run:
            if not store.get_meta(conn, legacy_import.META_KEY):
                print("[DRY RUN] Older state files have not been imported yet; the first live run will import them.")
        else:
            backup = legacy_import.import_legacy_state_once(conn, config, Path("backups"), now_fn())
            if backup:
                print(f"Imported older state files into the database (backup: {backup}).")
                log_event(config, "legacy_state_imported", backup=backup)

        password = credentials.resolve_mail_password(config)
        smtp = None if dry_run else (smtp_factory or default_smtp_factory)(config, password)
        outbox = Outbox(conn, smtp, dry_run)
        stats = CycleStats(dry_run=dry_run)
        # AUDIT M4: the configured mailbox is honoured (ignore_promotions used to
        # force INBOX, and its Gmail-category filter never worked).
        mailbox_name = str(config.get("mailbox") or "INBOX")

        with (mailbox_factory or default_mailbox_factory)(config, password) as mbox:
            # readonly in dry-run: the server will not let us change flags.
            uidvalidity = mbox.select(mailbox_name, readonly=dry_run)
            uids = mbox.search_uids(unseen_only=bool(config.get("read_unseen_only", True)))
            if max_emails is not None:
                uids = uids[: max(0, max_emails)]
            print(f"Found {len(uids)} email(s) to inspect.")
            ctx = CycleContext(
                config=config, conn=conn, mbox=mbox, outbox=outbox, dry_run=dry_run,
                mailbox_name=mailbox_name, uidvalidity=uidvalidity,
                own_addresses=safety.own_address_set(config), stats=stats, now=now_fn,
            )
            for uid in uids:
                process_message(ctx, uid)
            stats.follow_ups = process_follow_ups(ctx)
            stats.dispatch_items = process_dispatch_summary(ctx)

        if not dry_run:
            for failure in store.export_csvs(conn, csv_export_paths(config)):
                print(f"WARNING: CSV export failed ({failure}). Close the file if it is open in Excel.")
                log_event(config, "csv_export_failed", detail=failure)

        print(
            f"Done. New: {stats.processed}, Already handled: {stats.skipped_known}, Own mail ignored: {stats.ignored_own}, "
            f"Failed: {stats.failed}, Auto-replies: {stats.auto_replies}, Follow-ups: {stats.follow_ups}, "
            f"Dispatch-items: {stats.dispatch_items}, Dry-run: {dry_run}"
        )
        return stats
    finally:
        if smtp is not None:
            smtp.close()
        conn.close()


def calculate_dashboard_metrics(config: dict) -> dict[str, int]:
    """Dashboard counters, read from SQLite (the single source of truth, AUDIT M1).

    Uses a read-only connection so the 5-second UI refresh never writes to the
    database or competes with the agent for the write lock (AUDIT L5).
    """
    metrics = {
        "emails_processed_today": 0,
        "service_requests_today": 0,
        "quotes_today": 0,
        "spam_today": 0,
        "open_tickets": 0,
        "high_priority_open_tickets": 0,
        "jobs_completed_today": 0,
        "leads_this_week": 0,
        "avg_response_minutes": 0,
    }
    conn = store.open_readonly(Path(str(config.get("db_path", "data/hvac_agent.db"))))
    if conn is None:
        return metrics
    # Everything is compared in the office's local calendar (AUDIT L6).
    local_now = system_now()
    today = local_now.date()
    week_start = today - timedelta(days=today.weekday())

    def local_date(value: object):
        parsed = parse_iso_datetime(str(value or ""))
        return parsed.astimezone().date() if parsed else None

    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "lead_log" in tables:
            for row in conn.execute("SELECT timestamp, classification FROM lead_log"):
                if local_date(row["timestamp"]) == today:
                    metrics["emails_processed_today"] += 1
                    if str(row["classification"]).lower() == "spam":
                        metrics["spam_today"] += 1
        if "service_requests" in tables:
            for row in conn.execute("SELECT timestamp, request_type FROM service_requests"):
                if local_date(row["timestamp"]) == today:
                    key = "quotes_today" if str(row["request_type"]).lower() == "quote" else "service_requests_today"
                    metrics[key] += 1
        if "jobs" in tables:
            response_minutes: list[int] = []
            for row in conn.execute("SELECT created_at, scheduled_time, status, priority, last_updated_at FROM jobs"):
                created = parse_iso_datetime(row.get("created_at") or "")
                status = str(row.get("status") or "").lower()
                if status in OPEN_STATUSES:
                    metrics["open_tickets"] += 1
                    if str(row.get("priority") or "").lower() == "high":
                        metrics["high_priority_open_tickets"] += 1
                if status == "completed" and local_date(row.get("last_updated_at")) == today:
                    metrics["jobs_completed_today"] += 1
                if created and created.astimezone().date() >= week_start:
                    metrics["leads_this_week"] += 1
                scheduled = parse_iso_datetime(row.get("scheduled_time") or "")
                if created and scheduled:
                    response_minutes.append(int((scheduled - created).total_seconds() // 60))
            if response_minutes:
                metrics["avg_response_minutes"] = int(sum(response_minutes) / len(response_minutes))
    finally:
        conn.close()
    return metrics


OPEN_STATUSES = {"new", "scheduled", "in progress"}


def get_recent_open_tickets(config: dict, limit: int = 8) -> list[str]:
    """Return recent open ticket summaries for dashboard display."""
    db_path = Path(str(config.get("db_path", "data/hvac_agent.db")))
    if db_path.exists():
        conn = database.get_connection(db_path)
        try:
            rows = conn.execute(
                """
                SELECT j.job_id, j.issue_description, j.priority, j.address, j.created_at
                FROM jobs j
                WHERE j.status IN ('new', 'scheduled', 'in progress')
                ORDER BY j.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [
            f"JOB-{row.get('job_id')} | [{row.get('priority','Normal')}] {row.get('issue_description','')} | {row.get('address','')}"
            for row in rows
        ]

    tickets_path = Path(str(config.get("job_tickets_file", "job_tickets.csv")))
    if not tickets_path.exists() or tickets_path.stat().st_size == 0:
        return []

    rows: list[dict] = []
    with tickets_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if str(row.get("status", "")).strip().lower() == "open":
                rows.append(row)

    rows.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    results: list[str] = []
    for row in rows[:limit]:
        ticket_id = str(row.get("ticket_id", "")).strip() or "TKT-UNKNOWN"
        issue = str(row.get("issue", "")).strip() or "HVAC request"
        priority = str(row.get("priority", "")).strip() or "Normal"
        address = str(row.get("address", "")).strip() or "Unknown"
        results.append(f"{ticket_id} | [{priority}] {issue} | {address}")
    return results


def run_ui(config: dict, dry_run_override: bool | None = None) -> None:
    """Run a minimal local Tkinter dashboard with start/stop controls."""
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog, ttk
    except Exception as err:
        raise RuntimeError("Tkinter is unavailable in this Python environment.") from err

    dry_run = config.get("dry_run", True) if dry_run_override is None else dry_run_override
    poll_interval = max(5, int(config.get("poll_interval_seconds", 30)))
    refresh_seconds = max(1, int(config.get("ui_refresh_seconds", 5)))

    bg_color = "#0A0F14"
    panel_color = "#101820"
    accent_color = "#1F6FEB"
    text_color = "#D7E0E7"
    muted_color = "#9FB0BF"

    root = tk.Tk()
    root.title("HVAC Email Agent")
    root.geometry("1120x760")
    root.configure(bg=bg_color, highlightthickness=0, bd=0)
    root.option_add("*Font", ("Segoe UI", 10))

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=bg_color)
    style.configure("TLabel", background=bg_color, foreground=text_color, font=("Segoe UI", 10))
    style.configure("Title.TLabel", background=bg_color, foreground=text_color, font=("Segoe UI", 18, "bold"))
    style.configure("Subtitle.TLabel", background=bg_color, foreground=muted_color, font=("Segoe UI", 10, "italic"))
    style.configure("TButton", padding=6)
    style.map("TButton", background=[("active", accent_color)])
    style.configure("Card.TFrame", background=panel_color)
    style.configure("Card.TLabel", background=panel_color, foreground=text_color, font=("Segoe UI", 9))
    style.configure("Treeview", background=panel_color, fieldbackground=panel_color, foreground=text_color)
    style.configure("Treeview.Heading", background=panel_color, foreground=text_color)
    style.map("Treeview", background=[("selected", accent_color)])
    style.configure("TNotebook", background=bg_color, borderwidth=0, relief="flat")
    style.configure(
        "TNotebook.Tab",
        background=panel_color,
        foreground=text_color,
        padding=[14, 8],
        borderwidth=0,
        relief="flat",
    )
    style.map("TNotebook.Tab", background=[("selected", accent_color)])

    db_path = Path(str(config.get("db_path", "data/hvac_agent.db")))
    # Create/upgrade the schema once at startup (adds the Phase 1 tables).
    store.open_state_db(db_path).close()
    agent_lock = InstanceLock(lock_path_for(config))

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    overview_tab = tk.Frame(notebook, bg=bg_color)
    tickets_tab = tk.Frame(notebook, bg=bg_color)
    history_tab = tk.Frame(notebook, bg=bg_color)

    notebook.add(overview_tab, text="Overview")
    notebook.add(tickets_tab, text="Tickets")
    notebook.add(history_tab, text="History")

    status_label_var = tk.StringVar(value="Status: Stopped")
    processed_var = tk.StringVar(value="Emails processed today: 0")
    service_var = tk.StringVar(value="Service requests: 0")
    quote_var = tk.StringVar(value="Quotes: 0")
    spam_var = tk.StringVar(value="Spam: 0")
    open_tickets_var = tk.StringVar(value="Open tickets: 0")
    high_priority_var = tk.StringVar(value="High-priority open tickets: 0")
    completed_today_var = tk.StringVar(value="Jobs completed today: 0")
    leads_week_var = tk.StringVar(value="Leads this week: 0")
    avg_response_var = tk.StringVar(value="Avg response time (min): 0")
    last_checked_var = tk.StringVar(value="Last checked email: --")
    license_label_var = tk.StringVar(
        value=f"License: {config.get('license_key', '')} | {config.get('email_address', '')}"
    )
    effective_auto_reply = bool(config.get("feature_auto_reply", False)) and bool(
        config.get("auto_reply_enabled", False)
    )
    feature_auto_reply = tk.BooleanVar(value=effective_auto_reply)
    feature_ticketing = tk.BooleanVar(value=bool(config.get("feature_ticketing", True)))
    feature_dispatch = tk.BooleanVar(value=bool(config.get("feature_dispatch_summary", True)))
    effective_sms = bool(config.get("feature_sms_notifications", False)) and bool(config.get("sms_enabled", False))
    feature_sms = tk.BooleanVar(value=effective_sms)
    effective_followup = bool(config.get("feature_follow_up", False)) and bool(config.get("follow_up_enabled", False))
    feature_followup = tk.BooleanVar(value=effective_followup)
    ticket_sort_var = tk.StringVar(value="Newest")
    ticket_signature_cache: dict[str, str] = {"value": ""}

    tickets_container: tk.Frame | None = None
    tickets_canvas: tk.Canvas | None = None
    history_tree: ttk.Treeview | None = None
    jobs_tree: ttk.Treeview | None = None
    workload_list: tk.Listbox | None = None
    ticket_list: tk.Listbox | None = None
    new_jobs_list: tk.Listbox | None = None
    in_progress_list: tk.Listbox | None = None
    completed_today_list: tk.Listbox | None = None
    activity_list: tk.Listbox | None = None

    stop_event = threading.Event()
    worker_ref: dict[str, threading.Thread | None] = {"thread": None}
    runtime_state: dict[str, str] = {"status": "Stopped", "last_checked": "--"}

    def update_metrics() -> None:
        metrics = calculate_dashboard_metrics(config)
        processed_var.set(f"Emails processed today: {metrics['emails_processed_today']}")
        service_var.set(f"Service requests: {metrics['service_requests_today']}")
        quote_var.set(f"Quotes: {metrics['quotes_today']}")
        spam_var.set(f"Spam: {metrics['spam_today']}")
        open_tickets_var.set(f"Open tickets: {metrics['open_tickets']}")
        high_priority_var.set(f"High-priority open tickets: {metrics['high_priority_open_tickets']}")
        completed_today_var.set(f"Jobs completed today: {metrics['jobs_completed_today']}")
        leads_week_var.set(f"Leads this week: {metrics['leads_this_week']}")
        avg_response_var.set(f"Avg response time (min): {metrics['avg_response_minutes']}")
        status_label_var.set(f"Status: {runtime_state['status']}")
        last_checked_var.set(f"Last checked email: {runtime_state.get('last_checked', '--')}")

        if ticket_list is not None:
            ticket_list.delete(0, tk.END)
            for item in get_recent_open_tickets(config):
                ticket_list.insert(tk.END, item)

        if workload_list is not None:
            workload_list.delete(0, tk.END)
            for row in technicians.technician_workload(Path(config.get("db_path", "data/hvac_agent.db"))):
                tech = row.get("technician") or "Unassigned"
                count = row.get("open_jobs") or 0
                workload_list.insert(tk.END, f"{tech}: {count}")

        if jobs_tree is not None:
            jobs_tree.delete(*jobs_tree.get_children())
            for row in crm.list_open_jobs(Path(config.get("db_path", "data/hvac_agent.db"))):
                customer = f"{row.get('first_name','')} {row.get('last_name','')}".strip()
                jobs_tree.insert(
                    "",
                    tk.END,
                    values=(
                        row.get("job_id", ""),
                        customer,
                        row.get("address", row.get("customer_address", "")),
                        row.get("service_type", ""),
                        row.get("priority", ""),
                        row.get("technician_assigned", ""),
                        row.get("scheduled_time", ""),
                        row.get("status", ""),
                    ),
                )

        def format_job_line(row: dict) -> str:
            issue = row.get("issue_description") or row.get("service_type") or "HVAC job"
            customer = f"{row.get('first_name','')} {row.get('last_name','')}".strip() or "Customer"
            address = row.get("address", row.get("customer_address", "")) or "Address"
            priority = str(row.get("priority", "")).lower()
            badge = "[HIGH]" if priority == "high" else "[NORMAL]"
            return f"{badge} {issue} | {customer} | {address}"

        if new_jobs_list is not None:
            new_jobs_list.delete(0, tk.END)
            for row in crm.list_jobs_by_status(db_path, ["new"]):
                new_jobs_list.insert(tk.END, format_job_line(row))

        if in_progress_list is not None:
            in_progress_list.delete(0, tk.END)
            for row in crm.list_jobs_by_status(db_path, ["scheduled", "in progress"]):
                tech = row.get("technician_assigned") or "TBD"
                issue = row.get("issue_description") or row.get("service_type") or "HVAC job"
                customer = f"{row.get('first_name','')} {row.get('last_name','')}".strip() or "Customer"
                in_progress_list.insert(tk.END, f"{tech} -> {issue} -> {customer}")

        if completed_today_list is not None:
            completed_today_list.delete(0, tk.END)
            today_local = system_now().date()
            for row in crm.list_jobs_by_status(db_path, ["completed"]):
                completed_at = parse_iso_datetime(row.get("last_updated_at"))
                if not completed_at or completed_at.date() != today_local:
                    continue
                completed_today_list.insert(tk.END, format_job_line(row))

        if activity_list is not None:
            activity_list.delete(0, tk.END)
            for item in get_recent_activity(config, limit=6):
                activity_list.insert(tk.END, item)

        refresh_ticket_cards()
        refresh_history_tree()
        root.after(refresh_seconds * 1000, update_metrics)

    def worker_loop() -> None:
        runtime_state["status"] = "Running (dry-run)" if dry_run else "Running"
        failures = 0
        try:
            while not stop_event.is_set():
                auth_failure = False
                try:
                    if not has_email_credentials(config):
                        runtime_state["status"] = "Needs sign-in"
                        stop_event.wait(poll_interval)
                        continue
                    process_inbox(config, dry_run_override=dry_run)
                    failures = 0
                    runtime_state["last_checked"] = system_now().strftime("%Y-%m-%d %H:%M")
                    runtime_state["status"] = "Running OK (dry-run)" if dry_run else "Running OK"
                except MailAuthError as err:
                    failures += 1
                    auth_failure = True
                    append_error_log(config, "ui worker: mailbox login rejected", err)
                    runtime_state["status"] = "Login rejected - run setup again"
                except MailUnavailable as err:
                    failures += 1
                    append_error_log(config, "ui worker: mail server unavailable", err)
                    runtime_state["status"] = "Mail server unreachable - retrying"
                except Exception as err:
                    failures += 1
                    append_error_log(config, "ui worker cycle failure", err)
                    runtime_state["status"] = "Error (check error.log)"
                # Back off on repeated failures instead of hammering the server (AUDIT H10).
                stop_event.wait(backoff_seconds(poll_interval, failures, auth_failure))
        finally:
            agent_lock.release()
            runtime_state["status"] = "Stopped"

    def start_agent() -> None:
        thread = worker_ref.get("thread")
        if thread and thread.is_alive():
            return
        # AUDIT C3: refuse to start if the background task (or another UI)
        # is already processing this mailbox.
        if not dry_run and not agent_lock.acquire():
            messagebox.showerror(
                "Already running",
                "Another copy of the HVAC agent is already processing this mailbox "
                "(the background task or another window). Stop it before starting here.",
            )
            return
        stop_event.clear()
        new_thread = threading.Thread(target=worker_loop, daemon=True)
        worker_ref["thread"] = new_thread
        new_thread.start()

    def stop_agent() -> None:
        stop_event.set()

    fullscreen_state = {"enabled": False}

    def set_fullscreen(enable: bool) -> None:
        fullscreen_state["enabled"] = enable
        root.attributes("-fullscreen", enable)
        root.overrideredirect(enable)

    def toggle_fullscreen() -> None:
        set_fullscreen(not fullscreen_state["enabled"])

    toggle_guard = {"busy": False}

    def save_feature_toggles() -> None:
        if toggle_guard["busy"]:
            return
        auto_reply_enabled = bool(feature_auto_reply.get())
        if auto_reply_enabled and not config.get("auto_reply_enabled", False) and not dry_run:
            # AUDIT M12: one click used to start emailing real customers immediately.
            confirmed = messagebox.askyesno(
                "Turn on auto-replies?",
                "The agent is in LIVE mode. Turning this on sends acknowledgment emails to real customers.\n\nContinue?",
            )
            if not confirmed:
                toggle_guard["busy"] = True
                feature_auto_reply.set(False)
                toggle_guard["busy"] = False
                return
        # Both the canonical and the legacy key are written so they can never disagree.
        updates = {
            "auto_reply_enabled": auto_reply_enabled,
            "feature_auto_reply": auto_reply_enabled,
            "feature_ticketing": bool(feature_ticketing.get()),
            "dispatch_summary_enabled": bool(feature_dispatch.get()),
            "feature_dispatch_summary": bool(feature_dispatch.get()),
            "sms_enabled": bool(feature_sms.get()),
            "feature_sms_notifications": bool(feature_sms.get()),
            "follow_up_enabled": bool(feature_followup.get()),
            "feature_follow_up": bool(feature_followup.get()),
        }
        config.update(updates)
        try:
            patch_config_file(Path(config.get("config_path", "config.json")), updates)
        except Exception as err:
            append_error_log(config, "saving feature toggles", err)
            messagebox.showerror("Could not save settings", f"The change is active now but was not saved to config.json:\n{err}")

    def auto_save_toggle(*_args: object) -> None:
        save_feature_toggles()

    for _var in (feature_auto_reply, feature_ticketing, feature_dispatch, feature_sms, feature_followup):
        _var.trace_add("write", auto_save_toggle)

    def normalize_status_value(value: str) -> str:
        cleaned = normalize_text(str(value))
        cleaned = cleaned.replace("_", " ").replace("-", " ").strip()
        if cleaned in {"inprogress", "in progress", "progress"}:
            return "in progress"
        if cleaned in {"scheduled", "schedule"}:
            return "scheduled"
        if cleaned in {"completed", "complete", "closed", "done"}:
            return "completed"
        if cleaned in {"new", "open", ""}:
            return "new"
        return "new"

    def update_job() -> None:
        job_id = job_id_var.get().strip()
        technician_name = tech_var.get().strip()
        scheduled_time = schedule_var.get().strip()
        status_value = normalize_status_value(job_status_var.get())
        if not job_id:
            messagebox.showwarning("Missing", "Enter Job ID.")
            return
        if status_value not in {"new", "scheduled", "in progress", "completed"}:
            messagebox.showwarning("Invalid", "Status must be new, scheduled, in progress, or completed.")
            return
        db_path = Path(config.get("db_path", "data/hvac_agent.db"))
        if scheduled_time and technician_name:
            if not scheduler.is_slot_available(db_path, technician_name, scheduled_time):
                messagebox.showwarning("Conflict", "Technician has a job at that time.")
                return
        crm.update_job_status(db_path, int(job_id), status_value, scheduled_time, technician_name)
        messagebox.showinfo("Updated", f"Job {job_id} updated.")

    def add_technician_ui() -> None:
        name = tech_name_var.get().strip()
        phone = tech_phone_var.get().strip()
        area = tech_area_var.get().strip()
        if not name:
            messagebox.showwarning("Missing", "Technician name is required.")
            return
        technicians.add_technician(Path(config.get("db_path", "data/hvac_agent.db")), name, phone, area, 1)
        messagebox.showinfo("Added", f"Technician {name} added.")

    def sort_jobs(rows: list[dict]) -> list[dict]:
        mode = ticket_sort_var.get().strip().lower()
        if mode == "oldest":
            return sorted(rows, key=lambda row: row.get("created_at", ""))
        if mode == "service type":
            return sorted(rows, key=lambda row: str(row.get("service_type", "")).lower())
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def build_ticket_signature(rows: list[dict]) -> str:
        return "|".join(
            f"{row.get('job_id','')}:{row.get('status','')}:{row.get('last_updated_at','')}:"
            f"{row.get('technician_assigned','')}:{row.get('priority','')}"
            for row in rows
        )

    def refresh_ticket_cards(force: bool = False) -> None:
        if tickets_container is None or tickets_canvas is None:
            return
        xview = tickets_canvas.xview()
        rows = crm.list_jobs_by_status(db_path, ["new", "scheduled", "in progress"])
        rows = sort_jobs(rows)
        signature = build_ticket_signature(rows)
        if not force and signature == ticket_signature_cache["value"]:
            return
        ticket_signature_cache["value"] = signature

        for child in tickets_container.winfo_children():
            child.destroy()

        tech_names = [row.get("name", "") for row in technicians.list_technicians(db_path)]
        tech_options = ["TBD"] + [name for name in tech_names if name] + ["Add technician..."]

        for idx, row in enumerate(rows):
            job_id = row.get("job_id", "")
            customer = f"{row.get('first_name','')} {row.get('last_name','')}".strip() or "Unknown"
            issue = row.get("issue_description", "") or "HVAC request"
            service_type = row.get("service_type", "") or "Unknown"
            priority = row.get("priority", "") or "Normal"
            address = row.get("address", row.get("customer_address", "")) or "Unknown"
            phone = row.get("phone", "") or ""
            created_at = row.get("created_at", "")
            scheduled_time = row.get("scheduled_time", "")
            technician_name = row.get("technician_assigned", "") or "TBD"
            if technician_name and technician_name not in tech_options:
                tech_options.insert(1, technician_name)
            status_label = normalize_status_value(row.get("status", ""))

            shadow = tk.Frame(tickets_container, bg="#0B1218")
            shadow.grid(row=idx % 2, column=idx // 2, padx=8, pady=8, sticky="n")
            card = tk.Frame(shadow, bg=panel_color, relief="flat", borderwidth=0, padx=12, pady=10)
            card.pack(padx=(0, 3), pady=(0, 3))

            header = tk.Label(
                card,
                text=f"Ticket {job_id} - {service_type}",
                font=("Segoe UI", 10, "bold"),
                bg=panel_color,
                fg=text_color,
            )
            header.pack(anchor="w")

            details = (
                f"Customer: {customer}\n"
                f"Phone: {phone}\n"
                f"Address: {address}\n"
                f"Issue: {issue}\n"
                f"Priority: {priority}\n"
                f"Status: {status_label}\n"
                f"Created: {created_at}\n"
                f"Scheduled: {scheduled_time}"
            )
            details_text = tk.Text(
                card,
                height=8,
                wrap="word",
                bg=panel_color,
                fg=text_color,
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                font=("Segoe UI", 9),
            )
            details_text.insert("1.0", details)
            details_text.bind("<Key>", lambda _event: "break")
            details_text.bind("<<Paste>>", lambda _event: "break")
            details_text.bind("<Button-1>", lambda _event: details_text.focus_set())
            details_text.pack(anchor="w", pady=(6, 6), fill="x")

            bottom = tk.Frame(card, bg=panel_color)
            bottom.pack(fill="x")
            tk.Label(bottom, text="Technician:", font=("Segoe UI", 9), bg=panel_color, fg=text_color).pack(side="left")
            tech_var_card = tk.StringVar(value=technician_name)
            tech_box = ttk.Combobox(bottom, textvariable=tech_var_card, values=tech_options, width=16, state="readonly")
            tech_box.pack(side="left", padx=(6, 6))

            status_value = status_label
            status_var_card = tk.StringVar(value=status_value)
            status_box = ttk.Combobox(
                bottom,
                textvariable=status_var_card,
                values=["new", "scheduled", "in progress", "completed"],
                width=12,
                state="readonly",
            )
            status_box.pack(side="left", padx=(6, 6))

            def bind_scroll(widget: tk.Widget) -> None:
                widget.bind("<MouseWheel>", on_ticket_scroll)
                widget.bind("<Shift-MouseWheel>", on_ticket_scroll)

            for widget in (shadow, card, header, details_text, bottom, tech_box, status_box):
                bind_scroll(widget)

            def handle_tech_selection(var: tk.StringVar) -> None:
                if var.get() != "Add technician...":
                    return
                name = simpledialog.askstring("Add Technician", "Technician name:")
                if not name:
                    var.set("TBD")
                    return
                phone = simpledialog.askstring("Add Technician", "Phone (optional):") or ""
                area = simpledialog.askstring("Add Technician", "Service area (optional):") or ""
                technicians.add_technician(db_path, name.strip(), phone.strip(), area.strip(), 1)
                var.set(name.strip())
                refresh_ticket_cards(force=True)

            tech_box.bind("<<ComboboxSelected>>", lambda _event, var=tech_var_card: handle_tech_selection(var))

            def save_assignment(job_id_val: int, var: tk.StringVar) -> None:
                value = var.get().strip()
                crm.update_job_assignment(db_path, job_id_val, "" if value.lower() == "tbd" else value)
                refresh_ticket_cards(force=True)

            def save_status(job_id_val: int, status_value: str) -> None:
                crm.set_job_status(db_path, job_id_val, normalize_status_value(status_value))
                refresh_ticket_cards(force=True)
                refresh_history_tree()

            def close_ticket(job_id_val: int) -> None:
                crm.set_job_status(db_path, job_id_val, "completed")
                refresh_ticket_cards(force=True)
                refresh_history_tree()

            save_button = tk.Button(
                bottom,
                text="Save",
                bg=accent_color,
                fg="white",
                activebackground=accent_color,
                relief="flat",
                command=lambda jid=int(job_id): (
                    save_assignment(jid, tech_var_card),
                    save_status(jid, status_var_card.get().strip()),
                ),
            )
            save_button.pack(side="left", padx=(6, 6))
            bind_scroll(save_button)

            done_button = tk.Button(
                bottom,
                text="Done",
                bg=accent_color,
                fg="white",
                activebackground=accent_color,
                relief="flat",
                command=lambda jid=int(job_id): close_ticket(jid),
            )
            done_button.pack(side="left")
            bind_scroll(done_button)

        tickets_container.update_idletasks()
        tickets_canvas.configure(scrollregion=tickets_canvas.bbox("all"))
        try:
            tickets_canvas.xview_moveto(xview[0])
        except (tk.TclError, IndexError, TypeError):
            pass

    def refresh_history_tree() -> None:
        if history_tree is None:
            return
        history_tree.delete(*history_tree.get_children())
        rows = crm.list_jobs_by_status(db_path, ["completed"])
        rows = sorted(rows, key=lambda row: row.get("last_updated_at", ""), reverse=True)
        for row in rows:
            customer = f"{row.get('first_name','')} {row.get('last_name','')}".strip()
            history_tree.insert(
                "",
                tk.END,
                values=(
                    row.get("job_id", ""),
                    customer,
                    row.get("service_type", ""),
                    row.get("address", ""),
                    row.get("last_updated_at", ""),
                ),
            )

    def on_close() -> None:
        stop_event.set()
        root.destroy()

    frame = tk.Frame(overview_tab, padx=16, pady=16, bg=bg_color)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="HVAC Service Automation", style="Title.TLabel").pack(anchor="w")
    tk.Label(frame, textvariable=status_label_var, bg=bg_color, fg=text_color).pack(anchor="w", pady=(8, 0))
    tk.Label(frame, textvariable=last_checked_var, bg=bg_color, fg=text_color).pack(anchor="w")
    tk.Label(frame, textvariable=license_label_var, bg=bg_color, fg=text_color).pack(anchor="w")

    ttk.Label(
        frame,
        text="Automatically turns your emails into organized job tickets so you never miss a customer.",
        style="Subtitle.TLabel",
    ).pack(anchor="w", pady=(6, 12))

    tk.Label(frame, text="Main Screen", font=("Segoe UI", 12, "bold"), background=bg_color, foreground=text_color).pack(
        anchor="w"
    )
    main_grid = tk.Frame(frame, bg=bg_color)
    main_grid.pack(anchor="w", fill="x", pady=(6, 12))

    new_jobs_list = tk.Listbox(
        main_grid,
        width=40,
        height=6,
        bg=panel_color,
        fg=text_color,
        selectbackground=accent_color,
        selectforeground=text_color,
        highlightthickness=0,
        bd=0,
    )
    in_progress_list = tk.Listbox(
        main_grid,
        width=40,
        height=6,
        bg=panel_color,
        fg=text_color,
        selectbackground=accent_color,
        selectforeground=text_color,
        highlightthickness=0,
        bd=0,
    )
    completed_today_list = tk.Listbox(
        main_grid,
        width=40,
        height=6,
        bg=panel_color,
        fg=text_color,
        selectbackground=accent_color,
        selectforeground=text_color,
        highlightthickness=0,
        bd=0,
    )

    tk.Label(main_grid, text="NEW JOBS", font=("Segoe UI", 10, "bold"), bg=bg_color, fg=text_color).grid(
        row=0, column=0, sticky="w"
    )
    tk.Label(main_grid, text="IN PROGRESS", font=("Segoe UI", 10, "bold"), bg=bg_color, fg=text_color).grid(
        row=0, column=1, sticky="w"
    )
    tk.Label(main_grid, text="COMPLETED TODAY", font=("Segoe UI", 10, "bold"), bg=bg_color, fg=text_color).grid(
        row=0, column=2, sticky="w"
    )
    new_jobs_list.grid(row=1, column=0, padx=(0, 12), sticky="w")
    in_progress_list.grid(row=1, column=1, padx=(0, 12), sticky="w")
    completed_today_list.grid(row=1, column=2, sticky="w")

    stats_frame = tk.Frame(frame, bg=panel_color, padx=12, pady=10, highlightthickness=0, bd=0)
    stats_visible = {"value": False}

    def toggle_stats() -> None:
        if stats_visible["value"]:
            stats_frame.pack_forget()
            stats_toggle.config(text="Show Stats")
            stats_visible["value"] = False
        else:
            stats_frame.pack(anchor="w", fill="x", pady=(8, 0))
            stats_toggle.config(text="Hide Stats")
            stats_visible["value"] = True

    stats_toggle = tk.Button(
        frame,
        text="Show Stats",
        bg=accent_color,
        fg="white",
        activebackground=accent_color,
        highlightthickness=0,
        bd=0,
        relief="flat",
        command=toggle_stats,
    )
    stats_toggle.pack(anchor="w", pady=(6, 0))

    tk.Label(stats_frame, textvariable=processed_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=service_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=quote_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=spam_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=open_tickets_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=high_priority_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=completed_today_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=leads_week_var, bg=panel_color, fg=text_color).pack(anchor="w")
    tk.Label(stats_frame, textvariable=avg_response_var, bg=panel_color, fg=text_color).pack(anchor="w")

    tk.Label(stats_frame, text="Recent Activity", font=("Segoe UI", 10, "bold"), bg=panel_color, fg=text_color).pack(
        anchor="w", pady=(8, 4)
    )
    activity_list = tk.Listbox(
        stats_frame,
        width=110,
        height=4,
        bg=panel_color,
        fg=text_color,
        selectbackground=accent_color,
        highlightthickness=0,
        bd=0,
    )
    activity_list.pack(anchor="w", fill="x")

    button_bar = tk.Frame(frame, bg=bg_color)
    button_bar.pack(anchor="w", pady=(16, 0))
    tk.Button(
        button_bar,
        text="Start",
        width=12,
        bg=accent_color,
        fg="white",
        activebackground=accent_color,
        highlightthickness=0,
        bd=0,
        relief="flat",
        command=start_agent,
    ).pack(side="left", padx=(0, 8))
    tk.Button(
        button_bar,
        text="Stop",
        width=12,
        bg=accent_color,
        fg="white",
        activebackground=accent_color,
        highlightthickness=0,
        bd=0,
        relief="flat",
        command=stop_agent,
    ).pack(side="left")
    tk.Button(
        button_bar,
        text="Full Screen",
        width=12,
        bg=accent_color,
        fg="white",
        activebackground=accent_color,
        highlightthickness=0,
        bd=0,
        relief="flat",
        command=toggle_fullscreen,
    ).pack(side="left", padx=(8, 0))

    tk.Label(frame, text="Feature Toggles", font=("Segoe UI", 11, "bold"), bg=bg_color, fg=text_color).pack(
        anchor="w", pady=(16, 4)
    )
    toggles = tk.Frame(frame, bg=bg_color)
    toggles.pack(anchor="w")
    toggle_kwargs = {
        "bg": bg_color,
        "fg": text_color,
        "activebackground": bg_color,
        "activeforeground": text_color,
        "selectcolor": accent_color,
        "highlightthickness": 0,
        "bd": 0,
    }
    tk.Checkbutton(toggles, text="Auto Replies", variable=feature_auto_reply, **toggle_kwargs).pack(
        side="left", padx=(0, 8)
    )
    tk.Checkbutton(toggles, text="Ticketing/CRM", variable=feature_ticketing, **toggle_kwargs).pack(
        side="left", padx=(0, 8)
    )
    tk.Checkbutton(toggles, text="Dispatch Summary", variable=feature_dispatch, **toggle_kwargs).pack(
        side="left", padx=(0, 8)
    )
    tk.Checkbutton(toggles, text="SMS Alerts", variable=feature_sms, **toggle_kwargs).pack(
        side="left", padx=(0, 8)
    )
    tk.Checkbutton(toggles, text="Follow-Up Emails", variable=feature_followup, **toggle_kwargs).pack(
        side="left", padx=(0, 8)
    )

    # Dispatch board and admin controls moved to the Tickets tab.

    tickets_frame = tk.Frame(tickets_tab, padx=16, pady=16, bg=bg_color)
    tickets_frame.pack(fill="both", expand=True)
    tk.Label(tickets_frame, text="Open Tickets", font=("Segoe UI", 14, "bold"), bg=bg_color, fg=text_color).pack(
        anchor="w"
    )
    sort_bar = tk.Frame(tickets_frame, bg=bg_color)
    sort_bar.pack(anchor="w", pady=(8, 6))
    tk.Label(sort_bar, text="Sort by:", font=("Segoe UI", 9), bg=bg_color, fg=text_color).pack(side="left")
    sort_box = ttk.Combobox(
        sort_bar,
        textvariable=ticket_sort_var,
        values=["Newest", "Oldest", "Service Type"],
        width=14,
        state="readonly",
    )
    sort_box.pack(side="left", padx=(6, 0))
    sort_box.bind("<<ComboboxSelected>>", lambda _event: refresh_ticket_cards(force=True))

    tickets_canvas = tk.Canvas(tickets_frame, height=420, bg=bg_color, highlightthickness=0)
    tickets_scroll = ttk.Scrollbar(tickets_frame, orient="horizontal", command=tickets_canvas.xview)
    tickets_canvas.configure(xscrollcommand=tickets_scroll.set)
    tickets_canvas.pack(fill="both", expand=True)
    tickets_scroll.pack(fill="x")

    tickets_container = tk.Frame(tickets_canvas, bg=bg_color)
    tickets_canvas.create_window((0, 0), window=tickets_container, anchor="nw")
    tickets_container.bind("<Configure>", lambda _event: tickets_canvas.configure(scrollregion=tickets_canvas.bbox("all")))

    def on_ticket_scroll(event: tk.Event) -> str:
        delta = -1 if event.delta > 0 else 1
        tickets_canvas.xview_scroll(delta, "units")
        return "break"

    tickets_canvas.bind("<MouseWheel>", on_ticket_scroll)
    tickets_container.bind("<MouseWheel>", on_ticket_scroll)

    history_frame = tk.Frame(history_tab, padx=16, pady=16, bg=bg_color)
    history_frame.pack(fill="both", expand=True)
    tk.Label(history_frame, text="Closed Tickets", font=("Segoe UI", 14, "bold"), bg=bg_color, fg=text_color).pack(
        anchor="w"
    )

    history_tree = ttk.Treeview(
        history_frame,
        columns=("job_id", "customer", "service", "address", "closed_at"),
        show="headings",
        height=12,
    )
    for col, label, width in [
        ("job_id", "Job ID", 70),
        ("customer", "Customer", 160),
        ("service", "Service Type", 140),
        ("address", "Address", 220),
        ("closed_at", "Closed At", 160),
    ]:
        history_tree.heading(col, text=label)
        history_tree.column(col, width=width, anchor="w")
    history_tree.pack(fill="both", expand=True, pady=(8, 8))

    def reopen_selected() -> None:
        selection = history_tree.selection()
        if not selection:
            messagebox.showwarning("Select", "Select a closed ticket to reopen.")
            return
        for item in selection:
            job_id = history_tree.item(item, "values")[0]
            try:
                crm.set_job_status(db_path, int(job_id), "new")
            except (TypeError, ValueError):
                continue
        refresh_ticket_cards(force=True)
        refresh_history_tree()

    tk.Button(
        history_frame,
        text="Reopen Selected",
        bg=accent_color,
        fg="white",
        activebackground=accent_color,
        relief="flat",
        command=reopen_selected,
    ).pack(anchor="w")

    reset_state = {"armed": False}

    def perform_full_reset() -> None:
        thread = worker_ref.get("thread")
        if thread and thread.is_alive():
            # Deleting the database under a running cycle would lose its writes.
            messagebox.showwarning("Agent running", "Press Stop and wait until the status shows Stopped, then reset.")
            return
        data_paths = [
            db_path,
            Path(str(config.get("leads_csv_file", "data/hvac_leads.csv"))),
            Path(str(config.get("service_requests_file", "data/service_requests.csv"))),
            Path(str(config.get("draft_replies_file", "data/draft_replies.csv"))),
            Path(str(config.get("job_tickets_file", "data/job_tickets.csv"))),
            Path(str(config.get("processed_ids_file", "data/processed_ids.txt"))),
            Path(str(config.get("sender_cooldown_file", "data/sender_cooldown.json"))),
            Path(str(config.get("follow_up_state_file", "data/follow_up_state.json"))),
            Path(str(config.get("dispatch_summary_state_file", "data/dispatch_summary_state.json"))),
            Path(str(config.get("dispatch_summary_log_file", "logs/dispatch_summary.log"))),
            Path(str(config.get("error_log_file", "logs/error.log"))),
            Path(str(config.get("agent_log_file", "logs/hvac_agent.log"))),
        ]
        wal_path = Path(str(db_path)) if str(db_path).endswith(".db") else None
        if wal_path:
            data_paths.extend([Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")])

        # AUDIT H7: always back up first; if the backup fails, delete nothing.
        try:
            backup_dir = backup_paths(data_paths, Path("backups"), "ui-reset")
        except Exception as err:
            append_error_log(config, "reset backup failed", err)
            messagebox.showerror("Reset cancelled", f"Could not back up the data first, so nothing was deleted.\n\n{err}")
            return

        for path in data_paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                continue

        store.open_state_db(db_path).close()
        refresh_ticket_cards(force=True)
        refresh_history_tree()
        messagebox.showinfo("Reset complete", f"All data was cleared. A backup was saved to:\n{backup_dir}")

    def reset_button_click() -> None:
        if not reset_state["armed"]:
            reset_state["armed"] = True
            reset_button.config(text="Confirm Reset (click again)")

            def reset_timeout() -> None:
                reset_state["armed"] = False
                reset_button.config(text="Reset All Data")

            root.after(8000, reset_timeout)
            return

        reset_state["armed"] = False
        reset_button.config(text="Reset All Data")
        perform_full_reset()

    reset_button = tk.Button(
        history_frame,
        text="Reset All Data",
        bg=accent_color,
        fg="white",
        activebackground=accent_color,
        relief="flat",
        command=reset_button_click,
    )
    reset_button.pack(anchor="w", pady=(8, 0))

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.bind("<Escape>", lambda _event: set_fullscreen(False))
    root.bind("<F11>", lambda _event: toggle_fullscreen())
    update_metrics()
    root.mainloop()


def backoff_seconds(poll_interval: int, consecutive_failures: int, auth_failure: bool) -> int:
    """How long to wait after failures (AUDIT H10).

    Doubles per consecutive failure, capped at 15 min (30 min for a rejected
    login, so a wrong password doesn't hammer the provider and risk a lockout).
    """
    base = max(5, int(poll_interval))
    if consecutive_failures <= 0:
        return base
    cap = 1800 if auth_failure else 900
    return min(cap, base * (2 ** min(consecutive_failures, 10)))


def run_agent(
    config: dict,
    max_emails: int | None = None,
    dry_run_override: bool | None = None,
    daemon: bool = False,
    poll_interval_override: int | None = None,
) -> None:
    """Run one-shot or daemonized agent loop with heartbeat, backoff and error capture."""
    dry_run = resolve_dry_run(config, dry_run_override)
    poll_interval = int(config.get("poll_interval_seconds", 30))
    if poll_interval_override is not None:
        poll_interval = max(5, poll_interval_override)

    heartbeat_minutes = int(config.get("heartbeat_interval_minutes", 30))
    next_heartbeat: datetime | None = None
    if daemon and heartbeat_minutes > 0:
        next_heartbeat = datetime.now(timezone.utc) + timedelta(minutes=heartbeat_minutes)

    # AUDIT C3: only one live agent per data folder. Dry-run works on an
    # in-memory copy and changes nothing, so it may run alongside.
    lock = InstanceLock(lock_path_for(config))
    if not dry_run and not lock.acquire():
        raise AlreadyRunningError(
            "Another HVAC agent (background task or the UI's Start button) is already running "
            "for this data folder. Stop it first."
        )

    mode = "daemon" if daemon else "one-shot"
    print(f"Agent started at {utc_now_iso()} | mode={mode} | dry_run={dry_run}")
    failures = 0
    try:
        while True:
            auth_failure = False
            try:
                process_inbox(config, max_emails=max_emails, dry_run_override=dry_run_override)
                failures = 0
            except KeyboardInterrupt:
                print("Agent stopped by user.")
                raise
            except MailAuthError as err:
                failures += 1
                auth_failure = True
                append_error_log(config, "mailbox login rejected", err)
                print("Mailbox login was rejected. Check the email address / app password (run --quick-setup).")
                log_event(config, "error", context="login", error=str(err))
            except MailUnavailable as err:
                failures += 1
                print(f"Mail server unavailable: {err}")
                log_event(config, "error", context="network", error=str(err))
            except Exception as err:
                failures += 1
                append_error_log(config, "process_inbox failure", err)
                print(f"Error encountered. Details written to {config.get('error_log_file', 'error.log')}")
                log_event(config, "error", context="process_inbox", error=str(err))

            if not daemon:
                return

            now_utc = datetime.now(timezone.utc)
            if next_heartbeat and now_utc >= next_heartbeat:
                print(f"Agent heartbeat at {utc_now_iso()} | waiting for next poll")
                next_heartbeat = now_utc + timedelta(minutes=heartbeat_minutes)

            delay = backoff_seconds(poll_interval, failures, auth_failure)
            if failures:
                print(f"Retrying in {delay}s ({failures} consecutive failure(s)).")
            time_module.sleep(delay)
    finally:
        lock.release()


def run_demo(config: dict) -> None:
    """Run fixed sample cases to validate classification and service detection quickly."""
    samples = [
        {
            "subject": "Furnace issue",
            "body": "My furnace stopped working this morning and we have no heat.",
            "expected": "lead",
        },
        {
            "subject": "Need quote",
            "body": "Looking for quote on AC install in Oshawa.",
            "expected": "lead",
        },
        {
            "subject": "Invoice follow up",
            "body": "Following up on the invoice sent last week.",
            "expected": "existing",
        },
        {
            "subject": "Grow your traffic",
            "body": "Boost your SEO traffic with backlinks and marketing.",
            "expected": "spam",
        },
        {
            "subject": "Emergency repair",
            "body": "Need emergency repair tonight. Furnace is broken.",
            "expected": "lead",
        },
    ]

    print("Demo classification run:")
    for idx, sample in enumerate(samples, start=1):
        result = classify(sample["subject"], sample["body"], config)
        service = detect_service(sample["subject"], sample["body"], config)
        extracted = extract_service_request(
            sample["subject"],
            sample["body"],
            sender_name="Demo User",
            sender_email="demo@example.com",
            service_detected=service,
            config=config,
        )
        auto_reply = "yes" if result.classification == "lead" else "no"
        status = "OK" if result.classification == sample["expected"] else "MISMATCH"
        print(
            f"{idx}. class={result.classification} expected={sample['expected']} [{status}] "
            f"auto-reply={auto_reply} service={service} priority={extracted['priority']}"
        )

def prompt_text(prompt: str, default_value: str = "") -> str:
    """Prompt for free-text value with default support."""
    default_value = default_value or ""
    suffix = f" [{default_value}]" if default_value else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else default_value


def prompt_bool(prompt: str, default_value: bool) -> bool:
    """Prompt for yes/no and return boolean choice."""
    default_label = "Y/n" if default_value else "y/N"
    raw = input(f"{prompt} ({default_label}): ").strip().lower()
    if not raw:
        return default_value
    return raw in {"y", "yes", "true", "1"}


def parse_weekday_list(raw_value: str) -> list[str]:
    """Parse comma-separated weekday names into internal 3-letter keys."""
    token_map = {
        "mon": "mon",
        "monday": "mon",
        "tue": "tue",
        "tues": "tue",
        "tuesday": "tue",
        "wed": "wed",
        "wednesday": "wed",
        "thu": "thu",
        "thur": "thu",
        "thurs": "thu",
        "thursday": "thu",
        "fri": "fri",
        "friday": "fri",
        "sat": "sat",
        "saturday": "sat",
        "sun": "sun",
        "sunday": "sun",
    }

    result: list[str] = []
    for token in [part.strip().lower() for part in raw_value.split(",") if part.strip()]:
        mapped = token_map.get(token)
        if mapped and mapped not in result:
            result.append(mapped)

    return result


def run_setup(config_path: Path) -> None:
    """Interactive installer to create/update local config.json."""
    try:
        run_setup_gui(config_path)
        return
    except Exception:
        pass

    existing = {}
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            existing = {}

    config = deep_merge(DEFAULT_CONFIG, existing if isinstance(existing, dict) else {})

    print("HVAC setup wizard")
    print("Provide business and email settings. Press Enter to keep defaults.\n")

    config["business_name"] = prompt_text("Business name", str(config.get("business_name", "")))
    config["phone_number"] = prompt_text("Phone number", str(config.get("phone_number", "")))
    config["default_service_area"] = prompt_text("Default service area", str(config.get("default_service_area", "")))
    config["dispatcher_phone"] = prompt_text("Dispatcher phone number", str(config.get("dispatcher_phone", "")))
    config["office_hours"] = prompt_text("Office hours display text", str(config.get("office_hours", "Mon-Fri 8am-5pm")))
    config["office_timezone"] = prompt_text("Office timezone (IANA, e.g. America/Toronto)", str(config.get("office_timezone", "America/Toronto")))

    default_start = str(config.get("office_schedule", {}).get("mon", {}).get("start", "08:00"))
    default_end = str(config.get("office_schedule", {}).get("mon", {}).get("end", "17:00"))
    day_start = prompt_text("Daily start time (HH:MM)", default_start)
    day_end = prompt_text("Daily end time (HH:MM)", default_end)

    default_days = "mon,tue,wed,thu,fri"
    active_days = parse_weekday_list(prompt_text("Business days (comma-separated)", default_days))
    if not active_days:
        active_days = ["mon", "tue", "wed", "thu", "fri"]

    office_schedule = {day: None for day in WEEKDAY_KEYS}
    for day in active_days:
        office_schedule[day] = {"start": day_start, "end": day_end}
    config["office_schedule"] = office_schedule

    config["imap_server"] = prompt_text("IMAP server", str(config.get("imap_server", "imap.example.com")))
    config["imap_port"] = int(prompt_text("IMAP port", str(config.get("imap_port", 993))))
    config["smtp_server"] = prompt_text("SMTP server", str(config.get("smtp_server", "smtp.example.com")))
    config["smtp_port"] = int(prompt_text("SMTP port", str(config.get("smtp_port", 587))))
    config["email_address"] = prompt_text("Email address", str(config.get("email_address", "you@example.com")))
    config["sent_mailbox"] = prompt_text("Sent mailbox folder name", str(config.get("sent_mailbox", "Sent")))

    config["poll_interval_seconds"] = int(prompt_text("Daemon poll interval seconds", str(config.get("poll_interval_seconds", 30))))
    config["heartbeat_interval_minutes"] = int(
        prompt_text("Heartbeat interval minutes (0 to disable)", str(config.get("heartbeat_interval_minutes", 30)))
    )
    config["ui_refresh_seconds"] = int(prompt_text("UI refresh seconds", str(config.get("ui_refresh_seconds", 5))))
    config["error_log_file"] = prompt_text("Error log file", str(config.get("error_log_file", "error.log")))
    config["service_requests_file"] = prompt_text(
        "Service requests CSV file",
        str(config.get("service_requests_file", "service_requests.csv")),
    )
    config["draft_replies_file"] = prompt_text(
        "Draft replies CSV file",
        str(config.get("draft_replies_file", "draft_replies.csv")),
    )
    config["job_tickets_file"] = prompt_text(
        "Job tickets CSV file",
        str(config.get("job_tickets_file", "job_tickets.csv")),
    )
    config["ticket_duplicate_mode"] = prompt_text(
        "Ticket duplicate mode (update|replace|delete)",
        str(config.get("ticket_duplicate_mode", "update")),
    ).lower()
    config["dispatch_summary_enabled"] = prompt_bool(
        "Enable dispatch summary emails",
        bool(config.get("dispatch_summary_enabled", True)),
    )
    config["dispatch_summary_interval_minutes"] = int(
        prompt_text("Dispatch summary interval minutes", str(config.get("dispatch_summary_interval_minutes", 60)))
    )
    config["dispatch_summary_recipient"] = prompt_text(
        "Dispatch summary recipient email",
        str(config.get("dispatch_summary_recipient", config.get("email_address", "you@example.com"))),
    )
    config["license_key"] = prompt_text("License key", str(config.get("license_key", "DEMO-TRIAL")))
    config["license_enforcement"] = prompt_bool(
        "Enable license enforcement",
        bool(config.get("license_enforcement", True)),
    )

    password_hint = "(hidden; press Enter to keep current)"
    password_value = getpass.getpass(f"Email app password {password_hint}: ").replace(" ", "").strip()
    store_mail_password(config, password_value)
    if not credentials.has_credentials(config):
        print("WARNING: no mailbox password is saved yet; the agent cannot sign in until you run setup again.")

    config["auto_reply_enabled"] = prompt_bool("Enable auto-replies", bool(config.get("auto_reply_enabled", False)))
    config["feature_auto_reply"] = bool(config["auto_reply_enabled"])
    config["follow_up_enabled"] = prompt_bool("Enable 24h follow-up automation", bool(config.get("follow_up_enabled", False)))
    config["dry_run"] = prompt_bool("Enable dry-run by default", bool(config.get("dry_run", False)))

    targets = write_config_files(config, config_path)
    write_license_binding(config)
    print("Saved setup to:")
    for path in targets:
        print(f"  {path}")


def run_setup_gui(config_path: Path) -> None:
    """GUI-based setup wizard for non-technical users."""
    import tkinter as tk
    from tkinter import messagebox

    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    config = deep_merge(DEFAULT_CONFIG, existing if isinstance(existing, dict) else {})

    root = tk.Tk()
    root.title("HVAC Setup Wizard")
    root.geometry("520x620")
    root.configure(bg="#0E2A3A")

    def add_field(frame: tk.Frame, label: str, default: str = "", show: str | None = None) -> tk.StringVar:
        var = tk.StringVar(value=default)
        tk.Label(frame, text=label, bg="#0E2A3A", fg="#F5F7FA").pack(anchor="w", pady=(8, 0))
        tk.Entry(frame, textvariable=var, show=show, width=44).pack(anchor="w")
        return var

    tk.Label(root, text="HVAC Setup Wizard", bg="#0E2A3A", fg="#F5F7FA", font=("Segoe UI", 16, "bold")).pack(
        anchor="w", padx=16, pady=(16, 8)
    )
    tk.Label(
        root,
        text="Enter your business and Gmail settings.",
        bg="#0E2A3A",
        fg="#B8C7D1",
        font=("Segoe UI", 10),
    ).pack(anchor="w", padx=16)

    form = tk.Frame(root, bg="#0E2A3A")
    form.pack(fill="both", expand=True, padx=16, pady=8)

    business_name_var = add_field(form, "Business name", str(config.get("business_name", "")))
    phone_var = add_field(form, "Business phone", str(config.get("phone_number", "")))
    office_hours_var = add_field(form, "Office hours", str(config.get("office_hours", "Mon-Fri 8am-5pm")))
    office_tz_var = add_field(form, "Office timezone", str(config.get("office_timezone", "America/Toronto")))
    service_area_var = add_field(form, "Default service area", str(config.get("default_service_area", "")))
    dispatcher_phone_var = add_field(form, "Dispatcher phone", str(config.get("dispatcher_phone", "")))
    email_var = add_field(form, "Gmail address", str(config.get("email_address", "")))
    password_var = add_field(form, "Gmail app password", "", show="*")
    license_var = add_field(form, "License key", str(config.get("license_key", "HVAC-TRIAL-001")))

    imap_server_var = add_field(form, "IMAP server", str(config.get("imap_server", "imap.gmail.com")))
    imap_port_var = add_field(form, "IMAP port", str(config.get("imap_port", 993)))
    smtp_server_var = add_field(form, "SMTP server", str(config.get("smtp_server", "smtp.gmail.com")))
    smtp_port_var = add_field(form, "SMTP port", str(config.get("smtp_port", 587)))
    sent_mailbox_var = add_field(form, "Sent mailbox", str(config.get("sent_mailbox", "[Gmail]/Sent Mail")))

    auto_reply_var = tk.BooleanVar(value=bool(config.get("auto_reply_enabled", False)))
    follow_up_var = tk.BooleanVar(value=bool(config.get("follow_up_enabled", False)))
    dry_run_var = tk.BooleanVar(value=bool(config.get("dry_run", False)))

    tk.Checkbutton(form, text="Enable auto-replies", variable=auto_reply_var, bg="#0E2A3A", fg="#F5F7FA").pack(
        anchor="w", pady=(10, 0)
    )
    tk.Checkbutton(form, text="Enable 24h follow-ups", variable=follow_up_var, bg="#0E2A3A", fg="#F5F7FA").pack(
        anchor="w"
    )
    tk.Checkbutton(form, text="Enable dry-run mode", variable=dry_run_var, bg="#0E2A3A", fg="#F5F7FA").pack(
        anchor="w"
    )

    def show_app_password_help() -> None:
        help_win = tk.Toplevel(root)
        help_win.title("Gmail App Password Help")
        help_win.geometry("520x340")
        help_win.configure(bg="#0E2A3A")
        text = (
            "How to create a Gmail app password:\n\n"
            "1. Go to myaccount.google.com\n"
            "2. Security → 2‑Step Verification (enable if needed)\n"
            "3. App passwords → Select app: Mail → Device: Windows\n"
            "4. Click Generate and copy the 16‑character password\n"
            "5. Paste it here (spaces are okay)\n\n"
            "Tip: Use the app password, not your regular Gmail password."
        )
        tk.Label(help_win, text=text, bg="#0E2A3A", fg="#F5F7FA", justify="left", wraplength=480).pack(
            anchor="w", padx=16, pady=16
        )

    tk.Button(
        form,
        text="How to create an app password",
        bg="#1E88E5",
        fg="white",
        activebackground="#1E88E5",
        relief="flat",
        command=show_app_password_help,
    ).pack(anchor="w", pady=(6, 0))

    def on_save() -> None:
        gmail = email_var.get().strip()
        if not gmail or "@" not in gmail:
            messagebox.showerror("Missing Gmail", "Enter a valid Gmail address.")
            return
        app_password = password_var.get().replace(" ", "").strip()
        if not app_password:
            messagebox.showerror("Missing App Password", "Enter the Gmail app password.")
            return

        config["business_name"] = business_name_var.get().strip()
        config["phone_number"] = phone_var.get().strip()
        config["office_hours"] = office_hours_var.get().strip()
        config["office_timezone"] = office_tz_var.get().strip()
        config["default_service_area"] = service_area_var.get().strip()
        config["dispatcher_phone"] = dispatcher_phone_var.get().strip()
        config["email_address"] = gmail
        config["imap_server"] = imap_server_var.get().strip() or "imap.gmail.com"
        config["imap_port"] = int(imap_port_var.get().strip() or 993)
        config["smtp_server"] = smtp_server_var.get().strip() or "smtp.gmail.com"
        config["smtp_port"] = int(smtp_port_var.get().strip() or 587)
        config["sent_mailbox"] = sent_mailbox_var.get().strip() or "[Gmail]/Sent Mail"
        config["dispatch_summary_recipient"] = gmail
        config["license_key"] = license_var.get().strip()
        config["auto_reply_enabled"] = bool(auto_reply_var.get())
        config["feature_auto_reply"] = bool(auto_reply_var.get())
        config["follow_up_enabled"] = bool(follow_up_var.get())
        config["dry_run"] = bool(dry_run_var.get())

        valid_keys = load_or_initialize_license_keys(Path(str(config.get("license_keys_file", "valid_licenses.json"))))
        if config.get("license_enforcement", True) and config["license_key"] not in valid_keys:
            messagebox.showerror("Invalid License", "License key is invalid. Please enter a valid key.")
            return

        try:
            with ImapMailbox(config["imap_server"], int(config["imap_port"]), gmail, app_password, timeout=20):
                pass
        except MailAuthError:
            messagebox.showerror("Wrong Password", "Could not sign in. Check Gmail and app password.")
            return
        except MailUnavailable:
            messagebox.showerror("Connection Error", "Could not reach Gmail. Check your internet connection.")
            return

        try:
            store_mail_password(config, app_password)
        except credentials.CredentialError as err:
            messagebox.showerror("Could not save password", str(err))
            return
        write_config_files(config, config_path)
        write_license_binding(config)
        # No popups for setup completion.
        root.destroy()

    tk.Button(
        root,
        text="Save Setup",
        bg="#1E88E5",
        fg="white",
        activebackground="#1E88E5",
        relief="flat",
        command=on_save,
    ).pack(anchor="e", padx=16, pady=16)

    root.mainloop()


def run_quick_setup(config_path: Path) -> None:
    """Minimal Gmail-focused setup wizard for buyers/installers."""
    try:
        run_quick_setup_gui(config_path)
        return
    except Exception:
        pass

    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    config = deep_merge(DEFAULT_CONFIG, existing if isinstance(existing, dict) else {})

    print("HVAC quick setup (Gmail)")
    print("Enter Gmail and app password. Other values use safe defaults.\n")

    gmail_address = prompt_text("Gmail address", "")
    gmail_password = getpass.getpass("Gmail app password: ").replace(" ", "").strip()

    default_business_name = str(config.get("business_name", "")) or fallback_name_from_email(gmail_address).title()
    business_name = prompt_text("Business name", default_business_name)
    phone_number = prompt_text("Business phone", str(config.get("phone_number", "555-123-4567")))
    license_key = prompt_text("License key", str(config.get("license_key", "HVAC-TRIAL-001")))

    config["business_name"] = business_name
    config["phone_number"] = phone_number
    config["email_address"] = gmail_address
    store_mail_password(config, gmail_password)
    config["imap_server"] = "imap.gmail.com"
    config["imap_port"] = 993
    config["smtp_server"] = "smtp.gmail.com"
    config["smtp_port"] = 587
    config["sent_mailbox"] = "[Gmail]/Sent Mail"
    config["dispatch_summary_recipient"] = gmail_address
    config["license_key"] = license_key
    config["feature_auto_reply"] = False
    config["auto_reply_enabled"] = False
    config["dry_run"] = False

    targets = write_config_files(config, config_path)
    write_license_binding(config)
    print("Saved quick setup to (password stored in Windows Credential Manager):")
    for path in targets:
        print(f"  {path}")


def run_quick_setup_gui(config_path: Path) -> None:
    """Minimal Gmail-only GUI setup wizard."""
    import tkinter as tk
    from tkinter import messagebox

    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    config = deep_merge(DEFAULT_CONFIG, existing if isinstance(existing, dict) else {})

    root = tk.Tk()
    root.title("HVAC Quick Setup (Gmail)")
    root.geometry("480x420")
    root.configure(bg="#0E2A3A")

    tk.Label(root, text="HVAC Quick Setup (Gmail)", bg="#0E2A3A", fg="#F5F7FA", font=("Segoe UI", 15, "bold")).pack(
        anchor="w", padx=16, pady=(16, 8)
    )
    tk.Label(
        root,
        text="Enter your Gmail details. Other settings use safe defaults.",
        bg="#0E2A3A",
        fg="#B8C7D1",
        font=("Segoe UI", 10),
    ).pack(anchor="w", padx=16)

    form = tk.Frame(root, bg="#0E2A3A")
    form.pack(fill="both", expand=True, padx=16, pady=8)

    def add_field(label: str, show: str | None = None) -> tk.StringVar:
        var = tk.StringVar()
        tk.Label(form, text=label, bg="#0E2A3A", fg="#F5F7FA").pack(anchor="w", pady=(8, 0))
        tk.Entry(form, textvariable=var, show=show, width=40).pack(anchor="w")
        return var

    gmail_var = add_field("Gmail address")
    password_var = add_field("Gmail app password", show="*")
    business_var = add_field("Business name")
    phone_var = add_field("Business phone")
    license_var = add_field("License key")

    def show_app_password_help() -> None:
        help_win = tk.Toplevel(root)
        help_win.title("Gmail App Password Help")
        help_win.geometry("520x340")
        help_win.configure(bg="#0E2A3A")
        text = (
            "How to create a Gmail app password:\n\n"
            "1. Go to myaccount.google.com\n"
            "2. Security → 2‑Step Verification (enable if needed)\n"
            "3. App passwords → Select app: Mail → Device: Windows\n"
            "4. Click Generate and copy the 16‑character password\n"
            "5. Paste it here (spaces are okay)\n\n"
            "Tip: Use the app password, not your regular Gmail password."
        )
        tk.Label(help_win, text=text, bg="#0E2A3A", fg="#F5F7FA", justify="left", wraplength=480).pack(
            anchor="w", padx=16, pady=16
        )

    tk.Button(
        form,
        text="How to create an app password",
        bg="#1E88E5",
        fg="white",
        activebackground="#1E88E5",
        relief="flat",
        command=show_app_password_help,
    ).pack(anchor="w", pady=(6, 0))

    def on_save() -> None:
        gmail = gmail_var.get().strip()
        if not gmail or "@" not in gmail:
            messagebox.showerror("Missing Gmail", "Enter a valid Gmail address.")
            return
        app_password = password_var.get().replace(" ", "").strip()
        if not app_password:
            messagebox.showerror("Missing App Password", "Enter the Gmail app password.")
            return

        business_name = business_var.get().strip() or fallback_name_from_email(gmail).title()
        phone_number = phone_var.get().strip() or str(config.get("phone_number", "555-123-4567"))
        license_key = license_var.get().strip() or str(config.get("license_key", "HVAC-TRIAL-001"))

        config["business_name"] = business_name
        config["phone_number"] = phone_number
        config["email_address"] = gmail
        config["imap_server"] = "imap.gmail.com"
        config["imap_port"] = 993
        config["smtp_server"] = "smtp.gmail.com"
        config["smtp_port"] = 587
        config["sent_mailbox"] = "[Gmail]/Sent Mail"
        config["dispatch_summary_recipient"] = gmail
        config["license_key"] = license_key
        config["feature_auto_reply"] = False
        config["auto_reply_enabled"] = False
        config["dry_run"] = False

        valid_keys = load_or_initialize_license_keys(Path(str(config.get("license_keys_file", "valid_licenses.json"))))
        if config.get("license_enforcement", True) and config["license_key"] not in valid_keys:
            messagebox.showerror("Invalid License", "License key is invalid. Please enter a valid key.")
            return

        try:
            with ImapMailbox(config["imap_server"], int(config["imap_port"]), gmail, app_password, timeout=20):
                pass
        except MailAuthError:
            messagebox.showerror("Wrong Password", "Could not sign in. Check Gmail and app password.")
            return
        except MailUnavailable:
            messagebox.showerror("Connection Error", "Could not reach Gmail. Check your internet connection.")
            return

        try:
            store_mail_password(config, app_password)
        except credentials.CredentialError as err:
            messagebox.showerror("Could not save password", str(err))
            return
        write_config_files(config, config_path)
        write_license_binding(config)
        # No popups for setup completion.
        root.destroy()

    tk.Button(
        root,
        text="Save Setup",
        bg="#1E88E5",
        fg="white",
        activebackground="#1E88E5",
        relief="flat",
        command=on_save,
    ).pack(anchor="e", padx=16, pady=16)

    root.mainloop()


def parse_args() -> argparse.Namespace:
    """Define command-line interface flags and options."""
    parser = argparse.ArgumentParser(description="HVAC automated inquiry responder")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument("--setup", action="store_true", help="Run interactive setup and save config")
    parser.add_argument("--quick-setup", action="store_true", help="Run minimal Gmail-focused setup wizard")
    parser.add_argument("--setup-and-ui", action="store_true", help="Run setup then launch UI")
    parser.add_argument("--demo", action="store_true", help="Run demo classification samples")
    parser.add_argument("--ui", action="store_true", help="Launch local dashboard UI")
    parser.add_argument("--daemon", action="store_true", help="Run continuously and poll inbox on an interval")
    parser.add_argument("--max-emails", type=int, default=None, help="Only process first N fetched emails")
    parser.add_argument("--poll-interval", type=int, default=None, help="Override polling interval in seconds for daemon mode")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    parser.add_argument("--live", action="store_true", help="Force live mode (sends emails)")
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint with setup/demo/live orchestration and fatal error logging."""
    args = parse_args()
    config_path = Path(args.config).resolve()
    os.chdir(config_path.parent)

    try:
        if args.setup:
            run_setup(config_path)
            return
        if args.quick_setup:
            run_quick_setup(config_path)
            return
        if args.setup_and_ui:
            run_quick_setup(config_path)
            if not config_path.exists():
                return
            config = load_config(config_path)
            config["config_path"] = str(config_path)
            validate_license(config)
            run_ui(config, dry_run_override=None)
            return

        config = load_config(config_path)
        config["config_path"] = str(config_path)
        ensure_runtime_paths(config, config_path.parent)
        validate_license(config)

        if args.demo:
            run_demo(config)
            return

        if args.dry_run and args.live:
            raise ValueError("Use either --dry-run or --live, not both.")

        dry_run_override = None
        if args.dry_run:
            dry_run_override = True
        elif args.live:
            dry_run_override = False

        if args.ui:
            if not has_email_credentials(config):
                run_quick_setup(config_path)
                if not config_path.exists():
                    return
                config = load_config(config_path)
                config["config_path"] = str(config_path)
                ensure_runtime_paths(config, config_path.parent)
                validate_license(config)
            run_ui(config, dry_run_override=dry_run_override)
            return

        if not has_email_credentials(config):
            print("Missing email credentials. Run --quick-setup to sign in again.")
            return

        run_agent(
            config,
            max_emails=args.max_emails,
            dry_run_override=dry_run_override,
            daemon=args.daemon,
            poll_interval_override=args.poll_interval,
        )
    except KeyboardInterrupt:
        pass
    except (ConfigError, AlreadyRunningError, credentials.CredentialError, legacy_import.LegacyImportError) as err:
        # Expected, actionable problems: say exactly what to fix, no traceback.
        print(f"Cannot start: {err}")
        append_error_log({"error_log_file": "logs/error.log"}, "startup refused", err)
        raise SystemExit(2)
    except Exception as err:
        fallback_config = {"error_log_file": "logs/error.log"}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("error_log_file"):
                    fallback_config["error_log_file"] = str(raw.get("error_log_file"))
            except Exception:
                pass
        append_error_log(fallback_config, "main failure", err)
        print(f"Fatal error. Details written to {fallback_config['error_log_file']}")


if __name__ == "__main__":
    main()
