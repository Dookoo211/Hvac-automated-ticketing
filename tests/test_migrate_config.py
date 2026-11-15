from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app import credentials
from app.settings import ConfigError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import migrate_config  # noqa: E402


def _write(tmp_path, **raw):
    base = {"business_name": "X", "email_address": "office@hvac-test.com", "dispatch_summary_recipient": "office@hvac-test.com"}
    base.update(raw)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base))
    return path


def test_moves_password_and_collapses_flags(tmp_path, monkeypatch):
    stored = {}
    monkeypatch.setattr(credentials, "set_password", lambda email, pw: stored.update({email: pw}))
    path = _write(tmp_path, email_password="abcd efgh", auto_reply_enabled=True, feature_auto_reply=False,
                  feature_dispatch_summary=True)
    changes = migrate_config.migrate(path)
    saved = json.loads(path.read_text())
    assert stored == {"office@hvac-test.com": "abcdefgh"}
    assert "email_password" not in saved
    assert saved["auto_reply_enabled"] is False and "feature_auto_reply" not in saved  # disagreement -> off
    assert saved["dispatch_summary_enabled"] is True and "feature_dispatch_summary" not in saved
    backups = list(tmp_path.glob("config.json.bak-*"))
    assert len(backups) == 1 and "email_password" not in json.loads(backups[0].read_text())
    assert any("Credential Manager" in change for change in changes)


def test_check_mode_changes_nothing(tmp_path):
    path = _write(tmp_path, email_password="secret", feature_follow_up=True)
    before = path.read_text()
    assert migrate_config.migrate(path, check_only=True)
    assert path.read_text() == before


def test_credential_failure_leaves_file_untouched(tmp_path, monkeypatch):
    def fail(_email, _pw):
        raise credentials.CredentialError("store unavailable")

    monkeypatch.setattr(credentials, "set_password", fail)
    path = _write(tmp_path, email_password="secret")
    before = path.read_text()
    with pytest.raises(credentials.CredentialError):
        migrate_config.migrate(path)
    assert path.read_text() == before


def test_invalid_config_is_refused(tmp_path):
    path = _write(tmp_path, imap_port="abc")
    with pytest.raises(ConfigError):
        migrate_config.migrate(path)
