"""Thin IMAP/SMTP wrappers with timeouts and clear failure categories.

WHY (AUDIT H10, C1, C2):
* every connection has a timeout, so a dead network can't hang the daemon forever;
* messages are fetched with BODY.PEEK[] so *reading* a message never marks it
  as read. The agent marks a message \\Seen only after it is fully processed,
  and never in dry-run;
* failures fall into two groups. ``MailUnavailable`` means nothing happened
  and it is safe to retry later. ``SendFailed(ambiguous=True)`` means the
  message may or may not have been delivered, so the caller must not retry
  (we prefer a missed acknowledgment over a duplicate).

These classes are also the seam the tests replace with fakes (no network).
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import socket
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime


class MailUnavailable(RuntimeError):
    """Server unreachable or refused us before anything was changed. Safe to retry."""


class MailAuthError(MailUnavailable):
    """Login rejected (wrong address/app password)."""


class SendFailed(RuntimeError):
    def __init__(self, message: str, ambiguous: bool):
        super().__init__(message)
        self.ambiguous = ambiguous


def quote_mailbox(name: str) -> str:
    """IMAP mailbox names with spaces must be quoted.

    WHY: the old code sent ``SELECT [Gmail]/Sent Mail`` unquoted, which the
    server rejects, so manual-reply detection silently never worked.
    """
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_UID_RE = re.compile(rb"UID (\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


class ImapMailbox:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 30):
        self.host, self.port, self.user, self.password, self.timeout = host, int(port), user, password, timeout
        self.conn: imaplib.IMAP4_SSL | None = None
        self.selected: str | None = None

    # -- connection -------------------------------------------------------
    def __enter__(self) -> "ImapMailbox":
        try:
            self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        except (OSError, imaplib.IMAP4.error) as err:
            raise MailUnavailable(f"Cannot reach IMAP server {self.host}:{self.port}: {err}") from err
        try:
            self.conn.login(self.user, self.password)
        except imaplib.IMAP4.error as err:
            self._safe_logout()
            raise MailAuthError(f"IMAP login rejected for {self.user}: {err}") from err
        except OSError as err:
            self._safe_logout()
            raise MailUnavailable(f"IMAP connection dropped during login: {err}") from err
        return self

    def __exit__(self, *_exc: object) -> None:
        self._safe_logout()

    def _safe_logout(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _check(self, typ: str, data: object, what: str) -> None:
        if typ != "OK":
            raise RuntimeError(f"IMAP {what} failed: {data!r}")

    # -- operations ---------------------------------------------------------
    def select(self, mailbox: str, readonly: bool) -> str:
        """Select a mailbox; returns its UIDVALIDITY (part of our dedup key)."""
        typ, data = self.conn.select(quote_mailbox(mailbox), readonly=readonly)
        self._check(typ, data, f"SELECT {mailbox}")
        self.selected = mailbox
        _typ, values = self.conn.response("UIDVALIDITY")
        value = values[0] if values and values[0] else b"0"
        return value.decode() if isinstance(value, bytes) else str(value)

    def try_select(self, mailbox: str, readonly: bool) -> bool:
        try:
            self.select(mailbox, readonly)
            return True
        except (RuntimeError, imaplib.IMAP4.error):
            return False

    def search_uids(self, unseen_only: bool) -> list[str]:
        typ, data = self.conn.uid("SEARCH", None, "UNSEEN" if unseen_only else "ALL")
        self._check(typ, data, "SEARCH")
        if not data or not data[0]:
            return []
        return [uid.decode() for uid in data[0].split()]

    def fetch(self, uid: str) -> tuple[bytes, datetime | None]:
        """Return (raw message, server receive time). PEEK = does not set \\Seen."""
        typ, data = self.conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
        self._check(typ, data, f"FETCH {uid}")
        for part in data or []:
            if isinstance(part, tuple) and len(part) > 1:
                return part[1], _parse_internaldate(part[0])
        raise RuntimeError(f"IMAP FETCH {uid} returned no message body")

    def mark_seen(self, uid: str) -> None:
        typ, data = self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        self._check(typ, data, f"STORE {uid}")

    def sent_recipients_since(self, sent_mailboxes: list[str], since: datetime, own_addresses: set[str]) -> dict[str, datetime] | None:
        """Map recipient -> latest *manual* (non-agent) email we sent them since ``since``.

        Returns None if no Sent folder could be opened; the caller treats that
        as "cannot verify" and holds follow-ups instead of guessing.
        Only headers are fetched (the old code downloaded every full sent email).
        """
        opened = next((name for name in sent_mailboxes if name and self.try_select(name, readonly=True)), None)
        if opened is None:
            return None
        typ, data = self.conn.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
        self._check(typ, data, "SEARCH sent")
        uids = data[0].split() if data and data[0] else []
        result: dict[str, datetime] = {}
        if not uids:
            return result
        typ, data = self.conn.uid(
            "FETCH", b",".join(uids).decode(), "(BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE X-HVAC-AUTO)])"
        )
        self._check(typ, data, "FETCH sent headers")
        from email import message_from_bytes
        from email.policy import default as default_policy

        for part in data or []:
            if not (isinstance(part, tuple) and len(part) > 1):
                continue
            headers = message_from_bytes(part[1], policy=default_policy)
            if str(headers.get("X-HVAC-Auto", "")).strip().lower() == "true":
                continue  # our own automatic mail is not a "manual reply"
            sender = parseaddr(str(headers.get("From", "")))[1].strip().lower()
            if sender not in own_addresses:
                continue
            try:
                sent_at = parsedate_to_datetime(str(headers.get("Date", "")))
            except (TypeError, ValueError):
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            sent_at = sent_at.astimezone(timezone.utc)
            if sent_at < since:
                continue
            for _name, address in getaddresses([str(headers.get("To", "")), str(headers.get("Cc", ""))]):
                address = address.strip().lower()
                if address and (address not in result or sent_at > result[address]):
                    result[address] = sent_at
        return result


def _parse_internaldate(header: bytes) -> datetime | None:
    match = _INTERNALDATE_RE.search(header or b"")
    if not match:
        return None
    parsed = imaplib.Internaldate2tuple(b'INTERNALDATE "' + match.group(1) + b'"')
    if parsed is None:
        return None
    return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)


class SmtpSender:
    """Lazily connected, reused-per-cycle SMTP session."""

    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 30):
        self.host, self.port, self.user, self.password, self.timeout = host, int(port), user, password, timeout
        self.smtp: smtplib.SMTP | None = None

    def connect(self) -> None:
        """Make sure we have a live, logged-in session. Raises MailUnavailable/MailAuthError.

        Called BEFORE the agent commits "about to send", so a server that is
        down never leaves a lead half-processed (AUDIT C1).
        """
        if self.smtp is not None:
            try:
                if self.smtp.noop()[0] == 250:
                    return
            except (smtplib.SMTPException, OSError):
                pass
            self.close()
        try:
            if self.port == 465:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            smtp.login(self.user, self.password)
        except smtplib.SMTPAuthenticationError as err:
            raise MailAuthError(f"SMTP login rejected for {self.user}: {err}") from err
        except (smtplib.SMTPException, OSError, socket.timeout) as err:
            raise MailUnavailable(f"Cannot reach SMTP server {self.host}:{self.port}: {err}") from err
        self.smtp = smtp

    def send(self, message: EmailMessage) -> None:
        self.connect()
        try:
            refused = self.smtp.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as err:
            # The server refused before accepting the message: definitely not sent.
            raise SendFailed(f"Server refused the message: {err}", ambiguous=False) from err
        except (smtplib.SMTPException, OSError) as err:
            # Failure during/after DATA: the server may have accepted it.
            self.close()
            raise SendFailed(f"Send failed (may or may not have been delivered): {err}", ambiguous=True) from err
        if refused:
            raise SendFailed(f"Some recipients were refused: {refused}", ambiguous=False)

    def close(self) -> None:
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:
                pass
            self.smtp = None
