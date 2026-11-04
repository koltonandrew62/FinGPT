"""Texting and calling tools: number normalisation, contact lookup, Twilio (mocked), Messages via osascript
(subprocess faked) and tel: dialling. Nothing here reaches the network or a real phone."""
import asyncio
import base64
import json
import subprocess
from urllib.parse import parse_qs

import httpx
import pytest

from app.tools import TOOLS, ToolError, call_tool, comms, memory
from conftest import make_ctx, run

SID = "AC" + "0123456789abcdef" * 2
TOKEN = "secret-token"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in comms.TWILIO_KEYS + ("DEFAULT_COUNTRY_CODE",):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(comms, "SYSTEM", "Linux")


@pytest.fixture(autouse=True)
def fresh_approvals():
    comms._PREVIEWED.clear()
    yield
    comms._PREVIEWED.clear()


def tool(name, ctx=None, approve=True, **args):
    """Run a tool the way the agent does for a SENSITIVE_TOOLS entry: render its approval card, then run it."""
    if approve:
        TOOLS[name].preview(dict(args))
    return json.loads(run(call_tool(name, args, ctx or make_ctx())))


def twilio_env(monkeypatch, sender="+15557654321"):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", sender)


def save_contacts(*items):
    memory.save("contacts", [{"aliases": [], "email": "x@example.com", "phone": "", **c} for c in items])


class Recorder:
    def __init__(self, status=201, body=None):
        self.status, self.body, self.requests = status, body, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.body if self.body is not None else {"sid": "SM" + "a" * 32, "status": "queued"}
        return httpx.Response(self.status, json=body)


def http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ E.164
@pytest.mark.parametrize(
    "raw, cc, expected",
    [
        ("(555) 234-5678", None, "+15552345678"),
        ("555.234.5678", "+1", "+15552345678"),
        ("1-555-234-5678", "1", "+15552345678"),
        ("+1 555 234 5678", "+44", "+15552345678"),
        ("tel:+15552345678", None, "+15552345678"),
        ("+44 20 7946 0958", None, "+442079460958"),
        ("(+44) 20 7946 0958", None, "+442079460958"),
        ("0044 20 7946 0958", None, "+442079460958"),
        ("011 44 20 7946 0958", "+1", "+442079460958"),
        ("020 7946 0958", "+44", "+442079460958"),
        ("07911 123456", "44", "+447911123456"),
        ("0412 345 678", "+61", "+61412345678"),
        ("06 1234 5678", "+39", "+390612345678"),  # Italy keeps the leading 0
        ("8 (495) 123-45-67", "+7", "+74951234567"),  # Russia's trunk prefix is 8
        ("+49 30 123456", None, "+4930123456"),
    ],
)
def test_normalize_phone(raw, cc, expected):
    assert comms.normalize_phone(raw, cc) == expected


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("234-5678", "area code"),
        ("911", "emergency"),
        ("40404", "short"),
        ("+1 055 234 5678", "can't start with 0 or 1"),
        ("+1 555 234 567", "10 digits after +1"),
        ("12345678901234", "10-digit"),
        ("+0 123 456 789", "country code"),
        ("+1234567890123456", "too long"),
        ("+12345", "too short"),
        ("call me maybe", "doesn't look like a phone number"),
    ],
)
def test_normalize_phone_rejects(raw, fragment):
    with pytest.raises(ToolError) as exc:
        comms.normalize_phone(raw, "+1")
    assert fragment in str(exc.value)


def test_default_country_code_setting(monkeypatch, isolated):
    assert comms.country_code() == "1"
    isolated["env_path"].write_text("DEFAULT_COUNTRY_CODE=+44\n")
    assert comms.normalize_phone("020 7946 0958") == "+442079460958"
    monkeypatch.setenv("DEFAULT_COUNTRY_CODE", "0061")
    assert comms.country_code() == "61"
    monkeypatch.setenv("DEFAULT_COUNTRY_CODE", "UK")
    with pytest.raises(ToolError, match="DEFAULT_COUNTRY_CODE"):
        comms.normalize_phone("020 7946 0958")


def test_extensions_and_pretty():
    assert comms._split_extension("+1 555 234 5678 ext. 12") == ("+1 555 234 5678", "12")
    assert comms._split_extension("555-234-5678 x9") == ("555-234-5678", "9")
    assert comms._split_extension("555-234-5678,,4") == ("555-234-5678", "4")
    assert comms._split_extension("Max") == ("Max", "")
    assert comms.pretty_phone("+15552345678") == "+1 (555) 234-5678"
    assert comms.pretty_phone("+442079460958") == "+442079460958"
    assert comms.looks_like_phone("(555) 234-5678") and not comms.looks_like_phone("Mom")
    assert not comms.looks_like_phone("x12")


