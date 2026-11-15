"""Config flag consolidation and startup validation.

WHY (AUDIT M3): every feature had two switches (e.g. ``auto_reply_enabled``
and ``feature_auto_reply``), checked inconsistently and with different
defaults, and bad values (an unknown timezone, a text port) were silently
accepted or replaced with defaults. This module:

* collapses each pair into one canonical key while still accepting the old
  key, so existing config files keep working;
* validates the whole config once at startup and reports *every* problem in
  plain English, instead of failing later in the middle of a mail cycle.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# canonical key -> legacy key that still has to be honoured
FLAG_PAIRS: dict[str, str] = {
    "auto_reply_enabled": "feature_auto_reply",
    "follow_up_enabled": "feature_follow_up",
    "dispatch_summary_enabled": "feature_dispatch_summary",
    "sms_enabled": "feature_sms_notifications",
}

DUPLICATE_MODES = {"update", "replace", "ignore", "delete"}  # "delete" = legacy name for "ignore"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

BOOL_KEYS = [
    *FLAG_PAIRS.keys(),
    *FLAG_PAIRS.values(),
    "feature_ticketing",
    "read_unseen_only",
    "ignore_promotions",
    "dry_run",
    "license_enforcement",
    "mark_processed_as_read",
    "require_sender_auth",
]

# key -> (minimum, maximum or None)
INT_RANGES: dict[str, tuple[int, int | None]] = {
    "imap_port": (1, 65535),
    "smtp_port": (1, 65535),
    "poll_interval_seconds": (5, None),
    "heartbeat_interval_minutes": (0, None),
    "ui_refresh_seconds": (1, None),
    "dispatch_summary_interval_minutes": (1, None),
    "max_auto_emails_per_day": (0, None),
    "max_sms_per_day": (0, None),
    "network_timeout_seconds": (5, 600),
}

# key -> minimum (int or float accepted)
NUMBER_MINIMUMS: dict[str, float] = {
    "auto_reply_cooldown_hours": 0,
    "follow_up_delay_hours": 1,
    "max_reply_age_hours": 0,
}

STRING_LIST_KEYS = [
    "services",
    "lead_keywords",
    "existing_keywords",
    "spam_keywords",
    "high_priority_keywords",
    "trusted_authserv_ids",
    "own_addresses",
]

REQUIRED_TEXT_KEYS = [
    "business_name",
    "phone_number",
    "office_hours",
    "imap_server",
    "smtp_server",
    "mailbox",
    "db_path",
    "leads_csv_file",
    "service_requests_file",
    "draft_replies_file",
    "job_tickets_file",
    "error_log_file",
    "agent_log_file",
]


class ConfigError(ValueError):
    """Raised when config.json has one or more invalid values."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        lines = "\n".join(f"  - {item}" for item in self.problems)
        super().__init__(f"config.json has {len(self.problems)} problem(s):\n{lines}")


def _is_int(value: object) -> bool:
    # bool is a subclass of int in Python; "true" is not a valid port.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (isinstance(value, (int, float))) and not isinstance(value, bool)


def resolve_flag_pairs(raw: dict, merged: dict) -> list[str]:
    """Collapse each flag pair into one value and mirror it onto both keys.

    Rules:
    * only one of the two keys in config.json -> use it;
    * both present and equal -> use it;
    * both present and different -> OFF, with a warning. WHY: the old code ANDed
      them in some places and checked only one in others, so "disagree" never
      had a reliable meaning; OFF is the choice that cannot send unexpected email.
    * neither present -> the built-in default.

    Both keys are set to the result so any code still reading a legacy key
    sees the same answer. Non-boolean values are left for validate_config.
    """
    warnings: list[str] = []
    for canonical, legacy in FLAG_PAIRS.items():
        has_canonical = canonical in raw
        has_legacy = legacy in raw
        if has_canonical and has_legacy:
            first, second = raw[canonical], raw[legacy]
            if not isinstance(first, bool) or not isinstance(second, bool):
                continue
            value = first and second
            if first != second:
                warnings.append(
                    f"'{canonical}' is {str(first).lower()} but the older '{legacy}' is "
                    f"{str(second).lower()}; treating the feature as OFF. "
                    "Run scripts/migrate_config.py to clean this up."
                )
        elif has_canonical:
            value = raw[canonical]
        elif has_legacy:
            value = raw[legacy]
        else:
            value = merged.get(canonical, False)
        merged[canonical] = value
        merged[legacy] = value
    return warnings


