#!/usr/bin/env python3
"""Migrate an existing config.json to the Phase 1 format.

What it does:
  1. Moves ``email_password`` into Windows Credential Manager (verified by
     reading it back) and removes it from config.json (AUDIT C5).
  2. Collapses each legacy flag pair into one key, e.g. ``feature_auto_reply``
     + ``auto_reply_enabled`` -> ``auto_reply_enabled``, using the same rule as
     the agent at runtime: if the two disagree, the feature is turned OFF (AUDIT M3).
  3. Validates the result and refuses to write a config the agent would reject.

A backup copy is written first as config.json.bak-<timestamp>, WITHOUT the
password (keeping a plaintext copy would defeat the purpose; the password is
verified in Credential Manager before anything is written).

The old keys keep working without this script; it only cleans the file up.

Usage:
    python scripts/migrate_config.py              # migrate ./config.json
    python scripts/migrate_config.py --check      # show what would change, change nothing
    python scripts/migrate_config.py --config path\\to\\config.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# WHY: running "python scripts/x.py" puts scripts/ (not the project root) on
# sys.path, which is why the old migrate_csv_to_db.py failed with
# "No module named 'app'" (AUDIT L1).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import hvac_email_agent as agent  # noqa: E402
from app import credentials, settings  # noqa: E402
from app.fileutil import atomic_write_json, timestamp_slug  # noqa: E402


def migrate(config_path: Path, check_only: bool = False) -> list[str]:
    """Return a list of human-readable changes (made, or that would be made)."""
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise SystemExit("config.json does not contain a JSON object.")
    changes: list[str] = []
    updated = dict(raw)

    merged = agent.deep_merge(agent.DEFAULT_CONFIG, raw)
    for warning in settings.resolve_flag_pairs(raw, merged):
        changes.append(f"note: {warning}")
    for canonical, legacy in settings.FLAG_PAIRS.items():
        if canonical in raw or legacy in raw:
            if updated.get(canonical) != merged[canonical] or legacy in updated:
                changes.append(f"set '{canonical}' = {str(merged[canonical]).lower()} and remove '{legacy}'")
            updated[canonical] = merged[canonical]
            updated.pop(legacy, None)
    updated.pop("config_path", None)

    password = credentials.plaintext_password(raw)
    if password:
        changes.append("move email_password into Windows Credential Manager and remove it from config.json")
    elif "email_password" in raw:
        changes.append("remove empty/placeholder email_password")

    check = agent.deep_merge(agent.DEFAULT_CONFIG, updated)
    settings.resolve_flag_pairs(updated, check)
    check["office_schedule"] = agent.normalize_office_schedule(check.get("office_schedule"))
    problems = settings.validate_config(check)
    if problems:
        raise settings.ConfigError(problems)

    if check_only or not changes:
        return changes

    if password:
        # Store and verify BEFORE touching the file: if this fails, nothing changes.
        credentials.set_password(str(raw.get("email_address", "")), password)
    updated.pop("email_password", None)

    backup = config_path.with_name(f"{config_path.name}.bak-{timestamp_slug()}")
    backup_copy = {key: value for key, value in raw.items() if key != "email_password"}
    atomic_write_json(backup, backup_copy)
    atomic_write_json(config_path, updated)
    changes.append(f"backup (without password) written to {backup.name}")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--check", action="store_true", help="only report what would change")
    args = parser.parse_args()
    path = Path(args.config).resolve()
    if not path.exists():
        print(f"Not found: {path}")
        return 1
    try:
        changes = migrate(path, check_only=args.check)
    except (settings.ConfigError, credentials.CredentialError) as err:
        print(f"Nothing was changed: {err}")
        return 2
    if not changes:
        print("config.json is already up to date.")
    for change in changes:
        print(("WOULD " if args.check else "") + change)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
