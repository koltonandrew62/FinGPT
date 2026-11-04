"""Email through the user's own mailbox: SMTP for sending, IMAP for reading."""
from __future__ import annotations

import asyncio
import imaplib
import re
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, getaddresses, parsedate_to_datetime

from ..util import clip, html_to_text
from . import ToolError, tool
from .memory import find_contact

PROVIDERS = {
    "gmail.com": ("smtp.gmail.com", 465, "imap.gmail.com", 993),
    "googlemail.com": ("smtp.gmail.com", 465, "imap.gmail.com", 993),
    "outlook.com": ("smtp-mail.outlook.com", 587, "outlook.office365.com", 993),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "outlook.office365.com", 993),
    "live.com": ("smtp-mail.outlook.com", 587, "outlook.office365.com", 993),
    "yahoo.com": ("smtp.mail.yahoo.com", 465, "imap.mail.yahoo.com", 993),
    "icloud.com": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993),
    "me.com": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993),
    "aol.com": ("smtp.aol.com", 465, "imap.aol.com", 993),
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SETUP_HINT = (
    "Email isn't set up yet. Open Settings ▸ Email and enter your address plus an app password "
    "(for Gmail: myaccount.google.com/apppasswords, requires 2-Step Verification)."
)


def _servers(settings):
    domain = settings.email_address.split("@")[-1].lower()
    smtp_host, smtp_port, imap_host, imap_port = PROVIDERS.get(domain, (f"smtp.{domain}", 465, f"imap.{domain}", 993))
    return (
        settings.smtp_host or smtp_host,
        settings.smtp_port or smtp_port,
        settings.imap_host or imap_host,
        settings.imap_port or imap_port,
    )


def _require(settings):
    if not settings.email_configured:
        raise ToolError(SETUP_HINT)


def _is_gmail(settings) -> bool:
    return "gmail" in _servers(settings)[2]


def resolve_recipients(values) -> list[str]:
    if isinstance(values, str):
        values = [v for v in re.split(r"[;,]", values) if v.strip()]
    out = []
    for v in values or []:
        v = v.strip()
        if not v:
            continue
        # "Name <addr>" / "<addr>" first: EMAIL_RE would also accept "<addr>" with the brackets.
        m = re.search(r"<([^<>\s]+@[^<>\s]+)>", v)
        if m:
            out.append(m.group(1))
            continue
        if EMAIL_RE.match(v):
            out.append(v)
            continue
        contact = find_contact(v)
        if contact and contact.get("email"):
            out.append(contact["email"])
        elif contact:
            raise ToolError(f"{contact['name']} is saved without an email address. Ask the user for it and update the contact with save_contact.")
        else:
            raise ToolError(f"I don't have an email address for '{v}'. Ask the user for it (and offer to save it with save_contact).")
    return out