def normalize_duplicate_mode(value: object) -> str:
    mode = str(value or "update").strip().lower()
    return "ignore" if mode == "delete" else mode


def validate_config(config: dict) -> list[str]:
    """Return a list of human-readable problems (empty list = valid)."""
    problems: list[str] = []

    for key in REQUIRED_TEXT_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"'{key}' must be a non-empty text value.")

    email = config.get("email_address")
    if not isinstance(email, str) or not EMAIL_RE.match(email.strip()):
        problems.append("'email_address' must be a valid email address (e.g. office@yourcompany.com).")

    for key in BOOL_KEYS:
        if key in config and not isinstance(config[key], bool):
            problems.append(f"'{key}' must be true or false (no quotes), got {config[key]!r}.")

    for key, (minimum, maximum) in INT_RANGES.items():
        if key not in config:
            continue
        value = config[key]
        if not _is_int(value):
            problems.append(f"'{key}' must be a whole number, got {value!r}.")
            continue
        if value < minimum or (maximum is not None and value > maximum):
            upper = f" and at most {maximum}" if maximum is not None else ""
            problems.append(f"'{key}' must be at least {minimum}{upper}, got {value}.")

    for key, minimum in NUMBER_MINIMUMS.items():
        if key not in config:
            continue
        value = config[key]
        if not _is_number(value) or value < minimum:
            problems.append(f"'{key}' must be a number of at least {minimum}, got {value!r}.")

    for key in STRING_LIST_KEYS:
        if key not in config:
            continue
        value = config[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"'{key}' must be a list of text values, e.g. [\"a\", \"b\"].")

    keyword_map = config.get("service_keyword_map", {})
    if not isinstance(keyword_map, dict) or not all(
        isinstance(words, list) and all(isinstance(word, str) for word in words) for words in keyword_map.values()
    ):
        problems.append("'service_keyword_map' must map each service name to a list of text keywords.")

    tz_name = config.get("office_timezone")
    try:
        ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        # WHY an error now: the old code silently fell back to UTC, which put
        # the after-hours message on the wrong side of the office day.
        problems.append(
            f"'office_timezone' {tz_name!r} is not a known timezone (use a name like "
            "'America/Toronto'). If it should be valid, install the 'tzdata' package."
        )

    schedule = config.get("office_schedule")
    if not isinstance(schedule, dict):
        problems.append("'office_schedule' must map mon..sun to {\"start\": \"HH:MM\", \"end\": \"HH:MM\"} or null.")
    else:
        for day, hours in schedule.items():
            if hours is None:
                continue
            if not isinstance(hours, dict):
                problems.append(f"'office_schedule.{day}' must be null or {{\"start\": \"HH:MM\", \"end\": \"HH:MM\"}}.")
                continue
            for edge in ("start", "end"):
                match = HHMM_RE.match(str(hours.get(edge, "")))
                if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                    problems.append(f"'office_schedule.{day}.{edge}' must be a time like 08:00, got {hours.get(edge)!r}.")

    mode = str(config.get("ticket_duplicate_mode", "update")).strip().lower()
    if mode not in DUPLICATE_MODES:
        problems.append(
            f"'ticket_duplicate_mode' must be one of update, replace, ignore (or the old name 'delete'), got {mode!r}."
        )

    if config.get("dispatch_summary_enabled") is True:
        recipient = str(config.get("dispatch_summary_recipient", "")).strip()
        if not EMAIL_RE.match(recipient):
            problems.append("'dispatch_summary_recipient' must be a valid email when dispatch summaries are enabled.")

    if config.get("sms_enabled") is True:
        gateway = str(config.get("sms_gateway_email", "")).strip()
        if not EMAIL_RE.match(gateway):
            problems.append("'sms_gateway_email' must be a valid email-to-SMS address when SMS alerts are enabled.")

    return problems


def config_warnings(config: dict) -> list[str]:
    """Non-fatal advice shown at startup."""
    warnings: list[str] = []
    server = str(config.get("imap_server", "")).lower()
    trusted = [str(item).lower() for item in config.get("trusted_authserv_ids", [])]
    if config.get("require_sender_auth") and "gmail" not in server and trusted == ["mx.google.com"]:
        warnings.append(
            "require_sender_auth is on but the mailbox is not Gmail. Set 'trusted_authserv_ids' to your "
            "provider's Authentication-Results server name, or every auto-reply will be held back."
        )
    return warnings
