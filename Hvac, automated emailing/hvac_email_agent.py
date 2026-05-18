#!/usr/bin/env python3
"""HVAC Automatic Inquiry Response System (CLI MVP)."""

from __future__ import annotations

import argparse
import csv
import getpass
import imaplib
import json
import os
import re
import smtplib
import threading
import time as time_module
import traceback
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import crm, database, notifications, scheduler, setup_wizard, technicians


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
    "email_password": "replace-with-app-password",
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
}

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

DEFAULT_MARKETING_KEYWORDS = [
    "unsubscribe",
    "manage preferences",
    "view in browser",
    "newsletter",
    "welcome",
    "account",
    "security alert",
    "verify",
    "confirm",
    "tips",
    "recommendations",
    "promo",
    "promotion",
    "credits",
    "subscription",
    "update",
    "roadmap",
    "trial",
    "thanks for signing up",
    "get started",
    "privacy policy",
    "terms of service",
    "product manager",
    "user research",
    "customer interview",
    "feedback",
    "quick chat",
    "schedule a call",
    "schedule a chat",
    "book a time",
    "grab a time",
    "time that works",
    "learn more about your experience",
    "special offer",
    "special",
    "discount",
    "coupon",
    "limited time",
    "sale",
    "promo code",
    "deal",
]

DEFAULT_OUTREACH_KEYWORDS = [
    "product manager",
    "user research",
    "customer interview",
    "feedback",
    "quick chat",
    "20 minute",
    "30 minute",
    "20-30 minute",
    "20–30 minute",
    "schedule a call",
    "schedule a chat",
    "book a time",
    "grab a time",
    "time that works",
    "calendar",
    "availability",
    "talk with you",
    "we'd love to learn",
    "we would love to learn",
    "learn more about your experience",
]

PHONE_PATTERN = re.compile(
    r"(?:\+?1[\s\-\.]?)?(?:\(?\d{3}\)?[\s\-\.]?)\d{3}[\s\-\.]?\d{4}",
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
    """Load, merge, and validate runtime config from disk."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw_config = json.load(f)

    config = deep_merge(DEFAULT_CONFIG, raw_config)
    config["office_schedule"] = normalize_office_schedule(config.get("office_schedule"))

    required = [
        "business_name",
        "services",
        "office_hours",
        "phone_number",
        "lead_keywords",
        "existing_keywords",
        "spam_keywords",
        "imap_server",
        "imap_port",
        "smtp_server",
        "smtp_port",
        "email_address",
        "email_password",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Missing required config fields: {', '.join(missing)}")

    return config


def has_email_credentials(config: dict) -> bool:
    """Return True if both email address and app password are present."""
    return bool(str(config.get("email_address", "")).strip()) and bool(str(config.get("email_password", "")).strip())


def load_processed_ids(path: Path) -> set[str]:
    """Read processed message IDs used for de-duplication."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_processed_ids(path: Path, processed_ids: set[str]) -> None:
    """Persist processed message IDs to disk."""
    sorted_ids = sorted(processed_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted_ids) + ("\n" if sorted_ids else ""), encoding="utf-8")


def load_json_dict(path: Path) -> dict:
    """Load a JSON object file, returning an empty dict on missing/invalid content."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_json_dict(path: Path, data: dict) -> None:
    """Write a JSON dictionary to disk with indentation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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


def write_config_files(config: dict, config_path: Path) -> list[Path]:
    """Write config.json and keep root/release configs in sync when present."""
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

    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        ensure_runtime_paths(config, path.parent)
    return targets


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
    for field in path_fields:
        raw = str(config.get(field, "")).strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)


def ensure_csv_headers(path: Path, headers: list[str]) -> None:
    """Ensure a CSV file exists with the expected header row."""
    if path.exists() and path.stat().st_size > 0:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)