# ------------------------------------------------------------------ recipients
def test_resolve_recipient_contacts():
    save_contacts(
        {"name": "Maya Chen", "aliases": ["mom"], "phone": "555-234-5678"},
        {"name": "Sam Lee", "phone": "+44 7911 123456"},
        {"name": "Sam Ortiz", "phone": "+1 555 222 3333"},
        {"name": "Coach Rivera"},
        {"name": "Bad Number", "phone": "ask her"},
    )
    mom = comms.resolve_recipient("mom")
    assert mom == {"name": "Maya Chen", "number": "+15552345678", "extension": "", "display": "+1 (555) 234-5678"}
    assert comms.resolve_recipient("Sam Lee")["number"] == "+447911123456"
    assert comms.resolve_recipient("(555) 234-5678")["name"] is None
    with pytest.raises(ToolError, match="no phone number.*save_contact"):
        comms.resolve_recipient("Coach Rivera")
    with pytest.raises(ToolError, match="Several saved contacts match 'sam': Sam Lee, Sam Ortiz"):
        comms.resolve_recipient("sam")
    with pytest.raises(ToolError, match="don't have a saved contact called 'Zed'.*save_contact"):
        comms.resolve_recipient("Zed")
    with pytest.raises(ToolError, match="saved for Bad Number"):
        comms.resolve_recipient("Bad Number")
    with pytest.raises(ToolError, match="Who should I contact"):
        comms.resolve_recipient("  ")


# ------------------------------------------------------------------ Twilio
def test_twilio_send(monkeypatch):
    twilio_env(monkeypatch)
    save_contacts({"name": "Maya Chen", "aliases": ["mom"], "phone": "(555) 234-5678"})
    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), to="mom", message='On my way — "10 min" \\ ok')
    assert out["backend"] == "twilio" and out["recipient"] == "Maya Chen" and out["to"] == "+15552345678"
    assert out["sid"].startswith("SM") and out["twilio_status"] == "queued"
    assert out["status"] == "Text to Maya Chen accepted by Twilio"
    (req,) = rec.requests
    assert req.method == "POST"
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()
    form = parse_qs(req.content.decode())
    assert form == {"To": ["+15552345678"], "From": ["+15557654321"], "Body": ['On my way — "10 min" \\ ok']}


def test_twilio_messaging_service_sender(monkeypatch):
    twilio_env(monkeypatch, sender="MG" + "f" * 32)
    rec = Recorder()
    tool("send_text_message", make_ctx(http=http(rec)), to="+44 7911 123456", message="hi")
    form = parse_qs(rec.requests[0].content.decode())
    assert form["MessagingServiceSid"] == ["MG" + "f" * 32] and "From" not in form
    assert form["To"] == ["+447911123456"]


@pytest.mark.parametrize(
    "status, body, fragment",
    [
        (401, {"code": 20003, "message": "Authenticate"}, "rejected the credentials"),
        (400, {"code": 21608, "message": "unverified"}, "Verified Caller IDs"),
        (400, {"code": 21211, "message": "Invalid 'To'"}, "isn't a valid phone number"),
        (400, {"code": 21610, "message": "unsubscribed"}, "STOP"),
        (400, {"code": 21999, "message": "Something odd"}, "(400, error 21999): Something odd"),
        (404, {"code": 20404, "message": "not found"}, "TWILIO_ACCOUNT_SID"),
        (500, None, "Twilio couldn't send the text (500)"),
    ],
)
def test_twilio_errors(monkeypatch, status, body, fragment):
    twilio_env(monkeypatch)

    def handler(request):
        if body is None:
            return httpx.Response(status, text="oops")
        return httpx.Response(status, json=body)

    out = tool("send_text_message", make_ctx(http=http(handler)), to="555 234 5678", message="hi")
    assert fragment in out["error"]
    if body and body["code"] == 21608:
        assert "+1 (555) 234-5678" in out["error"] and "trial" in out["error"]


