"""Mailbox password storage in Windows Credential Manager (via ``keyring``).

WHY (AUDIT C5): the Gmail app password sat in plaintext in config.json, which
was committed to a public GitHub repo and bundled into the EXEs. The password
now lives in the OS credential store, keyed by the mailbox address, and
config.json keeps only non-secret settings.

Backward compatibility: an old config.json that still has ``email_password``
keeps working (with a loud warning) until scripts/migrate_config.py or the
setup wizard moves the password into the credential store.
"""

from __future__ import annotations

SERVICE_NAME = "HVACEmailAgent"
PLACEHOLDER_PASSWORDS = {"", "replace-with-app-password"}

_warned_plaintext = False


class CredentialError(RuntimeError):
    """The credential store is unavailable or did not accept the password."""


def _keyring():
    try:
        import keyring  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError as err:  # pragma: no cover - depends on install
        raise CredentialError(
            "The 'keyring' package is not installed, so the mailbox password cannot be stored securely. "
            "Run: pip install -r requirements.txt"
        ) from err
    return keyring


def _account(email: str) -> str:
    return str(email or "").strip().lower()


def get_password(email: str) -> str | None:
    if not _account(email):
        return None
    try:
        return _keyring().get_password(SERVICE_NAME, _account(email))
    except CredentialError:
        raise
    except Exception as err:  # keyring backends raise many different types
        raise CredentialError(f"Could not read from Windows Credential Manager: {err}") from err


def set_password(email: str, password: str) -> None:
    """Store the password and read it back. WHY the read-back: callers delete
    the plaintext copy afterwards, so we must be sure the store kept it."""
    if not _account(email) or not password:
        raise CredentialError("Email address and password are both required.")
    try:
        store = _keyring()
        store.set_password(SERVICE_NAME, _account(email), password)
        stored = store.get_password(SERVICE_NAME, _account(email))
    except CredentialError:
        raise
    except Exception as err:
        raise CredentialError(f"Could not save to Windows Credential Manager: {err}") from err
    if stored != password:
        raise CredentialError("Windows Credential Manager did not keep the password (read-back mismatch).")


def delete_password(email: str) -> None:
    try:
        _keyring().delete_password(SERVICE_NAME, _account(email))
    except Exception:
        pass


def plaintext_password(config: dict) -> str:
    value = str(config.get("email_password", "") or "").replace(" ", "").strip()
    return "" if value in PLACEHOLDER_PASSWORDS else value


def resolve_mail_password(config: dict) -> str:
    """Return the mailbox password: credential store first, legacy plaintext second."""
    global _warned_plaintext
    stored: str | None = None
    store_error: CredentialError | None = None
    try:
        stored = get_password(config.get("email_address", ""))
    except CredentialError as err:
        store_error = err
    if stored:
        return stored

    legacy = plaintext_password(config)
    if legacy:
        if not _warned_plaintext:
            print(
                "WARNING: the mailbox password is stored in plaintext in config.json. "
                "Run 'python scripts/migrate_config.py' to move it into Windows Credential Manager."
            )
            _warned_plaintext = True
        return legacy

    if store_error is not None:
        raise store_error
    raise CredentialError(
        f"No saved password for {config.get('email_address', '(no address)')}. Run setup (--quick-setup) to sign in."
    )


def has_credentials(config: dict) -> bool:
    try:
        return bool(resolve_mail_password(config))
    except CredentialError:
        return False