def append_dispatch_log(path: Path, content: str) -> None:
    """Append dispatch summary snapshots to a plain text operation log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{utc_now_iso()}]\n{content}\n\n")


def ensure_csv_header(path: Path) -> None:
    """Create the lead CSV file with headers when it does not exist yet."""
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "timestamp",
                "sender_email",
                "detected_name",
                "service_detected",
                "subject",
                "classification",
            ]
        )


def append_csv_row(path: Path, row: list[str]) -> None:
    """Append one lead/event record row to CSV."""
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)
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
    """Count keyword occurrences with a word-boundary first pass and safe fallback."""
    key = keyword.lower().strip()
    if not key:
        return 0
    pattern = r"\b" + re.escape(key) + r"\b"
    matches = re.findall(pattern, text)
    if matches:
        return len(matches)
    return text.count(key)


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
        if any(term in text for term in intent_terms):
            return True
    return False


def is_marketing_email(sender_email: str, subject: str, body: str) -> bool:
    """Detect marketing/newsletter/notification emails."""
    text = normalize_text(f"{subject}\n{body}")
    sender = (sender_email or "").lower()
    if "no-reply" in sender or "noreply" in sender or "mailer-daemon" in sender:
        return True
    outreach_hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_OUTREACH_KEYWORDS)
    if outreach_hits > 0:
        return True
    if any(keyword in text for keyword in ("calendly", "meet.google.com", "zoom.us", "schedule a time")):
        return True
    hits = sum(keyword_hits(text, keyword) for keyword in DEFAULT_MARKETING_KEYWORDS)
    return hits > 0


def has_customer_intent(subject: str, body: str, structured_fields: bool) -> bool:
    """Return True when the message reads like a real customer HVAC request."""
    if structured_fields:
        return True
    if extract_phone(body):
        return True
    if extract_address(body) != "Unknown":
        return True
    text = normalize_text(f"{subject}\n{body}")
    if any(keyword in text for keyword in STRONG_HVAC_PROBLEM_KEYWORDS):
        first_person_tokens = ("my ", "our ", "i ", "i'm", "im ", "we ", "we've", "we have")
        if any(token in text for token in first_person_tokens):
            return True
        # Strong problem statements can still be genuine even without "my".
        return True
    return False


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

    max_score = max(scores.values())
    if max_score == 0:
        classification = "existing"
    else:
        winners = [label for label, score in scores.items() if score == max_score]
        classification = winners[0] if len(winners) == 1 else "existing"

    return ClassificationResult(
        classification=classification,
        lead_score=lead_score,
        existing_score=existing_score,
        spam_score=spam_score,
    )


def resolve_service_name(service: str, configured_services: list[str]) -> str:
    """Map detected service to configured catalog, with AC fallback handling."""
    if service in configured_services:
        return service

    if service == "AC Repair" and "AC Installation" in configured_services:
        return "AC Installation"

    return service


def detect_service(subject: str, body: str, config: dict) -> str:
    """Infer the likely HVAC service from configured mappings and safety fallbacks."""
    text = normalize_text(f"{subject}\n{body}")
    services = config.get("services", [])

    for service in services:
        if service.lower() in text:
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

    if "no heat" in text or "furnace" in text or "heater" in text or "heat not work" in text:
        return "Furnace Repair"

    if "ac not cooling" in text or "air conditioner not cooling" in text:
        if "AC Repair" in services:
            return "AC Repair"
        if "AC Installation" in services:
            return "AC Installation"
        return "AC Repair"

    if "thermostat" in text or "house is freezing" in text or "shows 72" in text:
        return "Thermostat Service"

    if "smells weird" in text or "weird smell" in text or "burning smell" in text:
        return "AC Repair"

    if "heat pump not working" in text or "heat pump" in text:
        return "Heat Pump Repair"

    if "duct" in text:
        return "Duct Cleaning"

    return "Unknown"


def infer_request_type(subject: str, body: str) -> str:
    """Infer whether a message is a quote inquiry or service request."""
    text = normalize_text(f"{subject}\n{body}")
    if "quote" in text or "estimate" in text:
        return "quote"
    if "maintenance" in text or "tune up" in text or "tune-up" in text:
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


def extract_customer_name(body: str, sender_name: str, sender_email: str) -> str:
    """Extract customer name from email body first, then sender name/email fallback."""
    body_for_search = body.replace("\r", "\n")

    for raw_line in body_for_search.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line_match = re.match(r"^name\s*(?:is|maybe)?\s*[:\-]?\s*([a-z][a-z .'-]{0,40})$", line, flags=re.IGNORECASE)
        if line_match:
            candidate = " ".join(line_match.group(1).split())
            return " ".join(part.capitalize() for part in candidate.split())

    patterns = [
        r"(?:my name is|name is|name maybe|this is|i am)[ \t]+([a-z]+(?:[ \t]+[a-z]+){0,3})",
        r"(?:regards|thanks|thank you|sincerely|best),?[ \t]*\n+[ \t]*([a-z]+(?:[ \t]+[a-z]+){0,3})",
    ]
    body_lower = body_for_search.lower()
    for pattern in patterns:
        match = re.search(pattern, body_lower, flags=re.MULTILINE)
        if match:
            candidate = " ".join(match.group(1).split())
            return " ".join(part.capitalize() for part in candidate.split())

    if sender_name:
        return sender_name

    if sender_email:
        return fallback_name_from_email(sender_email)

    return "there"


def extract_address(body: str) -> str:
    """Extract a likely street address from message body text."""
    label_match = re.search(r"^\s*address\s*[:\-]\s*(.+)$", body, flags=re.IGNORECASE | re.MULTILINE)
    if label_match:
        candidate = label_match.group(1).strip()
        if candidate:
            return candidate

    match = ADDRESS_PATTERN.search(body)
    if match:
        return match.group(0).strip()

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^\d{1,6}\s+[A-Za-z0-9 .'-]{4,}$", line):
            return line

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
    if label_hits >= 2:
        return True
    if extract_phone(body) and extract_address(body) != "Unknown":
        return True
    return False


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
    if any(keyword and keyword in text for keyword in high_priority_keywords):
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


def load_dispatch_summary_state(path: Path) -> dict:
    """Load dispatch summary state with safe defaults."""
    data = load_json_dict(path)
    if not isinstance(data, dict):
        return {"last_summary_at": "", "pending": []}
    data.setdefault("last_summary_at", "")
    if not isinstance(data.get("pending"), list):
        data["pending"] = []
    return data


def append_dispatch_pending(dispatch_state: dict, request: dict) -> None:
    """Append a structured request entry to pending dispatch summary queue."""
    dispatch_state.setdefault("pending", [])
    dispatch_state["pending"].append(
        {
            "timestamp": request.get("timestamp", utc_now_iso()),
            "issue": request.get("issue", "HVAC request"),
            "address": request.get("address", "Unknown"),
            "priority": request.get("priority", "Normal"),
            "request_type": request.get("request_type", "service"),
            "service_detected": request.get("service_detected", "Unknown"),
            "ticket_id": request.get("ticket_id", ""),
            "maps_link": request.get("maps_link", ""),
        }
    )


def generate_job_ticket_id(message_id: str) -> str:
    """Generate a deterministic ticket ID from message context and local timestamp."""
    safe_fragment = re.sub(r"[^A-Za-z0-9]", "", message_id)[-6:] or "000000"
    return f"TKT-{system_now().strftime('%Y%m%d-%H%M%S')}-{safe_fragment.upper()}"


def split_first_last_name(customer_name: str) -> tuple[str, str]:
    """Split free-form customer name into first and last components."""
    tokens = [token for token in str(customer_name).strip().split() if token]
    if not tokens:
        return ("", "")
    if len(tokens) == 1:
        return (tokens[0], "")
    return (tokens[0], " ".join(tokens[1:]))


def normalize_ticket_signature(ticket_like: dict) -> tuple[str, str, str, str, str, str, str]:
    """Build duplicate-detection signature for ticket-like records."""
    return (
        normalize_text(str(ticket_like.get("sender_email", ""))),
        normalize_text(str(ticket_like.get("first_name", ""))),
        normalize_text(str(ticket_like.get("last_name", ""))),
        normalize_text(str(ticket_like.get("address", ""))),
        normalize_text(str(ticket_like.get("issue", ""))),
        normalize_text(str(ticket_like.get("service_detected", ""))),
        normalize_text(str(ticket_like.get("request_type", ""))),
    )


def load_csv_rows(path: Path) -> list[dict]:
    """Load CSV rows as dictionaries."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_csv_rows(path: Path, headers: list[str], rows: list[dict]) -> None:
    """Persist CSV dictionary rows with fixed header order."""
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})


def create_job_ticket_from_request(request: dict, message_id: str) -> dict:
    """Create a normalized job ticket payload from extracted request details."""
    first_name, last_name = split_first_last_name(str(request.get("customer_name", "")))
    return {
        "ticket_id": generate_job_ticket_id(message_id),
        "created_at": request.get("timestamp", utc_now_iso()),
        "last_updated_at": request.get("timestamp", utc_now_iso()),
        "occurrence_count": "1",
        "status": "Open",
        "customer_name": request.get("customer_name", "there"),
        "first_name": first_name,
        "last_name": last_name,
        "sender_email": request.get("sender_email", ""),
        "address": request.get("address", "Unknown"),
        "maps_link": request.get("maps_link", ""),
        "issue": request.get("issue", "HVAC inquiry"),
        "priority": request.get("priority", "Normal"),
        "service_detected": request.get("service_detected", "Unknown"),
        "request_type": request.get("request_type", "service"),
        "assigned_technician": "",
    }


