"""Fake IMAP/SMTP servers and helpers. No network is ever touched.

They implement the same small interface as app.mailbox.ImapMailbox and
SmtpSender, which is what hvac_email_agent.process_inbox talks to.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from app.mailbox import MailAuthError, MailUnavailable, SendFailed

OWN = "office@hvac-test.com"


class Clock:
    """Controllable 'now' for cooldown/follow-up/dispatch timing."""

    def __init__(self, start: datetime | None = None):
        self.current = start or datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)  # Mon 10:00 Toronto

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


def make_email(
    sender: str = "jane@customer-mail.com",
    subject: str = "Furnace not working",
    body: str = "Hi, my furnace stopped working and we have no heat.\nAddress: 12 Main Street\n\nThanks,\nJane Smith",
    *,
    display_name: str = "Jane Smith",
    headers: dict | None = None,
    authenticated: bool = True,
    message_id: str | None = None,
    date: datetime | None = None,
) -> bytes:
    """Build a raw RFC 822 message as Gmail would deliver it."""
    msg = EmailMessage()
    if authenticated:
        domain = sender.rsplit("@", 1)[1]
        # The receiving server (Gmail) prepends its verdict at the top.
        msg["Authentication-Results"] = (
            f"mx.google.com; dkim=pass header.i=@{domain} header.s=s1 header.b=abc; "
            f"spf=pass (google.com: domain of {sender} designates 1.2.3.4) smtp.mailfrom={sender}; "
            f"dmarc=pass (p=NONE) header.from={domain}"
        )
    msg["From"] = f"{display_name} <{sender}>" if display_name else sender
    msg["To"] = OWN
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or make_msgid(domain="customer-mail.com")
    msg["Date"] = format_datetime(date or datetime(2026, 11, 16, 14, 55, tzinfo=timezone.utc))
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    return msg.as_bytes()


class FakeMailbox:
    def __init__(self, uidvalidity: str = "777"):
        self.uidvalidity = uidvalidity
        self.messages: dict[str, tuple[bytes, datetime | None]] = {}
        self.seen: set[str] = set()
        self.next_uid = 1
        self.selects: list[tuple[str, bool]] = []
        self.fetches: list[str] = []
        self.sent_folder: list[tuple[str, datetime]] = []  # (recipient, sent_at) manual replies
        self.sent_unavailable = False
        self.fail_login = False
        self.fail_connect = False

    # helpers for tests
    def deliver(self, raw: bytes, received: datetime | None = None) -> str:
        uid = str(self.next_uid)
        self.next_uid += 1
        self.messages[uid] = (raw, received or datetime(2026, 11, 16, 14, 55, tzinfo=timezone.utc))
        return uid

    # interface used by the agent
    def __enter__(self):
        if self.fail_connect:
            raise MailUnavailable("fake IMAP unreachable")
        if self.fail_login:
            raise MailAuthError("fake IMAP login rejected")
        return self

    def __exit__(self, *_exc):
        return None

    def select(self, mailbox: str, readonly: bool) -> str:
        self.selects.append((mailbox, readonly))
        self.readonly = readonly
        return self.uidvalidity

    def search_uids(self, unseen_only: bool) -> list[str]:
        uids = sorted(self.messages, key=int)
        return [uid for uid in uids if not (unseen_only and uid in self.seen)]

    def fetch(self, uid: str):
        self.fetches.append(uid)
        return self.messages[uid]

    def mark_seen(self, uid: str) -> None:
        assert not getattr(self, "readonly", False), "tried to change flags on a read-only mailbox"
        self.seen.add(uid)

    def sent_recipients_since(self, _names, since: datetime, _own):
        if self.sent_unavailable:
            return None
        result: dict[str, datetime] = {}
        for recipient, sent_at in self.sent_folder:
            if sent_at >= since and (recipient not in result or sent_at > result[recipient]):
                result[recipient] = sent_at
        return result


class FakeSmtp:
    def __init__(self):
        self.sent: list[EmailMessage] = []
        self.connects = 0
        self.fail_connect = False
        self.fail_send: Exception | BaseException | None = None

    def connect(self) -> None:
        if self.fail_connect:
            raise MailUnavailable("fake SMTP unreachable")
        self.connects += 1

    def send(self, message: EmailMessage) -> None:
        self.connect()
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(message)

    def close(self) -> None:
        return None

    def of_kind(self, kind: str) -> list[EmailMessage]:
        return [message for message in self.sent if message.get("X-HVAC-Auto-Type") == kind]


class Crash(BaseException):
    """Simulates the process being killed (not caught by `except Exception`)."""


__all__ = ["Clock", "Crash", "FakeMailbox", "FakeSmtp", "OWN", "SendFailed", "make_email"]
