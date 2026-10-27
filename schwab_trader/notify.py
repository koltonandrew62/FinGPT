"""Email (SMTP) for the weekly report and the morning 0DTE check.

Email settings come from environment variables. For Gmail, create an App Password
(Google Account > Security > 2-Step Verification > App passwords) and use it as
SMTP_PASSWORD; your normal password will not work.

    SMTP_HOST=smtp.gmail.com  SMTP_PORT=587
    SMTP_USER=you@gmail.com   SMTP_PASSWORD=<16-char app password>
    EMAIL_TO=you@gmail.com    (comma-separate several addresses)
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))


def send_email(subject: str, text: str, html: str | None = None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM", os.environ["SMTP_USER"])
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
