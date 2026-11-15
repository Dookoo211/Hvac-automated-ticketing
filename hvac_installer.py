#!/usr/bin/env python3
"""One-click installer entrypoint: setup wizard then launch UI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app import credentials
from app.settings import ConfigError
from hvac_email_agent import ensure_runtime_paths, load_config, run_quick_setup_gui, run_ui, validate_license


def has_saved_login(config_path: Path) -> bool:
    """True when config.json has an address AND a password is available.

    The password is looked up in Windows Credential Manager (falling back to
    a legacy plaintext value in old config files), never only in config.json.
    """
    if not config_path.exists():
        return False
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return False
    email = str(raw.get("email_address", "")).strip()
    return bool(email and "@" in email and credentials.has_credentials(raw))


def show_fatal(title: str, message: str) -> None:
    """The installer is a windowed EXE with no console, so errors must be shown in a dialog."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        print(f"{title}: {message}")


def main() -> None:
    base_dir = Path(sys.argv[0]).resolve().parent
    os.chdir(base_dir)
    config_path = base_dir / "config.json"

    if not has_saved_login(config_path):
        run_quick_setup_gui(config_path)
        if not config_path.exists():
            return

    try:
        config = load_config(config_path)
    except ConfigError as err:
        show_fatal("Settings problem", str(err))
        return
    config["config_path"] = str(config_path)
    ensure_runtime_paths(config, config_path.parent)
    try:
        validate_license(config)
    except PermissionError as err:
        show_fatal("License problem", str(err))
        return
    run_ui(config, dry_run_override=None)


if __name__ == "__main__":
    main()
