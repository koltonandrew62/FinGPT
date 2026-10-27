"""Email (SMTP) and desktop notifications for the weekly report.

Email settings come from environment variables. For Gmail, create an App Password
(Google Account > Security > 2-Step Verification > App passwords) and use it as
SMTP_PASSWORD; your normal password will not work.

    SMTP_HOST=smtp.gmail.com  SMTP_PORT=587
    SMTP_USER=you@gmail.com   SMTP_PASSWORD=<16-char app password>
    EMAIL_TO=you@gmail.com    (comma-separate several addresses)
"""
from __future__ import annotations

import os
import platform
import shutil
import smtplib
import subprocess
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


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def desktop_notify(title: str, message: str) -> bool:
    """Pop a native notification. Returns False if no notifier is available."""
    message = message[:250]
    try:
        from plyer import notification   # optional, cross-platform
        notification.notify(title=title, message=message, app_name="Weekly Picks", timeout=15)
        return True
    except Exception:
        pass
    system = platform.system()
    if system == "Darwin":
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        cmd = ["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"']
    elif system == "Linux" and shutil.which("notify-send"):
        cmd = ["notify-send", title, message]
    elif system == "Windows":
        app_id = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$x = $t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode({_ps_quote(title)})) > $null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode({_ps_quote(message)})) > $null;"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(app_id)})"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        cmd = ["powershell", "-NoProfile", "-Command", script]
    else:
        return False
    return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