def upsert_job_ticket(path: Path, ticket: dict, config: dict) -> tuple[str, str]:
    """Create/update/replace/delete duplicate tickets based on configured strategy."""
    headers = [
        "ticket_id",
        "created_at",
        "last_updated_at",
        "occurrence_count",
        "status",
        "customer_name",
        "first_name",
        "last_name",
        "sender_email",
        "address",
        "maps_link",
        "issue",
        "priority",
        "service_detected",
        "request_type",
        "assigned_technician",
    ]
    mode = normalize_text(str(config.get("ticket_duplicate_mode", "update"))) or "update"
    rows = load_csv_rows(path)
    incoming_sig = normalize_ticket_signature(ticket)
    now_iso = utc_now_iso()

    duplicate_index = -1
    for idx, row in enumerate(rows):
        if normalize_ticket_signature(row) == incoming_sig:
            duplicate_index = idx
            break

    if duplicate_index == -1:
        rows.append(ticket)
        save_csv_rows(path, headers, rows)
        return str(ticket.get("ticket_id", "")), "created"

    existing = rows[duplicate_index]
    existing_count = int(str(existing.get("occurrence_count", "1") or "1"))
    new_count = str(existing_count + 1)

    if mode == "replace":
        replacement = dict(ticket)
        replacement["occurrence_count"] = new_count
        replacement["last_updated_at"] = now_iso
        rows[duplicate_index] = replacement
        save_csv_rows(path, headers, rows)
        return str(replacement.get("ticket_id", "")), "replaced"

    if mode == "delete":
        # Keep original ticket and silently ignore creating duplicate record.
        existing["occurrence_count"] = new_count
        existing["last_updated_at"] = now_iso
        rows[duplicate_index] = existing
        save_csv_rows(path, headers, rows)
        return str(existing.get("ticket_id", "")), "ignored-duplicate"

    # Default: update existing ticket record.
    existing["last_updated_at"] = now_iso
    existing["occurrence_count"] = new_count
    for key in ["address", "maps_link", "issue", "priority", "service_detected", "request_type"]:
        existing[key] = ticket.get(key, existing.get(key, ""))
    rows[duplicate_index] = existing
    save_csv_rows(path, headers, rows)
    return str(existing.get("ticket_id", "")), "updated"


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
        lines.append(f"{idx}. {ticket_segment}[{priority}] {issue} - {address} ({request_type})")
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