def test_twilio_config_problems(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SID)
    out = tool("send_text_message", to="555 234 5678", message="hi")
    assert "partly set up" in out["error"] and "TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER" in out["error"]
    twilio_env(monkeypatch)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "not-a-sid/../x")
    assert "starts with 'AC'" in tool("send_text_message", make_ctx(http=http(Recorder())), to="5552345678", message="x")["error"]
    twilio_env(monkeypatch)
    out = tool("send_text_message", make_ctx(http=http(Recorder())), to="5552345678", message="x" * 1601)
    assert "limit is 1600" in out["error"]


def test_twilio_network_errors(monkeypatch):
    twilio_env(monkeypatch)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("nope", request=request)

    assert "can't confirm" in tool("send_text_message", make_ctx(http=http(timeout)), to="5552345678", message="x")["error"]
    assert "Couldn't reach Twilio" in tool("send_text_message", make_ctx(http=http(refused)), to="5552345678", message="x")["error"]


def test_text_rejects_empty_and_extension(monkeypatch):
    twilio_env(monkeypatch)
    assert tool("send_text_message", to="5552345678", message="  ")["error"] == "The message is empty."
    assert "landline" in tool("send_text_message", to="555-234-5678 ext 4", message="hi")["error"]


def test_no_backend_gives_setup_help():
    out = tool("send_text_message", to="5552345678", message="hi")
    assert "TWILIO_ACCOUNT_SID" in out["error"] and "twilio.com" in out["error"] and "Mac" in out["error"]


# ------------------------------------------------------------------ Messages (macOS)
class FakeRun:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        code, err = self.results.pop(0)
        return subprocess.CompletedProcess(args, code, stdout="", stderr=err)


