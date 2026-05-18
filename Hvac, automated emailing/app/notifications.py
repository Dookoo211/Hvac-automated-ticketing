#!/usr/bin/env python3
"""Notification utilities (SMS/email-to-SMS)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage


def send_sms_via_gateway(
    smtp_server: str,
    smtp_port: int,
    username: str,
    password: str,
    gateway_address: str,
    message_body: str,
) -> None:
    """Send SMS via email-to-SMS gateway."""
    message = EmailMessage()
    message["Subject"] = ""
    message["From"] = username
    message["To"] = gateway_address
    message.set_content(message_body)

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)

