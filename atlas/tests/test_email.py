import json
import smtplib

import pytest

from app.config import Settings
from app.tools import ToolError, call_tool, email_tools
from app.tools.email_tools import SETUP_HINT, _is_gmail, _send_preview, _servers, resolve_recipients
from conftest import make_ctx, run


@pytest.fixture
def contacts():
    from app.tools import memory

    memory.save(
        "contacts",
        [
            {"name": "Mom", "email": "mom@example.com", "aliases": ["mother"], "phone": ""},
            {"name": "Maya Patel", "email": "maya@example.com", "aliases": [], "phone": ""},
            {"name": "Maya Chen", "email": "mchen@example.com", "aliases": [], "phone": ""},
        ],
    )


def test_resolve_recipients_forms(contacts):
    assert resolve_recipients(["a@example.com"]) == ["a@example.com"]
    assert resolve_recipients("a@example.com, b@example.org; c@example.net") == ["a@example.com", "b@example.org", "c@example.net"]
    assert resolve_recipients(["Jane Doe <jane@example.com>"]) == ["jane@example.com"]
    assert resolve_recipients(["<bare@example.com>"]) == ["bare@example.com"]
    assert resolve_recipients(["Mom", "mother", " maya patel "]) == ["mom@example.com", "mom@example.com", "maya@example.com"]
    assert resolve_recipients([]) == [] and resolve_recipients(None) == [] and resolve_recipients(["", "  "]) == []


def test_resolve_recipients_unknown_or_ambiguous(contacts):
    with pytest.raises(ToolError, match="don't have an email address for 'Uncle Bob'"):
        resolve_recipients(["Uncle Bob"])
    with pytest.raises(ToolError, match="'Maya'"):
        resolve_recipients(["Maya"])


@pytest.mark.parametrize(
    "address, expected",
    [
        ("me@gmail.com", ("smtp.gmail.com", 465, "imap.gmail.com", 993)),
        ("me@GoogleMail.com", ("smtp.gmail.com", 465, "imap.gmail.com", 993)),
        ("me@outlook.com", ("smtp-mail.outlook.com", 587, "outlook.office365.com", 993)),
        ("me@icloud.com", ("smtp.mail.me.com", 587, "imap.mail.me.com", 993)),
        ("me@mycompany.io", ("smtp.mycompany.io", 465, "imap.mycompany.io", 993)),
    ],
)
def test_server_defaults(address, expected):
    assert _servers(Settings(email_address=address)) == expected


def test_server_overrides_and_gmail_detection():
    s = Settings(email_address="me@mycompany.io", smtp_host="mail.corp", smtp_port=2525, imap_host="imap.corp", imap_port=1993)
    assert _servers(s) == ("mail.corp", 2525, "imap.corp", 1993)
    assert _is_gmail(Settings(email_address="me@gmail.com"))
    assert _is_gmail(Settings(email_address="me@mydomain.com", imap_host="imap.gmail.com"))  # Google Workspace
    assert not _is_gmail(Settings(email_address="me@outlook.com"))


def test_send_preview(contacts):
    p = _send_preview({"to": ["Mom"], "cc": ["x@example.com"], "subject": "Dinner", "body": "Hi Mom"})
    assert p == {"title": "Send email", "fields": [["To", "mom@example.com"], ["Cc", "x@example.com"], ["Subject", "Dinner"]], "body": "Hi Mom"}
    unresolved = _send_preview({"to": ["Uncle Bob"], "subject": "s", "body": "b"})
    assert unresolved["fields"][0] == ["To", "Uncle Bob"]