def test_messages_app_passes_text_as_argv(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")
    fake = FakeRun([(0, "")])
    monkeypatch.setattr(comms.subprocess, "run", fake)
    nasty = 'He said "hi" \\ end tell\n do shell script "rm -rf ~" -- -e'
    out = tool("send_text_message", to="+1 555 234 5678", message=nasty)
    assert out["backend"] == "messages" and out["to"] == "+15552345678"
    (args, kwargs), = fake.calls
    assert args[0] == "osascript" and kwargs["timeout"] == 30
    assert args[-2:] == ["+15552345678", nasty]  # verbatim, as run-handler arguments
    script = args[1:-2]
    assert script[0::2] == ["-e"] * (len(script) // 2)
    lines = script[1::2]
    assert lines == list(comms._MESSAGES_SCRIPT)
    assert not any("rm -rf" in line or "555" in line for line in lines)  # user text never becomes code
    assert "service type = iMessage" in "\n".join(lines)


def test_messages_app_falls_back_to_legacy_dictionary(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")
    fake = FakeRun([(1, "0:52: syntax error: Expected class name but found identifier. (-2741)"), (0, "")])
    monkeypatch.setattr(comms.subprocess, "run", fake)
    assert tool("send_text_message", to="5552345678", message="hi")["backend"] == "messages"
    legacy = fake.calls[1][0]
    assert "set targetBuddy to buddy targetNumber of targetService" in legacy
    assert "set targetService to 1st service whose service type = iMessage" in legacy


@pytest.mark.parametrize(
    "stderr, fragment",
    [
        ("execution error: Not authorized to send Apple events to Messages. (-1743)", "Privacy & Security → Automation"),
        ("execution error: Messages got an error: Can’t get account 1 whose service type = iMessage. (-1728)", "signed in to iMessage"),
        ("execution error: something else (-1)", "Messages couldn't send the text: execution error"),
    ],
)
def test_messages_app_errors(monkeypatch, stderr, fragment):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")
    fake = FakeRun([(1, stderr)])
    monkeypatch.setattr(comms.subprocess, "run", fake)
    assert fragment in tool("send_text_message", to="5552345678", message="hi")["error"]
    assert len(fake.calls) == 1  # runtime errors are not retried (it may have sent)


def test_messages_timeout(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(comms.subprocess, "run", slow)
    assert "didn't respond" in tool("send_text_message", to="5552345678", message="hi")["error"]


def test_twilio_preferred_over_messages_on_mac(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")
    twilio_env(monkeypatch)
    monkeypatch.setattr(comms.subprocess, "run", FakeRun([]))  # would raise if called
    assert tool("send_text_message", make_ctx(http=http(Recorder())), to="5552345678", message="hi")["backend"] == "twilio"


# ------------------------------------------------------------------ previews
def test_text_preview(monkeypatch):
    save_contacts({"name": "Maya Chen", "aliases": ["mom"], "phone": "555-234-5678"})
    twilio_env(monkeypatch)
    p = TOOLS["send_text_message"].preview({"to": "mom", "message": "Line one\nLine two"})
    assert p["title"] == "Send text message" and p["body"] == "Line one\nLine two"
    assert p["fields"] == [
        ["To", "Maya Chen"],
        ["Number", "+1 (555) 234-5678 (+15552345678)"],
        ["Send via", "Twilio SMS from +15557654321"],
    ]
    # Never raises, even when nothing can be resolved.
    for k in comms.TWILIO_KEYS:
        monkeypatch.delenv(k)
    bad = TOOLS["send_text_message"].preview({"to": "Nobody", "message": "x"})
    assert bad["fields"][0] == ["To", "Nobody"]
    assert bad["fields"][1][1].startswith("Can't resolve") and bad["fields"][2][1].startswith("Not available")
    assert all(isinstance(f, list) and len(f) == 2 for f in bad["fields"])


def test_call_preview():
    p = TOOLS["make_phone_call"].preview({"to": "+44 20 7946 0958 x7"})
    assert p["title"] == "Place a phone call"
    assert ["Number", "+442079460958"] in p["fields"] and ["Extension", "7"] in p["fields"]
    assert isinstance(p["body"], str) and p["body"]


# ------------------------------------------------------------------ calls
def test_call_windows(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Windows")
    opened = []
    monkeypatch.setattr(comms.os, "startfile", opened.append, raising=False)
    save_contacts({"name": "Maya Chen", "aliases": ["mom"], "phone": "555-234-5678"})
    out = tool("make_phone_call", to="mom")
    assert opened == ["tel:+15552345678"]
    assert out["opened"] == "tel:+15552345678" and out["recipient"] == "Maya Chen"
    assert "Phone Link" in out["app"] and "press Call" in out["note"]

    def no_handler(uri):
        raise OSError("no association")

    monkeypatch.setattr(comms.os, "startfile", no_handler, raising=False)
    assert "Phone Link" in tool("make_phone_call", to="mom")["error"]


def test_call_mac(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")
    fake = FakeRun([(0, ""), (1, "No application knows how to open URL tel:+15552345678")])
    monkeypatch.setattr(comms.subprocess, "run", fake)
    out = tool("make_phone_call", to="555 234 5678 ext. 21")
    assert fake.calls[0][0] == ["open", "tel:+15552345678"]
    assert "FaceTime" in out["app"] and "extension 21" in out["note"]
    assert "Calls from iPhone" in tool("make_phone_call", to="5552345678")["error"]


class FakePopen:
    def __init__(self, code=0, hang=False):
        self.code, self.hang, self.args = code, hang, None

    def __call__(self, args, **kwargs):
        self.args = args
        assert kwargs["start_new_session"] is True
        return self

    def wait(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.code


def test_call_linux(monkeypatch):
    monkeypatch.setattr(comms.shutil, "which", lambda name: "/usr/bin/xdg-open")
    ok = FakePopen()
    monkeypatch.setattr(comms.subprocess, "Popen", ok)
    assert tool("make_phone_call", to="+49 30 123456")["opened"] == "tel:+4930123456"
    assert ok.args == ["/usr/bin/xdg-open", "tel:+4930123456"]
    monkeypatch.setattr(comms.subprocess, "Popen", FakePopen(hang=True))
    assert tool("make_phone_call", to="+49 30 123456")["opened"] == "tel:+4930123456"
    monkeypatch.setattr(comms.subprocess, "Popen", FakePopen(code=4))
    assert "No app on this computer handles phone" in tool("make_phone_call", to="+49 30 123456")["error"]
    monkeypatch.setattr(comms.shutil, "which", lambda name: None)
    assert "xdg-open" in tool("make_phone_call", to="+49 30 123456")["error"]


def test_call_refuses_emergency_and_unknown(monkeypatch):
    monkeypatch.setattr(comms.subprocess, "Popen", FakePopen())  # must not be reached
    assert "dial directly" in tool("make_phone_call", to="911")["error"]
    assert "save_contact" in tool("make_phone_call", to="The Plumber")["error"]


def test_registered_schemas():
    for name in ("send_text_message", "make_phone_call"):
        t = TOOLS[name]
        assert t.preview is not None and t.description
        assert set(t.parameters["required"]) <= set(t.parameters["properties"])


# ------------------------------------------------------------------ review hardening
def test_refuses_without_approval_card(monkeypatch):
    """If the agent never rendered the card (e.g. the tool is missing from SENSITIVE_TOOLS), nothing goes out."""
    twilio_env(monkeypatch)
    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), approve=False, to="5552345678", message="wire $500")
    assert "Approve/Deny card" in out["error"] and rec.requests == []
    # A card for a different message doesn't approve this one.
    TOOLS["send_text_message"].preview({"to": "5552345678", "message": "hi"})
    out = tool("send_text_message", make_ctx(http=http(rec)), approve=False, to="5552345678", message="wire $500")
    assert "Approve/Deny card" in out["error"] and rec.requests == []
    # Nor for a different recipient.
    TOOLS["send_text_message"].preview({"to": "5559998888", "message": "wire $500"})
    out = tool("send_text_message", make_ctx(http=http(rec)), approve=False, to="5552345678", message="wire $500")
    assert "Approve/Deny card" in out["error"] and rec.requests == []

    monkeypatch.setattr(comms, "SYSTEM", "Windows")
    opened = []
    monkeypatch.setattr(comms.os, "startfile", opened.append, raising=False)
    out = tool("make_phone_call", approve=False, to="+1 555 234 5678")
    assert "Approve/Deny card" in out["error"] and opened == []


def test_approval_expires_and_auto_approve(monkeypatch):
    twilio_env(monkeypatch)
    rec = Recorder()
    TOOLS["send_text_message"].preview({"to": "5552345678", "message": "hi"})
    key = comms._approval_key("send_text_message", "5552345678", "hi", "+15552345678")
    comms._PREVIEWED[key] -= comms.APPROVAL_TTL + 1  # the card was shown long ago
    out = tool("send_text_message", make_ctx(http=http(rec)), approve=False, to="5552345678", message="hi")
    assert "Approve/Deny card" in out["error"] and rec.requests == []
    # The user explicitly put the tool in AUTO_APPROVE: no card needed.
    ctx = make_ctx(http=http(rec), auto_approve={"send_text_message"})
    assert tool("send_text_message", ctx, approve=False, to="5552345678", message="hi")["backend"] == "twilio"
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    "raw, cc, expected",
    [
        ("+44 (0)20 7946 0958", None, "+442079460958"),  # the (0) is only dialled inside the UK
        ("+44 (0) 7911 123456", "+44", "+447911123456"),
        ("+39 (0)6 1234 5678", None, "+390612345678"),  # Italy keeps its 0
    ],
)
def test_normalize_drops_bracketed_trunk_zero(raw, cc, expected):
    assert comms.normalize_phone(raw, cc) == expected


@pytest.mark.parametrize("raw", ["(911) 555-1234", "+1 411 555 1234", "1-611-555-1234"])
def test_normalize_rejects_n11_area_codes(raw):
    with pytest.raises(ToolError, match="service code"):
        comms.normalize_phone(raw, "+1")


def test_call_refuses_n11_number(monkeypatch):
    monkeypatch.setattr(comms.subprocess, "Popen", FakePopen())
    assert "service code" in tool("make_phone_call", to="(911) 555-1234")["error"]


def test_recipient_given_as_list():
    assert comms.resolve_recipient(["555 234 5678"])["number"] == "+15552345678"
    with pytest.raises(ToolError, match="one recipient at a time"):
        comms.resolve_recipient(["5552345678", "5559998888"])


def test_messages_strips_control_characters(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")
    fake = FakeRun([(0, "")])
    monkeypatch.setattr(comms.subprocess, "run", fake)
    out = tool("send_text_message", to="+1 555 234 5678", message="hi\x00 there\x07\nbye")
    assert out["backend"] == "messages"
    assert fake.calls[0][0][-1] == "hi there\nbye"  # NUL would make subprocess raise ValueError
    p = TOOLS["send_text_message"].preview({"to": "+1 555 234 5678", "message": "a\x00b"})
    assert p["body"] == "ab"  # the card shows exactly what is sent


def test_messages_oserror_is_friendly(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Darwin")

    def too_long(args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(comms.subprocess, "run", too_long)
    assert "Argument list too long" in tool("send_text_message", to="5552345678", message="hi")["error"]


def test_twilio_short_code_sender(monkeypatch):
    twilio_env(monkeypatch, sender="88222")
    rec = Recorder()
    tool("send_text_message", make_ctx(http=http(rec)), to="5552345678", message="hi")
    assert parse_qs(rec.requests[0].content.decode())["From"] == ["88222"]


def test_call_linux_popen_failure(monkeypatch):
    monkeypatch.setattr(comms.shutil, "which", lambda name: "/usr/bin/xdg-open")

    def boom(args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(comms.subprocess, "Popen", boom)
    assert "Permission denied" in tool("make_phone_call", to="+49 30 123456")["error"]


def test_secrets_never_in_results_or_cards(monkeypatch):
    twilio_env(monkeypatch)
    out = tool("send_text_message", make_ctx(http=http(Recorder())), to="5552345678", message="hi")
    p = TOOLS["send_text_message"].preview({"to": "5552345678", "message": "hi"})
    assert TOKEN not in json.dumps(out) and TOKEN not in json.dumps(p)
    for status in (401, 400, 500):
        err = tool("send_text_message", make_ctx(http=http(Recorder(status=status, body={"code": 1}))), to="5552345678", message="x")
        assert TOKEN not in err["error"]


# ------------------------------------------------------------------ end to end through the real agent loop
class ScriptedLLM:
    def __init__(self, *steps):
        self.steps = list(steps)

    async def resolve_model(self):
        return "fake/model"

    async def stream_chat(self, messages, model, tools=None):
        for ev in self.steps.pop(0) if self.steps else [{"type": "text", "text": "ok"}]:
            yield ev


def _agent_turn(monkeypatch, sensitive: bool, approve: bool, rec):
    from app import agent, config

    base = config.SENSITIVE_TOOLS - {"send_text_message"}
    monkeypatch.setattr(config, "SENSITIVE_TOOLS", base | {"send_text_message"} if sensitive else base)
    call = {"id": "c1", "name": "send_text_message", "arguments": json.dumps({"to": "5552345678", "message": "hi"})}
    llm = ScriptedLLM([{"type": "tool_calls", "calls": [call]}], [{"type": "text", "text": "Done."}])
    broker = agent.ConfirmationBroker()

    async def go():
        events = []
        async for ev in agent.run_turn([{"role": "user", "content": "text"}], config.Settings(), llm, broker, make_ctx(http=http(rec))):
            events.append(ev)
            if ev["type"] == "confirm":
                asyncio.get_running_loop().call_soon(broker.resolve, ev["id"], approve)
        return events

    return run(go())


def test_agent_flow_approved_denied_and_unregistered(monkeypatch):
    twilio_env(monkeypatch)
    rec = Recorder()
    events = _agent_turn(monkeypatch, sensitive=True, approve=True, rec=rec)
    (card,) = [e for e in events if e["type"] == "confirm"]
    assert card["title"] == "Send text message" and card["body"] == "hi"
    assert len(rec.requests) == 1
    comms._PREVIEWED.clear()
    events = _agent_turn(monkeypatch, sensitive=True, approve=False, rec=rec)
    assert len(rec.requests) == 1  # denied: nothing sent
    comms._PREVIEWED.clear()
    events = _agent_turn(monkeypatch, sensitive=False, approve=True, rec=rec)
    assert not [e for e in events if e["type"] == "confirm"]  # integration forgotten: no card...
    assert len(rec.requests) == 1  # ...and still nothing sent
    errors = [e for e in events if e["type"] == "tool" and e.get("state") == "error"]
    assert errors and "Approve/Deny card" in errors[0]["summary"]


def test_approval_is_bound_to_the_number_on_the_card(monkeypatch):
    """If the contact's number changes between the card and the send, the old approval doesn't cover it."""
    twilio_env(monkeypatch)
    save_contacts({"name": "Maya Chen", "aliases": ["mom"], "phone": "555-234-5678"})
    p = TOOLS["send_text_message"].preview({"to": "mom", "message": "hi"})
    assert ["Number", "+1 (555) 234-5678 (+15552345678)"] in p["fields"]
    save_contacts({"name": "Maya Chen", "aliases": ["mom"], "phone": "555-999-0000"})  # changed meanwhile
    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), approve=False, to="mom", message="hi")
    assert "Approve/Deny card" in out["error"] and rec.requests == []
    TOOLS["make_phone_call"].preview({"to": "mom"})
    save_contacts({"name": "Maya Chen", "aliases": ["mom"], "phone": "555-234-5678"})
    monkeypatch.setattr(comms.subprocess, "Popen", FakePopen())
    monkeypatch.setattr(comms.shutil, "which", lambda name: "/usr/bin/xdg-open")
    assert "Approve/Deny card" in tool("make_phone_call", approve=False, to="mom")["error"]
