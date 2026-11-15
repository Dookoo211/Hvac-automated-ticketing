#!/usr/bin/env python3
"""Setup wizard helpers."""

from __future__ import annotations

import getpass
import json
from pathlib import Path


def prompt_text(prompt: str, default_value: str = "") -> str:
    default_value = default_value or ""
    suffix = f" [{default_value}]" if default_value else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else default_value


def run_basic_setup(config_path: Path, config: dict) -> dict:
    """Basic setup to capture business and email credentials."""
    config["business_name"] = prompt_text("Business name", str(config.get("business_name", "")))
    config["phone_number"] = prompt_text("Phone number", str(config.get("phone_number", "")))
    config["email_address"] = prompt_text("Gmail address", str(config.get("email_address", "")))
    config["email_password"] = getpass.getpass("Gmail app password: ").strip() or config.get("email_password", "")
    config["default_service_area"] = prompt_text("Default service area", str(config.get("default_service_area", "")))
    config["dispatcher_phone"] = prompt_text("Dispatcher phone number", str(config.get("dispatcher_phone", "")))
    config["imap_server"] = "imap.gmail.com"
    config["imap_port"] = 993
    config["smtp_server"] = "smtp.gmail.com"
    config["smtp_port"] = 587
    config["sent_mailbox"] = "[Gmail]/Sent Mail"
    config["dispatch_summary_recipient"] = config["email_address"]
    config["dry_run"] = True
    return config


def save_config(config_path: Path, config: dict) -> None:
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