class FakeSMTP:
    instances = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port = host, port
        self.calls = []
        self.sent = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if password == "wrong":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(email_tools.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(email_tools.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def send(ctx, **args):
    return json.loads(run(call_tool("send_email", args, ctx)))


def test_send_email_requires_setup(fake_smtp):
    out = send(make_ctx(), to=["a@example.com"], subject="s", body="b")
    assert out == {"error": SETUP_HINT}
    assert fake_smtp.instances == []


def test_send_email_gmail_ssl(fake_smtp, contacts):
    ctx = make_ctx(email_address="me@gmail.com", email_password="abcd efgh ijkl mnop", user_name="Kolton")
    out = send(ctx, to=["Mom"], cc=["Jane <jane@example.com>"], subject="Dinner\r\nBcc: evil@example.com", body="Hi Mom,\n\nSee you at 7.\n\nKolton")
    assert out == {"status": "sent", "to": ["mom@example.com"], "cc": ["jane@example.com"], "subject": "Dinner\r\nBcc: evil@example.com"}
    (server,) = fake_smtp.instances
    assert (server.host, server.port) == ("smtp.gmail.com", 465)
    assert "starttls" not in server.calls
    assert ("login", "me@gmail.com", "abcd efgh ijkl mnop") in server.calls
    msg = server.sent[0]
    assert msg["To"] == "mom@example.com" and msg["Cc"] == "jane@example.com"
    assert msg["Subject"] == "Dinner Bcc: evil@example.com"  # line breaks can't inject headers
    assert msg["Bcc"] is None
    assert msg["From"] == "Kolton <me@gmail.com>"
    assert "See you at 7." in msg.get_content()
    assert server.closed


def test_send_email_starttls_for_587(fake_smtp):
    ctx = make_ctx(email_address="me@outlook.com", email_password="pw")
    assert send(ctx, to="a@example.com", subject="s", body="b")["status"] == "sent"
    (server,) = fake_smtp.instances
    assert (server.host, server.port) == ("smtp-mail.outlook.com", 587)
    assert server.calls[0] == "starttls"


def test_send_email_auth_failure(fake_smtp):
    ctx = make_ctx(email_address="me@gmail.com", email_password="wrong")
    out = send(ctx, to=["a@example.com"], subject="s", body="b")
    assert "rejected the login" in out["error"]
    assert fake_smtp.instances[0].closed


def test_send_email_unknown_recipient_never_connects(fake_smtp):
    ctx = make_ctx(email_address="me@gmail.com", email_password="pw")
    assert "don't have an email address" in send(ctx, to=["Nobody"], subject="s", body="b")["error"]
    assert "No recipients" in send(ctx, to=[], subject="s", body="b")["error"]
    assert fake_smtp.instances == []


HEADERS = {
    b"101": b"From: Amazon <ship@amazon.com>\r\nTo: me@gmail.com\r\nSubject: Your order shipped\r\nDate: Tue, 03 Nov 2026 10:00:00 +0000\r\n\r\n",
    b"102": b"From: Mom <mom@example.com>\r\nSubject: =?utf-8?q?Dinner_=F0=9F=8D=9D?=\r\nDate: not a date\r\n\r\n",
    b"103": b"From: Boss <boss@corp.com>\r\nDate: Wed, 04 Nov 2026 09:00:00 -0500\r\n\r\n",
}
FULL = (
    b"From: Mom <mom@example.com>\r\nTo: me@gmail.com\r\nReply-To: Mom Alt <mom.alt@example.com>\r\nSubject: Photos\r\n"
    b"Date: Tue, 03 Nov 2026 10:00:00 +0000\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=XX\r\n\r\n"
    b"--XX\r\nContent-Type: multipart/alternative; boundary=YY\r\n\r\n"
    b"--YY\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello <b>dear</b></p>\r\n--YY--\r\n"
    b"--XX\r\nContent-Type: image/jpeg\r\nContent-Disposition: attachment; filename=beach.jpg\r\nContent-Transfer-Encoding: base64\r\n\r\n/9j/\r\n--XX--\r\n"
)


class FakeIMAP:
    instances = []
    login_error = None

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port = host, port
        self.commands = []
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        if FakeIMAP.login_error:
            raise FakeIMAP.login_error

    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return ("OK", [b"3"]) if folder == '"INBOX"' else ("NO", [b"no such folder"])

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == "search":
            return "OK", [b"101 102 103"]
        if command == "fetch" and args[1] == "(BODY.PEEK[])":
            return ("OK", [(b"1 (UID 55 BODY[] {999}", FULL), b")"]) if args[0] == b"55" else ("OK", [None])
        rows = []
        for uid in args[0].split(b","):
            flags = b"\\Seen" if uid == b"101" else b""
            rows.append((b"1 (UID " + uid + b" FLAGS (" + flags + b") BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {99}", HEADERS[uid]))
            rows.append(b")")
        return "OK", rows

    def status(self, folder, what):
        return "OK", [b'"INBOX" (UNSEEN 2)']

    def logout(self):
        self.logged_out = True
        if FakeIMAP.logout_error:
            raise FakeIMAP.logout_error

    def shutdown(self):
        self.shut_down = True


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.login_error = None
    FakeIMAP.logout_error = None
    monkeypatch.setattr(email_tools.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def list_emails(ctx, **args):
    return json.loads(run(call_tool("list_emails", args, ctx)))


def test_list_emails_parses_and_orders(fake_imap):
    ctx = make_ctx(email_address="me@gmail.com", email_password="pw")
    out = list_emails(ctx, limit=10)
    assert out["folder"] == "INBOX" and out["unread_in_folder"] == 2
    msgs = out["messages"]
    assert [m["id"] for m in msgs] == ["103", "102", "101"]  # newest first
    assert msgs[2] == {"id": "101", "from": "Amazon <ship@amazon.com>", "subject": "Your order shipped", "date": "2026-11-03T10:00+00:00", "unread": False}
    assert msgs[1]["subject"] == "Dinner 🍝" and msgs[1]["date"] == "not a date" and msgs[1]["unread"] is True
    assert msgs[0]["subject"] == "(no subject)"
    imap = fake_imap.instances[0]
    assert (imap.host, imap.port) == ("imap.gmail.com", 993)
    assert ("select", '"INBOX"', True) in imap.commands  # read-only: listing never marks mail as read
    assert imap.logged_out


def test_list_emails_search_criteria(fake_imap):
    gmail = make_ctx(email_address="me@gmail.com", email_password="pw")
    list_emails(gmail, unread_only=True, query='from:amazon "newer_than:7d"', limit=2)
    search = next(c for c in fake_imap.instances[-1].commands if c[0] == "search")
    assert search[1] == (None, "UNSEEN", "X-GM-RAW", '"from:amazon newer_than:7d"')
    other = make_ctx(email_address="me@outlook.com", email_password="pw")
    list_emails(other, query="invoice")
    search = next(c for c in fake_imap.instances[-1].commands if c[0] == "search")
    assert search[1] == (None, "TEXT", '"invoice"')
    list_emails(other)
    search = next(c for c in fake_imap.instances[-1].commands if c[0] == "search")
    assert search[1] == (None, "ALL")


def test_list_emails_limit(fake_imap):
    out = list_emails(make_ctx(email_address="me@gmail.com", email_password="pw"), limit=1)
    assert [m["id"] for m in out["messages"]] == ["103"]


def test_list_emails_errors(fake_imap):
    ctx = make_ctx(email_address="me@gmail.com", email_password="pw")
    assert "Folder 'Nope' not found" in list_emails(ctx, folder="Nope")["error"]
    assert fake_imap.instances[-1].logged_out
    fake_imap.login_error = email_tools.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    assert "rejected the login" in list_emails(ctx)["error"]
    assert fake_imap.instances[-1].logged_out  # the connection is closed, not leaked
    fake_imap.login_error = OSError("unreachable")
    assert "Couldn't reach the mail server (imap.gmail.com)" in list_emails(ctx)["error"]
    assert fake_imap.instances[-1].logged_out
    fake_imap.login_error = UnicodeEncodeError("ascii", "pässword", 1, 2, "ordinal not in range(128)")
    assert "UnicodeEncodeError" in list_emails(ctx)["error"]
    assert fake_imap.instances[-1].logged_out
    assert list_emails(make_ctx())["error"] == SETUP_HINT


def test_imap_connect_failure_is_friendly(monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(email_tools.imaplib, "IMAP4_SSL", refuse)
    out = list_emails(make_ctx(email_address="me@gmail.com", email_password="pw"))
    assert out["error"].startswith("Couldn't reach the mail server (imap.gmail.com)")


def test_failed_logout_still_closes_the_socket(fake_imap):
    fake_imap.logout_error = OSError("connection reset")
    ctx = make_ctx(email_address="me@gmail.com", email_password="pw")
    assert list_emails(ctx)["messages"]
    assert getattr(fake_imap.instances[-1], "shut_down", False)
    fake_imap.login_error = email_tools.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    assert "rejected the login" in list_emails(ctx)["error"]
    assert getattr(fake_imap.instances[-1], "shut_down", False)


def test_rejected_login_closes_a_real_socket(monkeypatch):
    """End to end over loopback: a server that refuses LOGIN; the client socket must end up closed."""
    import socket
    import threading

    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    seen = []

    def serve():
        conn, _ = srv.accept()
        with conn, conn.makefile("rb") as rf:
            conn.sendall(b"* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] ready\r\n")
            for line in rf:
                tag, _, rest = line.decode().strip().partition(" ")
                seen.append(rest.split(" ", 1)[0].upper())
                if rest.upper().startswith("LOGIN"):
                    conn.sendall(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n".encode())
                elif rest.upper().startswith("LOGOUT"):
                    conn.sendall(f"* BYE\r\n{tag} OK LOGOUT completed\r\n".encode())
                    break
                else:
                    conn.sendall(f"{tag} OK\r\n".encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    made = []

    class PlainIMAP(email_tools.imaplib.IMAP4):  # same protocol without TLS
        def __init__(self, host, port, ssl_context=None, timeout=None):
            super().__init__(host, port, timeout=timeout)
            made.append(self)

    monkeypatch.setattr(email_tools.imaplib, "IMAP4_SSL", PlainIMAP)
    settings = Settings(email_address="me@example.com", email_password="wrong", imap_host="127.0.0.1", imap_port=port)
    try:
        with pytest.raises(ToolError, match="rejected the login"):
            email_tools._imap(settings)
        thread.join(5)
    finally:
        srv.close()
    assert made and made[0].sock.fileno() == -1  # closed
    assert "LOGIN" in seen and seen[-1] == "LOGOUT"


def test_read_email(fake_imap):
    ctx = make_ctx(email_address="me@gmail.com", email_password="pw")
    out = json.loads(run(call_tool("read_email", {"id": "55"}, ctx)))
    assert out["subject"] == "Photos" and out["body"] == "Hello dear"
    assert out["attachments"] == ["beach.jpg"]
    assert out["reply_to_addresses"] == ["mom.alt@example.com"]
    missing = json.loads(run(call_tool("read_email", {"id": "56"}, ctx)))
    assert "not found" in missing["error"]
    bad = json.loads(run(call_tool("read_email", {"id": "1 OR 2"}, ctx)))
    assert "numeric id" in bad["error"]
