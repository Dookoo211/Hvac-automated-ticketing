from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hvac_email_agent as agent  # noqa: E402
from app import credentials  # noqa: E402

from fakes import OWN, Clock, FakeMailbox, FakeSmtp  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test runs in its own temp folder and never touches the real
    Windows Credential Manager, config.json or data/ of the project."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credentials, "get_password", lambda _email: None)
    monkeypatch.setattr(credentials, "set_password", lambda _email, _pw: None)
    yield


def write_config(tmp_path: Path, **overrides) -> Path:
    raw = {
        "business_name": "Test HVAC",
        "phone_number": "555-000-1111",
        "email_address": OWN,
        "email_password": "test-only-password",  # legacy plaintext path; keyring is stubbed out
        "imap_server": "imap.gmail.com",
        "smtp_server": "smtp.gmail.com",
        "auto_reply_enabled": True,
        "dispatch_summary_enabled": False,
        "dispatch_summary_recipient": OWN,
        "follow_up_enabled": False,
        "license_enforcement": False,
        "dry_run": False,
    }
    raw.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


@pytest.fixture
def make_config(tmp_path):
    def factory(**overrides) -> dict:
        config = agent.load_config(write_config(tmp_path, **overrides))
        config["config_path"] = str(tmp_path / "config.json")
        return config

    return factory


class Harness:
    """Runs real process_inbox cycles against fake servers and a fake clock."""

    def __init__(self, config: dict):
        self.config = config
        self.mailbox = FakeMailbox()
        self.smtp = FakeSmtp()
        self.clock = Clock()

    def cycle(self, dry_run: bool | None = None, **kwargs):
        return agent.process_inbox(
            self.config,
            dry_run_override=dry_run,
            mailbox_factory=lambda _config, _password: self.mailbox,
            smtp_factory=lambda _config, _password: self.smtp,
            now_fn=self.clock,
            **kwargs,
        )

    def db(self):
        import sqlite3

        from app import database

        conn = sqlite3.connect(self.config["db_path"])
        conn.row_factory = database.dict_factory
        return conn

    def rows(self, query: str, params: tuple = ()) -> list[dict]:
        conn = self.db()
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()


@pytest.fixture
def harness(make_config):
    def factory(**overrides) -> Harness:
        return Harness(make_config(**overrides))

    return factory