def should_skip_auto_reply(sender_email: str, msg: Message, own_email: str) -> bool:
    """Suppress auto-replies for self, no-reply, bulk, and auto-submitted messages."""
    lower_sender = sender_email.lower()
    if not lower_sender:
        return True
    if lower_sender == own_email.lower():
        return True
    if any(tag in lower_sender for tag in ["noreply", "no-reply", "donotreply", "do-not-reply"]):
        return True

    auto_submitted = (msg.get("Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return True

    precedence = (msg.get("Precedence") or "").lower()
    if precedence in {"bulk", "junk", "list"}:
        return True

    return False

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


def build_ack_message(
    config: dict,
    recipient_email: str,
    recipient_name: str,
    in_office_hours: bool,
    request_details: dict | None = None,
) -> EmailMessage:
    """Build lead acknowledgment message with in-hours/after-hours branch."""
    business_name = config["business_name"]
    office_hours = config["office_hours"]
    phone_number = config["phone_number"]

    reply = EmailMessage()
    reply["Subject"] = f"Re: Thanks for contacting {business_name}"
    reply["From"] = config["email_address"]
    reply["To"] = recipient_email
    reply["X-HVAC-Auto"] = "true"
    reply["X-HVAC-Auto-Type"] = "ack_in_hours" if in_office_hours else "ack_after_hours"

    if in_office_hours:
        timing_line = f"We've received your message and a member of our team will review it during business hours ({office_hours})."
    else:
        timing_line = (
            "We've received your message and our team will respond during the next business day "
            f"({office_hours})."
        )

    request_block = ""
    if request_details:
        issue = str(request_details.get("issue", "")).strip()
        address = str(request_details.get("address", "")).strip()
        maps_link = str(request_details.get("maps_link", "")).strip()
        service = str(request_details.get("service_detected", "")).strip()
        ticket_id = str(request_details.get("ticket_id", "")).strip()
        pieces = []
        if ticket_id:
            pieces.append(f"Ticket ID: {ticket_id}")
        if service and service != "Unknown":
            pieces.append(f"Service type: {service}")
        if issue:
            pieces.append(f"Issue noted: {issue}")
        if address and address != "Unknown":
            pieces.append(f"Location: {address}")
        if maps_link:
            pieces.append(f"Map link: {maps_link}")
        if pieces:
            request_block = "\n".join(pieces) + "\n\n"

    body = (
        f"Hi {safe_recipient_name(recipient_name)},\n\n"
        "Thank you for reaching out regarding your HVAC inquiry.\n\n"
        f"{request_block}"
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
    reply["X-HVAC-Auto"] = "true"
    reply["X-HVAC-Auto-Type"] = "follow_up"

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


def send_reply(config: dict, message: EmailMessage) -> None:
    """Send email via SMTP (implicit TLS on 465, STARTTLS otherwise)."""
    smtp_server = config["smtp_server"]
    smtp_port = int(config["smtp_port"])
    username = config["email_address"]
    password = config["email_password"]

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)


def fetch_message_ids(imap_conn: imaplib.IMAP4_SSL, unseen_only: bool) -> list[bytes]:
    """Fetch message sequence numbers for target mailbox selection criteria."""
    criteria = "UNSEEN" if unseen_only else "ALL"
    status, data = imap_conn.search(None, criteria)
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def select_mailbox(imap_conn: imaplib.IMAP4_SSL, mailbox: str, readonly: bool = False) -> bool:
    """Select an IMAP mailbox and return whether selection succeeded."""
    status, _ = imap_conn.select(mailbox, readonly=readonly)
    return status == "OK"


def iter_messages(imap_conn: imaplib.IMAP4_SSL, message_nums: Iterable[bytes]):
    """Yield decoded message objects for IMAP sequence numbers."""
    for num in message_nums:
        status, data = imap_conn.fetch(num, "(RFC822)")
        if status != "OK" or not data:
            continue

        raw = None
        for chunk in data:
            if isinstance(chunk, tuple) and len(chunk) > 1:
                raw = chunk[1]
                break

        if not raw:
            continue

        msg = message_from_bytes(raw, policy=default)
        yield num.decode("utf-8", errors="ignore"), msg


def filter_out_promotions(imap_conn: imaplib.IMAP4_SSL, message_nums: list[bytes]) -> list[bytes]:
    """Remove Gmail promotions/social/category messages if configured."""
    filtered: list[bytes] = []
    for msg_num in message_nums:
        try:
            status, data = imap_conn.fetch(msg_num, "(BODY.PEEK[HEADER.FIELDS (X-GM-LABELS)])")
        except imaplib.IMAP4.error:
            filtered.append(msg_num)
            continue
        if status != "OK" or not data or not data[0]:
            filtered.append(msg_num)
            continue
        header_blob = data[0][1].decode(errors="ignore").lower()
        if "x-gm-labels" in header_blob and (
            "\\promotions" in header_blob or "promotions" in header_blob or "\\social" in header_blob or "social" in header_blob
        ):
            continue
        filtered.append(msg_num)
    return filtered

def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse ISO timestamp and normalize to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def in_sender_cooldown(sender_email: str, sender_cooldown: dict, now_utc: datetime, cooldown_hours: int) -> bool:
    """Return True when sender is still inside reply cooldown window."""
    last_reply_iso = sender_cooldown.get(sender_email)
    last_reply = parse_iso_datetime(last_reply_iso)
    if not last_reply:
        return False
    return now_utc < (last_reply + timedelta(hours=cooldown_hours))


def register_follow_up_record(
    follow_up_state: dict,
    message_id: str,
    sender_email: str,
    detected_name: str,
    subject: str,
    now_utc: datetime,
    delay_hours: int,
) -> None:
    """Store follow-up tracking metadata for a newly detected lead message."""
    if message_id in follow_up_state:
        return

    follow_up_state[message_id] = {
        "sender_email": sender_email,
        "detected_name": detected_name,
        "subject": subject,
        "lead_timestamp": now_utc.isoformat(),
        "follow_up_due_at": (now_utc + timedelta(hours=delay_hours)).isoformat(),
        "follow_up_sent": False,
        "follow_up_sent_at": None,
        "manual_reply_detected": False,
        "manual_reply_at": None,
    }


def resolve_sent_mailbox(imap_conn: imaplib.IMAP4_SSL, config: dict) -> str | None:
    """Find a usable sent-mailbox folder from configured and common provider names."""
    candidates = [
        config.get("sent_mailbox", ""),
        "Sent",
        "Sent Items",
        "INBOX.Sent",
        "[Gmail]/Sent Mail",
    ]
    seen: set[str] = set()

    for mailbox in candidates:
        candidate = str(mailbox).strip()
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        if select_mailbox(imap_conn, candidate, readonly=True):
            return candidate

    return None


def message_datetime_utc(msg: Message) -> datetime | None:
    """Read and normalize message Date header to UTC."""
    raw_date = msg.get("Date")
    if not raw_date:
        return None

    try:
        sent_dt = parsedate_to_datetime(raw_date)
    except Exception:
        return None

    if sent_dt.tzinfo is None:
        sent_dt = sent_dt.replace(tzinfo=timezone.utc)

    return sent_dt.astimezone(timezone.utc)


def fetch_manual_reply_times(
    imap_conn: imaplib.IMAP4_SSL,
    config: dict,
    since_utc: datetime,
) -> dict[str, datetime]:
    """Map recipient emails to latest manual sent-reply timestamp from sent mailbox."""
    manual_reply_times: dict[str, datetime] = {}
    inbox_mailbox = config.get("mailbox", "INBOX")

    sent_mailbox = resolve_sent_mailbox(imap_conn, config)
    if not sent_mailbox:
        select_mailbox(imap_conn, inbox_mailbox)
        return manual_reply_times

    since_key = since_utc.strftime("%d-%b-%Y")
    status, data = imap_conn.search(None, "SINCE", since_key)
    if status != "OK" or not data or not data[0]:
        select_mailbox(imap_conn, inbox_mailbox)
        return manual_reply_times

    own_email = config["email_address"].lower()

    for _, msg in iter_messages(imap_conn, data[0].split()):
        from_email = parseaddr(msg.get("From", ""))[1].strip().lower()
        if from_email != own_email:
            continue

        if (msg.get("X-HVAC-Auto") or "").strip().lower() == "true":
            continue

        sent_at = message_datetime_utc(msg)
        if not sent_at or sent_at < since_utc:
            continue

        recipients = [
            addr.strip().lower()
            for _, addr in getaddresses([msg.get("To", ""), msg.get("Cc", "")])
            if addr.strip()
        ]

        for recipient in recipients:
            previous = manual_reply_times.get(recipient)
            if not previous or sent_at > previous:
                manual_reply_times[recipient] = sent_at

    select_mailbox(imap_conn, inbox_mailbox)
    return manual_reply_times


def process_follow_up_queue(
    imap_conn: imaplib.IMAP4_SSL,
    config: dict,
    follow_up_state: dict,
    sender_cooldown: dict,
    dry_run: bool,
) -> int:
    """Send due follow-up reminders when no manual reply is detected."""
    if not config.get("follow_up_enabled", False):
        return 0

    pending: list[tuple[str, dict]] = []
    earliest_lead_time: datetime | None = None

    for lead_id, record in follow_up_state.items():
        if not isinstance(record, dict):
            continue
        if record.get("follow_up_sent") or record.get("manual_reply_detected"):
            continue

        lead_time = parse_iso_datetime(record.get("lead_timestamp"))
        if lead_time and (earliest_lead_time is None or lead_time < earliest_lead_time):
            earliest_lead_time = lead_time

        pending.append((lead_id, record))

    if not pending:
        return 0

    now_utc = datetime.now(timezone.utc)
    manual_reply_times = {}
    if earliest_lead_time is not None:
        manual_reply_times = fetch_manual_reply_times(imap_conn, config, earliest_lead_time)

    cooldown_hours = int(config.get("auto_reply_cooldown_hours", 24))
    follow_ups_sent = 0

    for _, record in pending:
        sender_email = str(record.get("sender_email", "")).strip().lower()
        lead_time = parse_iso_datetime(record.get("lead_timestamp"))

        if sender_email and lead_time:
            manual_reply_at = manual_reply_times.get(sender_email)
            if manual_reply_at and manual_reply_at >= lead_time:
                record["manual_reply_detected"] = True
                record["manual_reply_at"] = manual_reply_at.isoformat()
                continue

        due_at = parse_iso_datetime(record.get("follow_up_due_at"))
        if not due_at or now_utc < due_at:
            continue

        if not sender_email:
            record["manual_reply_detected"] = True
            record["manual_reply_at"] = now_utc.isoformat()
            continue

        if in_sender_cooldown(sender_email, sender_cooldown, now_utc, cooldown_hours):
            continue

        recipient_name = str(record.get("detected_name", "")).strip()
        follow_up_message = build_follow_up_message(config, sender_email, recipient_name)

        if dry_run:
            print(f"[DRY RUN] Would send follow-up to {sender_email}")
        else:
            send_reply(config, follow_up_message)
            print(f"Sent follow-up to {sender_email}")

        sender_cooldown[sender_email] = now_utc.isoformat()
        record["follow_up_sent"] = True
        record["follow_up_sent_at"] = now_utc.isoformat()
        follow_ups_sent += 1

    return follow_ups_sent

def process_inbox(config: dict, max_emails: int | None = None, dry_run_override: bool | None = None) -> None:
    """Execute one full inbox processing cycle (classify, log, reply, persist state)."""
    processed_path = Path(config["processed_ids_file"])
    csv_path = Path(config["leads_csv_file"])
    service_requests_path = Path(str(config.get("service_requests_file", "service_requests.csv")))
    draft_replies_path = Path(str(config.get("draft_replies_file", "draft_replies.csv")))
    job_tickets_path = Path(str(config.get("job_tickets_file", "job_tickets.csv")))
    db_path = Path(str(config.get("db_path", "data/hvac_agent.db")))
    sender_cooldown_path = Path(config["sender_cooldown_file"])
    follow_up_state_path = Path(config["follow_up_state_file"])
    dispatch_state_path = Path(str(config.get("dispatch_summary_state_file", "dispatch_summary_state.json")))

    processed_ids = load_processed_ids(processed_path)
    sender_cooldown = load_json_dict(sender_cooldown_path)
    follow_up_state = load_json_dict(follow_up_state_path)
    dispatch_state = load_dispatch_summary_state(dispatch_state_path)
    ensure_csv_header(csv_path)
    ensure_csv_headers(
        service_requests_path,
        [
            "timestamp",
            "sender_email",
            "customer_name",
            "first_name",
            "last_name",
            "phone",
            "address",
            "maps_link",
            "issue",
            "priority",
            "request_type",
            "service_detected",
            "subject",
        ],
    )
    ensure_csv_headers(
        draft_replies_path,
        [
            "timestamp",
            "sender_email",
            "draft_subject",
            "draft_body",
            "customer_name",
            "phone",
            "address",
            "maps_link",
            "service_detected",
            "priority",
        ],
    )
    ensure_csv_headers(
        job_tickets_path,
        [
            "ticket_id",
            "created_at",
            "last_updated_at",
            "occurrence_count",
            "status",
            "customer_name",
            "first_name",
            "last_name",
            "sender_email",
            "address",
            "maps_link",
            "issue",
            "priority",
            "service_detected",
            "request_type",
            "assigned_technician",
        ],
    )

    dry_run = config.get("dry_run", True) if dry_run_override is None else dry_run_override
    database.init_db(db_path)

    with imaplib.IMAP4_SSL(config["imap_server"], int(config["imap_port"])) as imap_conn:
        imap_conn.login(config["email_address"], config["email_password"])
        mailbox_name = config.get("mailbox", "INBOX")
        if config.get("ignore_promotions", True):
            mailbox_name = "INBOX"
        if not select_mailbox(imap_conn, mailbox_name):
            raise RuntimeError(f"Could not select mailbox: {mailbox_name}")

        use_email_followups = (
            bool(config.get("follow_up_enabled", False))
            and bool(config.get("feature_follow_up", True))
            and not bool(config.get("feature_ticketing", True))
        )
        follow_ups_sent = (
            process_follow_up_queue(imap_conn, config, follow_up_state, sender_cooldown, dry_run)
            if use_email_followups
            else 0
        )

        message_nums = fetch_message_ids(imap_conn, unseen_only=bool(config.get("read_unseen_only", True)))
        if config.get("ignore_promotions", True):
            message_nums = filter_out_promotions(imap_conn, message_nums)
        if max_emails is not None:
            message_nums = message_nums[: max(0, max_emails)]

        print(f"Found {len(message_nums)} email(s) to inspect.")

        processed_count = 0
        auto_replied_count = 0
        dispatch_summary_sent_items = 0
        cooldown_hours = int(config.get("auto_reply_cooldown_hours", 24))

        for msg_num, msg in iter_messages(imap_conn, message_nums):
            message_id = (msg.get("Message-ID") or f"imap-msg-{msg_num}").strip()
            if message_id in processed_ids:
                continue

            subject = clean_subject(msg.get("Subject"))
            body = extract_plain_text(msg)
            detected_name, sender_email = extract_sender(msg)

            result = classify(subject, body, config)
            service = detect_service(subject, body, config)
            hvac_related = is_hvac_related(subject, body, service)
            structured_fields = has_structured_fields(body)
            marketing_email = is_marketing_email(sender_email, subject, body)
            customer_intent = has_customer_intent(subject, body, structured_fields)
            if marketing_email and not customer_intent:
                result = ClassificationResult(
                    classification="spam",
                    lead_score=result.lead_score,
                    existing_score=result.existing_score,
                    spam_score=max(result.spam_score, result.lead_score + 5),
                )
            elif not hvac_related:
                result = ClassificationResult(
                    classification="existing",
                    lead_score=result.lead_score,
                    existing_score=max(result.existing_score, 1),
                    spam_score=result.spam_score,
                )
            elif (
                result.classification != "lead"
                and result.classification != "spam"
                and structured_fields
                and not marketing_email
            ):
                result = ClassificationResult(
                    classification="lead",
                    lead_score=max(result.lead_score, result.existing_score + 1, 1),
                    existing_score=result.existing_score,
                    spam_score=result.spam_score,
                )
                log_event(
                    config,
                    "lead_forced",
                    sender_email=sender_email,
                    subject=subject,
                    reason="structured_fields",
                )

            now_utc = datetime.now(timezone.utc)
            timestamp = utc_now_iso()
            append_csv_row(
                csv_path,
                [
                    timestamp,
                    sender_email,
                    detected_name,
                    service,
                    subject,
                    result.classification,
                ],
            )
            log_event(
                config,
                "email_processed",
                sender_email=sender_email,
                subject=subject,
                classification=result.classification,
            )
            if result.classification != "lead":
                log_event(
                    config,
                    "lead_skipped",
                    sender_email=sender_email,
                    subject=subject,
                    classification=result.classification,
                    hvac_related=str(hvac_related),
                    structured_fields=str(structured_fields),
                    marketing_email=str(marketing_email),
                    customer_intent=str(customer_intent),
                )

            should_reply = (
                config.get("feature_auto_reply", True)
                and config.get("auto_reply_enabled", True)
                and result.classification == "lead"
                and not should_skip_auto_reply(sender_email, msg, config["email_address"])
            )

            extracted_request: dict | None = None
            ticket_action = ""
            ticket_id = ""
            job_action = ""
            job_id = ""
            if result.classification == "lead":
                extracted_request = extract_service_request(
                    subject,
                    body,
                    detected_name,
                    sender_email,
                    service,
                    config,
                )
                log_event(
                    config,
                    "lead_parsed",
                    sender_email=sender_email,
                    subject=subject,
                    customer_name=str(extracted_request.get("customer_name", "")),
                    phone=str(extracted_request.get("phone", "")),
                    address=str(extracted_request.get("address", "")),
                    service_detected=str(extracted_request.get("service_detected", "")),
                )

                if config.get("feature_ticketing", True):
                    append_csv_row(
                        service_requests_path,
                        [
                            extracted_request["timestamp"],
                            extracted_request["sender_email"],
                            extracted_request["customer_name"],
                            extracted_request["first_name"],
                            extracted_request["last_name"],
                            extracted_request["phone"],
                            extracted_request["address"],
                            extracted_request["maps_link"],
                            extracted_request["issue"],
                            extracted_request["priority"],
                            extracted_request["request_type"],
                            extracted_request["service_detected"],
                            extracted_request["subject"],
                        ],
                    )

                    job_ticket = create_job_ticket_from_request(extracted_request, message_id)
                    ticket_id, ticket_action = upsert_job_ticket(job_tickets_path, job_ticket, config)
                    extracted_request["ticket_id"] = ticket_id
                    if ticket_action and ticket_action != "created":
                        print(f"Ticket {ticket_id} {ticket_action} (duplicate policy).")
                    elif ticket_action == "created":
                        log_event(config, "ticket_created", ticket_id=ticket_id, sender_email=sender_email)

                    existing_customer = crm.get_customer_by_email(db_path, extracted_request.get("sender_email", ""))
                    incoming_name = normalize_text(
                        f"{extracted_request.get('first_name', '')} {extracted_request.get('last_name', '')}".strip()
                    )
                    existing_name = ""
                    if existing_customer:
                        existing_name = normalize_text(
                            f"{existing_customer.get('first_name', '')} {existing_customer.get('last_name', '')}".strip()
                        )
                    name_matches = not (existing_name and incoming_name and existing_name != incoming_name)

                    customer_id = crm.upsert_customer(
                        db_path,
                        extracted_request.get("first_name", ""),
                        extracted_request.get("last_name", ""),
                        extracted_request.get("sender_email", ""),
                        extracted_request.get("phone", ""),
                        extracted_request.get("address", ""),
                    )
                    duplicate_job = None
                    if name_matches:
                        duplicate_job = crm.find_duplicate_job(
                            db_path,
                            customer_id,
                            extracted_request.get("issue", ""),
                            extracted_request.get("address", ""),
                            extracted_request.get("service_detected", ""),
                        )
                    duplicate_mode = normalize_text(str(config.get("ticket_duplicate_mode", "update"))) or "update"
                    if duplicate_job and duplicate_mode in {"update", "replace", "delete"}:
                        crm.update_job_duplicate(
                            db_path,
                            int(duplicate_job["job_id"]),
                            extracted_request.get("issue", ""),
                            extracted_request.get("priority", "Normal"),
                            extracted_request.get("service_detected", ""),
                            extracted_request.get("address", ""),
                            extracted_request.get("first_name", ""),
                            extracted_request.get("last_name", ""),
                            extracted_request.get("phone", ""),
                        )
                        job_id = str(duplicate_job["job_id"])
                        if duplicate_mode == "delete":
                            job_action = "ignored-duplicate"
                        elif duplicate_mode == "replace":
                            job_action = "replaced"
                        else:
                            job_action = "updated"
                    else:
                        job_id = str(
                            crm.create_job(
                                db_path,
                                customer_id,
                                extracted_request.get("first_name", ""),
                                extracted_request.get("last_name", ""),
                                extracted_request.get("phone", ""),
                                extracted_request.get("service_detected", ""),
                                extracted_request.get("issue", ""),
                                extracted_request.get("priority", "Normal"),
                                "new",
                                extracted_request.get("timestamp", utc_now_iso()),
                                "",
                                "",
                                extracted_request.get("timestamp", utc_now_iso()),
                                1,
                                extracted_request.get("address", ""),
                            )
                        )
                        job_action = "created"

                    if job_action == "created":
                        log_event(config, "job_created", job_id=job_id, sender_email=sender_email)

                in_hours_preview = is_within_office_hours(config, now_utc)
                draft_subject, draft_body = generate_draft_reply(config, extracted_request, in_hours_preview)
                append_csv_row(
                    draft_replies_path,
                    [
                        extracted_request["timestamp"],
                        extracted_request["sender_email"],
                        draft_subject,
                        draft_body,
                        extracted_request["customer_name"],
                        extracted_request["phone"],
                        extracted_request["address"],
                        extracted_request["maps_link"],
                        extracted_request["service_detected"],
                        extracted_request["priority"],
                    ],
                )
                if config.get("feature_dispatch_summary", True):
                    if not config.get("feature_ticketing", True):
                        append_dispatch_pending(dispatch_state, extracted_request)
                    elif ticket_action in {"created", "replaced"} or job_action == "created":
                        append_dispatch_pending(dispatch_state, extracted_request)

                if config.get("sms_enabled", False) and config.get("feature_sms_notifications", False):
                    gateway = str(config.get("sms_gateway_email", "")).strip()
                    if gateway:
                        sms_body = (
                            "NEW HVAC REQUEST\n"
                            f"Customer: {extracted_request.get('customer_name', '')}\n"
                            f"Issue: {extracted_request.get('issue', '')}\n"
                            f"Address: {extracted_request.get('address', '')}\n"
                            f"Priority: {extracted_request.get('priority', '')}\n"
                            f"Job ID: {job_id}"
                        )
                        if not dry_run:
                            notifications.send_sms_via_gateway(
                                config["smtp_server"],
                                int(config["smtp_port"]),
                                config["email_address"],
                                config["email_password"],
                                gateway,
                                sms_body,
                            )
                        else:
                            print(f"[DRY RUN] Would send SMS alert to {gateway}.")

            if result.classification == "lead" and use_email_followups:
                follow_up_name = detected_name
                if extracted_request and extracted_request.get("customer_name"):
                    follow_up_name = str(extracted_request.get("customer_name"))
                register_follow_up_record(
                    follow_up_state,
                    message_id,
                    sender_email,
                    follow_up_name,
                    subject,
                    now_utc,
                    int(config.get("follow_up_delay_hours", 24)),
                )

            if should_reply:
                if in_sender_cooldown(sender_email, sender_cooldown, now_utc, cooldown_hours):
                    print(f"Skipped auto-reply to {sender_email}: cooldown active ({cooldown_hours}h).")
                else:
                    in_hours = is_within_office_hours(config, now_utc)
                    recipient_name = detected_name
                    if extracted_request and extracted_request.get("customer_name"):
                        recipient_name = str(extracted_request.get("customer_name"))
                    reply = build_ack_message(
                        config,
                        sender_email,
                        recipient_name,
                        in_hours,
                        request_details=extracted_request,
                    )
                    reply_mode = "in-hours" if in_hours else "after-hours"
                    if dry_run:
                        print(f"[DRY RUN] Would send {reply_mode} auto-reply to {sender_email} | Subject: {subject}")
                    else:
                        send_reply(config, reply)
                        print(f"Sent {reply_mode} auto-reply to {sender_email} | Subject: {subject}")
                        log_event(config, "auto_reply_sent", sender_email=sender_email, mode=reply_mode)

                    sender_cooldown[sender_email] = now_utc.isoformat()
                    auto_replied_count += 1

            print(
                f"Processed {sender_email or '<unknown>'} | class={result.classification} "
                f"(lead={result.lead_score}, existing={result.existing_score}, spam={result.spam_score})"
            )

            processed_ids.add(message_id)
            processed_count += 1

        dispatch_summary_sent_items = process_dispatch_summary(config, dispatch_state, dry_run)
        job_followups_sent = process_job_followups(config, db_path, dry_run)
        save_processed_ids(processed_path, processed_ids)
        save_json_dict(sender_cooldown_path, sender_cooldown)
        save_json_dict(follow_up_state_path, follow_up_state)
        save_json_dict(dispatch_state_path, dispatch_state)

        print(
            f"Done. Processed: {processed_count}, Auto-replies: {auto_replied_count}, "
            f"Follow-ups: {follow_ups_sent}, Job-followups: {job_followups_sent}, "
            f"Dispatch-items: {dispatch_summary_sent_items}, Dry-run: {dry_run}"
        )


def process_dispatch_summary(config: dict, dispatch_state: dict, dry_run: bool) -> int:
    """Send periodic dispatch summaries for newly extracted service requests."""
    if not bool(config.get("dispatch_summary_enabled", True)) or not bool(config.get("feature_dispatch_summary", True)):
        return 0

    pending = dispatch_state.get("pending", [])
    if not pending:
        return 0

    interval_minutes = int(config.get("dispatch_summary_interval_minutes", 60))
    now_utc = datetime.now(timezone.utc)
    last_summary_at = parse_iso_datetime(dispatch_state.get("last_summary_at"))
    if last_summary_at and now_utc < (last_summary_at + timedelta(minutes=interval_minutes)):
        return 0

    recipient = str(config.get("dispatch_summary_recipient", config.get("email_address", ""))).strip()
    if not recipient:
        return 0

    body = build_dispatch_summary_body(pending)
    message = EmailMessage()
    message["Subject"] = f"Dispatch Summary - {system_now().strftime('%Y-%m-%d %H:%M %z')}"
    message["From"] = config["email_address"]
    message["To"] = recipient
    message["X-HVAC-Auto"] = "true"
    message["X-HVAC-Auto-Type"] = "dispatch_summary"
    message.set_content(body)

    if dry_run:
        print(f"[DRY RUN] Would send dispatch summary to {recipient} with {len(pending)} item(s).")
    else:
        send_reply(config, message)
        print(f"Sent dispatch summary to {recipient} with {len(pending)} item(s).")

    append_dispatch_log(Path(str(config.get("dispatch_summary_log_file", "dispatch_summary.log"))), body)
    dispatch_state["last_summary_at"] = now_utc.isoformat()
    dispatch_state["pending"] = []
    return len(pending)


def process_job_followups(config: dict, db_path: Path, dry_run: bool) -> int:
    """Send follow-up emails for jobs not scheduled after threshold."""
    if (
        not config.get("feature_follow_up", True)
        or not config.get("follow_up_enabled", False)
        or not config.get("feature_ticketing", True)
    ):
        return 0

    delay_hours = int(config.get("follow_up_delay_hours", 24))
    threshold = system_now() - timedelta(hours=delay_hours)
    sent = 0

    conn = database.get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT j.job_id, j.created_at, j.followup_sent_at, c.email, c.first_name, c.phone
            FROM jobs j
            JOIN customers c ON c.customer_id = j.customer_id
            WHERE j.status = 'new'
              AND (j.followup_sent_at IS NULL OR j.followup_sent_at = '')
            """
        ).fetchall()
        for row in rows:
            created_at = row.get("created_at") or ""
            try:
                created_dt = datetime.fromisoformat(created_at)
            except ValueError:
                continue
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=system_now().tzinfo)
            if created_dt > threshold:
                continue

            recipient = str(row.get("email", "")).strip()
            if not recipient:
                continue

            name = str(row.get("first_name", "") or "there").strip()
            follow_up = build_follow_up_message(config, recipient, name)
            if dry_run:
                print(f"[DRY RUN] Would send scheduling follow-up to {recipient}")
            else:
                send_reply(config, follow_up)
            conn.execute(
                "UPDATE jobs SET followup_sent_at = ? WHERE job_id = ?",
                (utc_now_iso(), row.get("job_id")),
            )
            conn.commit()
            sent += 1
    finally:
        conn.close()

    return sent


def calculate_dashboard_metrics(config: dict) -> dict[str, int]:
    """Calculate lightweight dashboard counters from CSV outputs."""
    leads_path = Path(str(config.get("leads_csv_file", "data/hvac_leads.csv")))
    service_path = Path(str(config.get("service_requests_file", "data/service_requests.csv")))
    tickets_path = Path(str(config.get("job_tickets_file", "job_tickets.csv")))
    db_path = Path(str(config.get("db_path", "data/hvac_agent.db")))
    today = datetime.now(timezone.utc).date()
    database.init_db(db_path)

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

    if leads_path.exists() and leads_path.stat().st_size > 0:
        with leads_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                parsed_ts = parse_iso_datetime(row.get("timestamp"))
                if not parsed_ts or parsed_ts.date() != today:
                    continue
                metrics["emails_processed_today"] += 1
                if str(row.get("classification", "")).strip().lower() == "spam":
                    metrics["spam_today"] += 1

    if service_path.exists() and service_path.stat().st_size > 0:
        with service_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                parsed_ts = parse_iso_datetime(row.get("timestamp"))
                if not parsed_ts or parsed_ts.date() != today:
                    continue
                req_type = str(row.get("request_type", "")).strip().lower()
                if req_type == "quote":
                    metrics["quotes_today"] += 1
                else:
                    metrics["service_requests_today"] += 1

    if db_path.exists():
        conn = database.get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT created_at, scheduled_time, status, priority FROM jobs"
            ).fetchall()
            response_minutes: list[int] = []
            for row in rows:
                created_at = row.get("created_at") or ""
                status = str(row.get("status", "")).lower()
                priority = str(row.get("priority", "")).lower()
                try:
                    created_dt = datetime.fromisoformat(created_at)
                except ValueError:
                    continue
                if created_dt.date() == today and status == "completed":
                    metrics["jobs_completed_today"] += 1
                if status in {"new", "scheduled", "in progress"}:
                    metrics["open_tickets"] += 1
                    if priority == "high":
                        metrics["high_priority_open_tickets"] += 1
                week_start = (system_now().date() - timedelta(days=system_now().weekday()))
                if created_dt.date() >= week_start:
                    metrics["leads_this_week"] += 1
                scheduled_time = row.get("scheduled_time") or ""
                if scheduled_time:
                    try:
                        scheduled_dt = datetime.fromisoformat(scheduled_time)
                        response_minutes.append(int((scheduled_dt - created_dt).total_seconds() // 60))
                    except ValueError:
                        pass
            if response_minutes:
                metrics["avg_response_minutes"] = int(sum(response_minutes) / len(response_minutes))
        finally:
            conn.close()
    elif tickets_path.exists() and tickets_path.stat().st_size > 0:
        with tickets_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                status = str(row.get("status", "")).strip().lower()
                if status != "open":
                    continue
                metrics["open_tickets"] += 1
                if str(row.get("priority", "")).strip().lower() == "high":
                    metrics["high_priority_open_tickets"] += 1

    return metrics


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
    database.init_db(db_path)

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
        runtime_state["status"] = "Running"
        while not stop_event.is_set():
            try:
                if not has_email_credentials(config):
                    runtime_state["status"] = "Needs sign-in"
                    stop_event.wait(poll_interval)
                    continue
                process_inbox(config, dry_run_override=dry_run)
                runtime_state["last_checked"] = system_now().strftime("%Y-%m-%d %H:%M")
                runtime_state["status"] = "Running OK"
            except Exception as err:
                append_error_log(config, "ui worker cycle failure", err)
                runtime_state["status"] = "Error (check error.log)"
            stop_event.wait(poll_interval)
        runtime_state["status"] = "Stopped"

    def start_agent() -> None:
        thread = worker_ref.get("thread")
        if thread and thread.is_alive():
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

    def save_feature_toggles() -> None:
        auto_reply_enabled = bool(feature_auto_reply.get())
        config["feature_auto_reply"] = auto_reply_enabled
        config["auto_reply_enabled"] = auto_reply_enabled
        config["feature_ticketing"] = bool(feature_ticketing.get())
        config["feature_dispatch_summary"] = bool(feature_dispatch.get())
        sms_enabled = bool(feature_sms.get())
        config["feature_sms_notifications"] = sms_enabled
        config["sms_enabled"] = sms_enabled
        followup_enabled = bool(feature_followup.get())
        config["feature_follow_up"] = followup_enabled
        config["follow_up_enabled"] = followup_enabled
        Path(config.get("config_path", "config.json")).write_text(json.dumps(config, indent=2), encoding="utf-8")
        # No popup; auto-save silently.

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
        stop_event.set()
        runtime_state["status"] = "Stopped"
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

        for path in data_paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                continue

        database.init_db(db_path)
        refresh_ticket_cards(force=True)
        refresh_history_tree()

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


def run_agent(
    config: dict,
    max_emails: int | None = None,
    dry_run_override: bool | None = None,
    daemon: bool = False,
    poll_interval_override: int | None = None,
) -> None:
    """Run one-shot or daemonized agent loop with heartbeat and error capture."""
    dry_run = config.get("dry_run", True) if dry_run_override is None else dry_run_override
    poll_interval = int(config.get("poll_interval_seconds", 30))
    if poll_interval_override is not None:
        poll_interval = max(5, poll_interval_override)

    heartbeat_minutes = int(config.get("heartbeat_interval_minutes", 30))
    next_heartbeat: datetime | None = None
    if daemon and heartbeat_minutes > 0:
        next_heartbeat = datetime.now(timezone.utc) + timedelta(minutes=heartbeat_minutes)

    mode = "daemon" if daemon else "one-shot"
    print(f"Agent started at {utc_now_iso()} | mode={mode} | dry_run={dry_run}")

    while True:
        try:
            process_inbox(config, max_emails=max_emails, dry_run_override=dry_run_override)
        except KeyboardInterrupt:
            print("Agent stopped by user.")
            raise
        except Exception as err:
            append_error_log(config, "process_inbox failure", err)
            print(f"Error encountered. Details written to {config.get('error_log_file', 'error.log')}")
            log_event(config, "error", context="process_inbox", error=str(err))

        if not daemon:
            return

        now_utc = datetime.now(timezone.utc)
        if next_heartbeat and now_utc >= next_heartbeat:
            print(f"Agent heartbeat at {utc_now_iso()} | waiting for next poll")
            next_heartbeat = now_utc + timedelta(minutes=heartbeat_minutes)

        time_module.sleep(max(5, poll_interval))


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

    current_password = str(config.get("email_password", ""))
    password_hint = "(hidden; press Enter to keep current)"
    password_value = getpass.getpass(f"Email app password {password_hint}: ").replace(" ", "").strip()
    if password_value:
        config["email_password"] = password_value
    elif not current_password:
        config["email_password"] = "replace-with-app-password"

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
        config["email_password"] = app_password
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
            with imaplib.IMAP4_SSL(config["imap_server"], int(config["imap_port"])) as imap_conn:
                imap_conn.login(config["email_address"], config["email_password"])
        except imaplib.IMAP4.error:
            messagebox.showerror("Wrong Password", "Could not sign in. Check Gmail and app password.")
            return
        except Exception:
            messagebox.showerror("Connection Error", "Could not reach Gmail. Check your internet connection.")
            return

        targets = write_config_files(config, config_path)
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
    if gmail_password:
        config["email_password"] = gmail_password
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
    print("Saved quick setup to:")
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
        config["email_password"] = app_password
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
            with imaplib.IMAP4_SSL(config["imap_server"], int(config["imap_port"])) as imap_conn:
                imap_conn.login(config["email_address"], config["email_password"])
        except imaplib.IMAP4.error:
            messagebox.showerror("Wrong Password", "Could not sign in. Check Gmail and app password.")
            return
        except Exception:
            messagebox.showerror("Connection Error", "Could not reach Gmail. Check your internet connection.")
            return

        targets = write_config_files(config, config_path)
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
    except Exception as err:
        fallback_config = {"error_log_file": "error.log"}
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
