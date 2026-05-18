#!/usr/bin/env python3
"""One-click installer entrypoint: setup wizard then launch UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import json

from hvac_email_agent import load_config, run_quick_setup_gui, run_ui, validate_license


def has_saved_login(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    email = str(raw.get("email_address", "")).strip()
    password = str(raw.get("email_password", "")).strip()
    return bool(email and "@" in email and password and password != "replace-with-app-password")


def main() -> None:
    base_dir = Path(sys.argv[0]).resolve().parent
    os.chdir(base_dir)
    config_path = base_dir / "config.json"

    if not has_saved_login(config_path):
        run_quick_setup_gui(config_path)
        if not config_path.exists():
            return

    config = load_config(config_path)
    config["config_path"] = str(config_path)
    validate_license(config)
    run_ui(config, dry_run_override=None)


if __name__ == "__main__":
    main()