# --------------------------------------------------------------------- sending
def _send_sync(settings, to, cc, subject, body):
    smtp_host, smtp_port, _, _ = _servers(settings)
    msg = EmailMessage()
    msg["From"] = formataddr((settings.user_name or "", settings.email_address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = " ".join(str(subject or "").split())  # headers can't contain line breaks
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        with server:  # also closes the socket if STARTTLS or login fails
            if smtp_port != 465:
                server.starttls(context=context)
            server.login(settings.email_address, settings.email_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise ToolError("The mail server rejected the login. Check the email address and app password in Settings.")
    except (smtplib.SMTPException, OSError) as exc:
        raise ToolError(f"Sending failed: {exc}")


def _send_preview(args: dict) -> dict:
    try:
        to = ", ".join(resolve_recipients(args.get("to")))
    except ToolError:
        to = ", ".join(args.get("to") or []) if isinstance(args.get("to"), list) else str(args.get("to"))
    cc = args.get("cc")
    cc_text = ", ".join(cc) if isinstance(cc, list) else (cc or "")
    fields = [["To", to]]
    if cc_text:
        fields.append(["Cc", cc_text])
    fields.append(["Subject", args.get("subject", "")])
    return {"title": "Send email", "fields": fields, "body": args.get("body", "")}


@tool(
    "send_email",
    "Send an email from the user's own address. Recipients can be email addresses or saved contact "
    "names. Write a complete, well-structured message in the user's voice and sign it with their name. "
    "The user sees the full draft and must approve before it sends.",
    {
        "to": {"type": "array", "items": {"type": "string"}, "description": "Addresses or saved contact names."},
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Plain-text body including greeting and sign-off."},
        "cc": {"type": "array", "items": {"type": "string"}},
    },
    ["to", "subject", "body"],
    label="Sending email",
    preview=_send_preview,
)
async def send_email(ctx, to, subject: str, body: str, cc=None):
    _require(ctx.settings)
    recipients = resolve_recipients(to)
    cc_list = resolve_recipients(cc) if cc else []
    if not recipients:
        raise ToolError("No recipients given.")
    await asyncio.to_thread(_send_sync, ctx.settings, recipients, cc_list, subject, body)
    return {"status": "sent", "to": recipients, "cc": cc_list, "subject": subject}


# --------------------------------------------------------------------- reading
def _imap(settings):
    _, _, host, port = _servers(settings)
    try:  # the constructor closes its own socket if the greeting fails
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ToolError(f"Couldn't reach the mail server ({host}): {exc}")
    try:
        conn.login(settings.email_address, settings.email_password)
        return conn
    except BaseException as exc:
        _close(conn)  # a refused login must not leave the TLS socket open
        if isinstance(exc, imaplib.IMAP4.error):
            raise ToolError("The mail server rejected the login. Check the email address and app password in Settings.") from None
        if isinstance(exc, OSError):
            raise ToolError(f"Couldn't reach the mail server ({host}): {exc}") from None
        raise


def _close(conn) -> None:
    """Log out, and make sure the socket is closed even if LOGOUT itself fails."""
    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception:
            pass


def _quote_imap(text: str) -> str:
    return '"' + text.replace("\\", "").replace('"', "") + '"'


def _list_sync(settings, folder, unread_only, query, limit):
    conn = _imap(settings)
    try:
        status, _ = conn.select(_quote_imap(folder), readonly=True)
        if status != "OK":
            raise ToolError(f"Folder '{folder}' not found.")
        criteria: list[str] = []
        if unread_only:
            criteria.append("UNSEEN")
        if query:
            if _is_gmail(settings):
                criteria += ["X-GM-RAW", _quote_imap(query)]
            else:
                criteria += ["TEXT", _quote_imap(query)]
        if not criteria:
            criteria = ["ALL"]
        status, data = conn.uid("search", None, *criteria)
        if status != "OK":
            raise ToolError("Mailbox search failed.")
        uids = data[0].split()[-limit:][::-1]
        unread_total = None
        try:
            st, cnt = conn.status(_quote_imap(folder), "(UNSEEN)")
            m = re.search(rb"UNSEEN (\d+)", cnt[0]) if st == "OK" else None
            unread_total = int(m.group(1)) if m else None
        except Exception:
            pass
        messages = []
        if uids:
            status, rows = conn.uid("fetch", b",".join(uids), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
            parser = BytesParser(policy=policy.default)
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                meta, raw = row
                uid_m = re.search(rb"UID (\d+)", meta)
                hdr = parser.parsebytes(raw)
                try:
                    date = parsedate_to_datetime(hdr["Date"]).isoformat(timespec="minutes") if hdr["Date"] else None
                except (TypeError, ValueError):
                    date = hdr["Date"]
                messages.append(
                    {
                        "id": uid_m.group(1).decode() if uid_m else None,
                        "from": str(hdr["From"] or ""),
                        "subject": str(hdr["Subject"] or "(no subject)"),
                        "date": date,
                        "unread": b"\\Seen" not in meta,
                    }
                )
            order = {u.decode(): i for i, u in enumerate(uids)}
            messages.sort(key=lambda m: order.get(m["id"], 0))
        return {"folder": folder, "unread_in_folder": unread_total, "messages": messages}
    finally:
        _close(conn)


def _read_sync(settings, uid, folder):
    conn = _imap(settings)
    try:
        conn.select(_quote_imap(folder), readonly=True)
        status, rows = conn.uid("fetch", str(uid).encode(), "(BODY.PEEK[])")
        raw = next((r[1] for r in rows if isinstance(r, tuple)), None)
        if status != "OK" or raw is None:
            raise ToolError(f"Email {uid} not found in {folder}.")
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        body_part = msg.get_body(preferencelist=("plain", "html"))
        text = ""
        if body_part is not None:
            text = body_part.get_content()
            if body_part.get_content_type() == "text/html":
                text = html_to_text(text)
        attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        return {
            "id": str(uid),
            "from": str(msg["From"] or ""),
            "to": str(msg["To"] or ""),
            "cc": str(msg["Cc"] or ""),
            "subject": str(msg["Subject"] or ""),
            "date": str(msg["Date"] or ""),
            "body": clip(text.strip(), 7000),
            "attachments": attachments,
            "reply_to_addresses": [a for _, a in getaddresses([str(msg["Reply-To"] or msg["From"] or "")]) if a],
        }
    finally:
        _close(conn)


@tool(
    "list_emails",
    "List recent emails in the user's mailbox (newest first) with sender, subject, date and unread flag. "
    "Supports unread-only and search (Gmail search syntax works for Gmail, e.g. 'from:amazon newer_than:7d').",
    {
        "unread_only": {"type": "boolean", "description": "Only unread mail. Default false."},
        "query": {"type": "string", "description": "Optional search terms."},
        "limit": {"type": "integer", "description": "1-25. Default 10."},
        "folder": {"type": "string", "description": "Mailbox folder. Default 'INBOX'."},
    },
    label="Checking inbox",
)
async def list_emails(ctx, unread_only: bool = False, query: str | None = None, limit: int = 10, folder: str = "INBOX"):
    _require(ctx.settings)
    limit = max(1, min(int(limit or 10), 25))
    return await asyncio.to_thread(_list_sync, ctx.settings, folder or "INBOX", bool(unread_only), query, limit)


@tool(
    "read_email",
    "Open one email by the id from list_emails and return its full text and attachment names.",
    {"id": {"type": "string"}, "folder": {"type": "string", "description": "Default 'INBOX'."}},
    ["id"],
    label="Opening email",
)
async def read_email(ctx, id: str, folder: str = "INBOX"):
    _require(ctx.settings)
    if not str(id).isdigit():
        raise ToolError("Email id must be the numeric id from list_emails.")
    return await asyncio.to_thread(_read_sync, ctx.settings, id, folder or "INBOX")
