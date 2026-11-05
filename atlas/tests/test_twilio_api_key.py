"""Twilio API keys (TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET): which credentials each Twilio REST request signs in
with, friendly format checks for SIDs pasted into the wrong box, the phone line's need for the Auth Token (webhook
signatures), and secrets staying out of /api/state, results and logs.

Every credential below is an obviously fake value; Twilio is an httpx.MockTransport throughout."""
import base64
import json
import time

import httpx
import pytest

from app import config, phone
from app.tools import TOOLS, ToolError, comms
from conftest import make_ctx, run
from test_comms import Recorder, http, tool
from test_phone import (
    LINE_NUMBER, OWNER, PUBLIC, SID, TOKEN, FakeAgent, FakeTwilio, client_for, drain, make_line, post, sms_params,
    start_line,
)
from test_server_phone import FakePhone, app_client, wait_for  # noqa: F401 - app_client is a fixture

KEY_SID = "SK" + "fedcba9876543210" * 2  # fake
KEY_SECRET = "fake-api-key-secret-not-real-0001"
SENDER = "+15557654321"


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


KEY_AUTH = basic(KEY_SID, KEY_SECRET)
TOKEN_AUTH = basic(SID, TOKEN)


@pytest.fixture(autouse=True)
def fresh_approvals(monkeypatch):
    monkeypatch.setattr(comms, "SYSTEM", "Linux")
    comms._PREVIEWED.clear()
    yield
    comms._PREVIEWED.clear()


def texting_env(monkeypatch, *, token=True, key=True, sid=SID):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", sid)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", SENDER)
    if token:
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    if key:
        monkeypatch.setenv("TWILIO_API_KEY_SID", KEY_SID)
        monkeypatch.setenv("TWILIO_API_KEY_SECRET", KEY_SECRET)


def phone_env(monkeypatch, *, token=True, key=True):
    for name, value in {
        "PHONE_ENABLED": "true", "PHONE_NUMBER": LINE_NUMBER, "USER_PHONE": OWNER, "PHONE_PUBLIC_URL": PUBLIC,
        "PHONE_PORT": "0", "TWILIO_ACCOUNT_SID": SID,
    }.items():
        monkeypatch.setenv(name, value)
    if token:
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    if key:
        monkeypatch.setenv("TWILIO_API_KEY_SID", KEY_SID)
        monkeypatch.setenv("TWILIO_API_KEY_SECRET", KEY_SECRET)


# ------------------------------------------------------------------ which credentials
@pytest.mark.parametrize("token, key_sid, key_secret, expected", [
    (TOKEN, KEY_SID, KEY_SECRET, (KEY_SID, KEY_SECRET)),  # the API key is preferred when both halves are set
    ("", KEY_SID, KEY_SECRET, (KEY_SID, KEY_SECRET)),  # ... and needs no Auth Token
    (TOKEN, "", "", (SID, TOKEN)),  # no key: Account SID + Auth Token
    (TOKEN, KEY_SID, "", (SID, TOKEN)),  # half a key falls back to the Auth Token
    (TOKEN, "", KEY_SECRET, (SID, TOKEN)),
])
def test_api_key_is_preferred_and_the_auth_token_is_the_fallback(token, key_sid, key_secret, expected):
    assert comms.twilio_auth(SID, token, key_sid, key_secret) == expected


@pytest.mark.parametrize("args, fragment", [
    (("", TOKEN, KEY_SID, KEY_SECRET), "Add TWILIO_ACCOUNT_SID"),
    ((KEY_SID, "", KEY_SID, KEY_SECRET), "That looks like an API Key SID — put it in API Key SID"),
    ((SID, "", SID, KEY_SECRET), "That looks like your Account SID — put it in Account SID"),
    ((SID, "", "SK-not-hex", KEY_SECRET), "starts with 'SK'"),
    ((SID, "", KEY_SID, ""), "TWILIO_API_KEY_SECRET is missing"),
    ((SID, "", "", KEY_SECRET), "TWILIO_API_KEY_SID is missing"),
    ((SID, "", "", ""), "Add TWILIO_AUTH_TOKEN"),
])
def test_unusable_credentials_say_what_to_fix(args, fragment):
    with pytest.raises(ToolError) as exc:
        comms.twilio_auth(*args)
    assert fragment in str(exc.value)
    assert KEY_SECRET not in str(exc.value) and TOKEN not in str(exc.value)


def test_sid_format_messages():
    assert comms.account_sid_problem(SID) is None and comms.api_key_sid_problem(KEY_SID) is None
    assert comms.account_sid_problem("  " + SID + " ") is None
    sk_in_account = comms.account_sid_problem(KEY_SID)
    assert sk_in_account.startswith("That looks like an API Key SID — put it in API Key SID")
    assert "the Account SID starts with AC" in sk_in_account
    ac_in_key = comms.api_key_sid_problem(SID)
    assert ac_in_key.startswith("That looks like your Account SID") and "starts with SK" in ac_in_key
    assert "starts with 'AC'" in comms.account_sid_problem("AC123")
    assert "starts with 'AC'" in comms.account_sid_problem("ac" + "0" * 32)  # Twilio's prefix is upper case
    assert "starts with 'SK'" in comms.api_key_sid_problem("SK" + "g" * 32)
    assert comms.API_KEY_SID_FORMAT.pattern == r"SK[0-9a-fA-F]{32}"


# ------------------------------------------------------------------ texting
def test_text_signs_in_with_the_api_key_and_keeps_the_account_in_the_url(monkeypatch):
    texting_env(monkeypatch, token=True, key=True)
    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), to="5552345678", message="hi")
    assert out["backend"] == "twilio" and out["to"] == "+15552345678"
    (req,) = rec.requests
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert req.headers["authorization"] == KEY_AUTH
    blob = json.dumps(out)
    assert KEY_SECRET not in blob and TOKEN not in blob


def test_text_works_with_an_api_key_and_no_auth_token(monkeypatch):
    texting_env(monkeypatch, token=False, key=True)
    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), to="5552345678", message="hi")
    assert "error" not in out and rec.requests[0].headers["authorization"] == KEY_AUTH


def test_text_without_a_key_uses_the_auth_token(monkeypatch):
    texting_env(monkeypatch, token=True, key=False)
    rec = Recorder()
    tool("send_text_message", make_ctx(http=http(rec)), to="5552345678", message="hi")
    assert rec.requests[0].headers["authorization"] == TOKEN_AUTH


def test_api_key_sid_in_the_account_sid_setting_is_explained_before_anything_is_sent(monkeypatch):
    texting_env(monkeypatch, token=False, key=True, sid=KEY_SID)
    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), to="5552345678", message="hi")
    assert "That looks like an API Key SID — put it in API Key SID" in out["error"]
    assert rec.requests == []
    card = TOOLS["send_text_message"].preview({"to": "5552345678", "message": "hi"})
    assert dict(card["fields"])["Send via"].startswith("Not available: That looks like an API Key SID")


def test_partial_api_key_setup_names_the_missing_half(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SID)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", SENDER)
    monkeypatch.setenv("TWILIO_API_KEY_SID", KEY_SID)
    out = tool("send_text_message", to="5552345678", message="hi")
    assert "partly set up" in out["error"] and "TWILIO_API_KEY_SECRET" in out["error"]
    monkeypatch.delenv("TWILIO_API_KEY_SID")
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", KEY_SECRET)
    out = tool("send_text_message", to="5552345678", message="hi")
    assert "TWILIO_API_KEY_SID" in out["error"] and KEY_SECRET not in out["error"]
    monkeypatch.delenv("TWILIO_API_KEY_SECRET")
    out = tool("send_text_message", to="5552345678", message="hi")
    assert "add TWILIO_AUTH_TOKEN in ATLAS Settings" in out["error"] and "API key" in out["error"]


@pytest.mark.parametrize("status, body, fragment", [
    (401, {"code": 20003, "message": "Authenticate"}, "Check TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET"),
    (403, {"code": 70051, "message": "Authorization failed"}, "restricted key needs permission"),
    (404, {"code": 20404, "message": "not found"}, "made in that account"),
    (400, {"code": 21608, "message": "unverified"}, "Verified Caller IDs"),  # specific codes still win
])
def test_api_key_rejections_point_at_the_key(monkeypatch, status, body, fragment):
    texting_env(monkeypatch, token=False, key=True)
    out = tool("send_text_message", make_ctx(http=http(Recorder(status=status, body=body))), to="5552345678", message="x")
    assert fragment in out["error"] and KEY_SECRET not in out["error"]


# ------------------------------------------------------------------ the phone line
def test_phone_line_refuses_to_start_without_the_auth_token_but_texting_works_with_the_key(monkeypatch):
    phone_env(monkeypatch, token=False, key=True)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", SENDER)
    line, settings, ctx_factory, twilio = start_line()
    st = run(line.start(settings, None, ctx_factory))
    assert st["status"] == "error" and st["enabled"] is True and st["public_url"] is None
    assert st["error"].startswith(
        "The phone line needs your Twilio Auth Token to verify incoming calls really come from Twilio (an API Key "
        "can't do that). Texting and outbound requests can use the API Key."
    )
    assert line.port is None and twilio.requests == []
    assert KEY_SECRET not in json.dumps(st)

    rec = Recorder()
    out = tool("send_text_message", make_ctx(http=http(rec)), to=OWNER, message="still texting")
    assert out["backend"] == "twilio" and rec.requests[0].headers["authorization"] == KEY_AUTH


def test_phone_line_sets_up_the_number_with_the_api_key(monkeypatch):
    phone_env(monkeypatch, token=True, key=True)
    line, settings, ctx_factory, twilio = start_line()

    async def main():
        st = await line.start(settings, None, ctx_factory)
        await line.stop()
        return st

    st = run(main())
    assert st["status"] == "online", st
    lookup, update = twilio.requests
    assert lookup.method == "GET" and lookup.url.path == f"/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers.json"
    assert update.method == "POST" and f"/Accounts/{SID}/IncomingPhoneNumbers/" in update.url.path
    assert all(r.headers["authorization"] == KEY_AUTH for r in twilio.requests)


def test_phone_line_without_a_key_still_uses_the_auth_token(monkeypatch):
    phone_env(monkeypatch, token=True, key=False)
    line, settings, ctx_factory, twilio = start_line()

    async def main():
        st = await line.start(settings, None, ctx_factory)
        await line.stop()
        return st

    assert run(main())["status"] == "online"
    assert twilio.requests and all(r.headers["authorization"] == TOKEN_AUTH for r in twilio.requests)


def test_phone_line_reports_a_misplaced_sid(monkeypatch):
    phone_env(monkeypatch, token=True, key=True)
    monkeypatch.setenv("TWILIO_API_KEY_SID", SID)  # the Account SID pasted into the API Key SID box
    line, settings, ctx_factory, twilio = start_line()
    st = run(line.start(settings, None, ctx_factory))
    assert st["status"] == "error" and "That looks like your Account SID" in st["error"]
    assert twilio.requests == []
    monkeypatch.setenv("TWILIO_API_KEY_SID", KEY_SID)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", KEY_SID)  # and the other way round
    st = run(line.start(settings, None, ctx_factory))
    assert st["status"] == "error" and "That looks like an API Key SID — put it in API Key SID" in st["error"]


def test_phone_line_rejection_with_a_key_names_the_key_settings(monkeypatch):
    phone_env(monkeypatch, token=True, key=True)
    line, settings, ctx_factory, _ = start_line(twilio=FakeTwilio(list_status=401))
    st = run(line.start(settings, None, ctx_factory))
    assert st["status"] == "error" and "TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET" in st["error"]
    assert KEY_SECRET not in st["error"] and TOKEN not in st["error"]


def test_sms_replies_go_out_with_the_api_key(monkeypatch):
    phone_env(monkeypatch, token=True, key=True)
    line, agent, twilio = make_line(FakeAgent(reply="Sunny."))

    async def main():
        async with client_for(line) as c:
            r = await post(c, "/twilio/sms", sms_params("Weather?"))
            assert r.status_code == 200
        await drain(line)

    run(main())
    assert twilio.messages() == [{"To": OWNER, "From": LINE_NUMBER, "Body": "Sunny."}]
    sent = next(r for r in twilio.requests if r.url.path == f"/2010-04-01/Accounts/{SID}/Messages.json")
    assert sent.headers["authorization"] == KEY_AUTH


def test_webhooks_are_still_verified_with_the_auth_token_not_the_key(monkeypatch):
    phone_env(monkeypatch, token=True, key=True)
    line, agent, twilio = make_line()

    async def main():
        async with client_for(line) as c:
            forged = await post(c, "/twilio/sms", sms_params("hello"), token=KEY_SECRET)  # signed with the key
            assert forged.status_code == 403
            real = await post(c, "/twilio/sms", sms_params("hello"))  # signed with the Auth Token
            assert real.status_code == 200
        await drain(line)

    run(main())
    assert len(agent.calls) == 1


def test_a_failed_sms_reply_never_logs_the_key_secret(monkeypatch, caplog):
    phone_env(monkeypatch, token=True, key=True)
    body = {"code": 21999, "message": f"bad credentials {KEY_SECRET}"}  # even if Twilio echoed it back
    line, _, twilio = make_line(twilio=FakeTwilio(message_status=400, message_body=body))
    with caplog.at_level("WARNING", logger="atlas.phone"):
        assert run(line.send_sms(OWNER, "hello")) is False
    assert "failed" in caplog.text and KEY_SECRET not in caplog.text and TOKEN not in caplog.text
    assert KEY_SECRET not in repr(phone.PhoneConfig(api_key_secret=KEY_SECRET, auth_token=TOKEN))


# ------------------------------------------------------------------ settings, /api/state and /api/setup
def test_new_keys_are_registered():
    from app import server

    assert {"TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET", "PHONE_PIN", "PHONE_LANGUAGE"} <= config.EDITABLE_KEYS
    assert "TWILIO_API_KEY_SECRET" in config.SECRET_KEYS and "PHONE_PIN" in config.SECRET_KEYS
    assert "TWILIO_API_KEY_SID" not in config.SECRET_KEYS and "PHONE_LANGUAGE" not in config.SECRET_KEYS
    assert {"TWILIO_API_KEY_SID", "PHONE_LANGUAGE"} <= server._STATE_KEYS
    assert not server._STATE_KEYS & {"TWILIO_API_KEY_SECRET", "PHONE_PIN"}


def test_state_reports_the_key_sid_but_never_the_secret(app_client, monkeypatch):
    server, open_client = app_client
    FakePhone(monkeypatch)
    with open_client() as client:
        r = client.post("/api/setup", json={"values": {
            "TWILIO_ACCOUNT_SID": SID, "TWILIO_API_KEY_SID": KEY_SID, "TWILIO_API_KEY_SECRET": KEY_SECRET,
            "TWILIO_AUTH_TOKEN": TOKEN, "PHONE_PIN": "482916", "PHONE_LANGUAGE": "en-AU",
        }})
        assert r.status_code == 200, r.text
        body = client.get("/api/state").text
        s = json.loads(body)
        for secret in (KEY_SECRET, TOKEN, "482916"):
            assert secret not in body
        assert s["extra"]["TWILIO_API_KEY_SID"] == KEY_SID and s["extra"]["TWILIO_ACCOUNT_SID"] == SID
        assert s["extra"]["PHONE_LANGUAGE"] == "en-AU"
        assert not {"TWILIO_API_KEY_SECRET", "TWILIO_AUTH_TOKEN", "PHONE_PIN"} & set(s["extra"])
        assert s["secrets_configured"]["TWILIO_API_KEY_SECRET"] is True
        assert s["secrets_configured"]["PHONE_PIN"] is True
        # a blank secret in the form keeps the saved one
        assert client.post("/api/setup", json={"values": {"TWILIO_API_KEY_SECRET": ""}}).json()["updated"] == []
    assert config.load_env_file(config.ENV_PATH)["TWILIO_API_KEY_SECRET"] == KEY_SECRET


@pytest.mark.parametrize("key, value, fragment", [
    ("TWILIO_ACCOUNT_SID", KEY_SID, "That looks like an API Key SID — put it in API Key SID"),
    ("TWILIO_API_KEY_SID", SID, "That looks like your Account SID — put it in Account SID"),
    ("TWILIO_ACCOUNT_SID", "AC-oops", "starts with 'AC'"),
    ("TWILIO_API_KEY_SID", "SK-oops", "starts with 'SK'"),
])
def test_setup_refuses_a_misplaced_sid_and_saves_nothing(app_client, monkeypatch, key, value, fragment):
    server, open_client = app_client
    fake = FakePhone(monkeypatch)
    with open_client() as client:
        r = client.post("/api/setup", json={"values": {"USER_NAME": "Sam", key: value}})
        assert r.status_code == 400 and fragment in r.json()["detail"]
        assert value not in r.text  # the refusal never echoes what was pasted: it could be a secret
        time.sleep(0.1)
        assert fake.starts == []  # nothing was saved, so nothing restarted
        assert not config.ENV_PATH.exists()
        assert client.post("/api/setup", json={"values": {key: ""}}).status_code == 200  # blank clears, unchecked
    assert config.load_env_file(config.ENV_PATH) == {key: ""}


@pytest.mark.parametrize("values", [{"TWILIO_API_KEY_SID": KEY_SID}, {"TWILIO_API_KEY_SECRET": KEY_SECRET}])
def test_saving_either_key_restarts_the_phone_line(app_client, monkeypatch, values):
    server, open_client = app_client
    fake = FakePhone(monkeypatch)
    with open_client() as client:
        assert client.post("/api/setup", json={"values": {"TWILIO_API_KEY_SECRET": ""}}).status_code == 200
        time.sleep(0.1)
        assert fake.starts == []  # a blank secret changes nothing, so nothing restarts
        assert client.post("/api/setup", json={"values": values}).status_code == 200
        wait_for(lambda: len(fake.starts) == 1)


# ------------------------------------------------------------------ review hardening
@pytest.mark.parametrize("bad_sid", [KEY_SID, SID + "/../Keys", "AC" + "0" * 31, ""])
def test_no_twilio_url_is_built_from_an_unchecked_account_sid(bad_sid):
    """configure_twilio_number and _send_twilio check the SID themselves, whoever calls them."""
    twilio, rec = FakeTwilio(), Recorder()
    with pytest.raises(phone.PhoneError):
        run(phone.configure_twilio_number(
            httpx.AsyncClient(transport=httpx.MockTransport(twilio)), bad_sid, (KEY_SID, KEY_SECRET), LINE_NUMBER, PUBLIC
        ))
    backend = {"name": "twilio", "sid": bad_sid, "auth": (KEY_SID, KEY_SECRET), "from": SENDER}
    with pytest.raises(ToolError) as exc:
        run(comms._send_twilio(make_ctx(http=http(rec)), backend, OWNER, "hi"))
    assert KEY_SECRET not in str(exc.value)
    assert twilio.requests == [] and rec.requests == []


def test_padded_account_sid_never_reaches_the_url():
    rec = Recorder()
    backend = {"name": "twilio", "sid": f"  {SID} ", "auth": (KEY_SID, KEY_SECRET), "from": SENDER}
    run(comms._send_twilio(make_ctx(http=http(rec)), backend, OWNER, "hi"))
    assert str(rec.requests[0].url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"


def test_a_line_without_the_auth_token_refuses_every_webhook_even_one_signed_with_the_key(monkeypatch):
    """Belt and braces: even if a line somehow had no Auth Token, nothing is accepted (no unsigned fallback)."""
    phone_env(monkeypatch, token=True, key=True)
    line, agent, twilio = make_line()
    line.cfg = phone.PhoneConfig(**{**line.cfg.__dict__, "auth_token": ""})

    async def main():
        async with client_for(line) as c:
            for token in (KEY_SECRET, "", TOKEN):
                assert (await post(c, "/twilio/sms", sms_params("hello"), token=token)).status_code == 403
            assert (await post(c, "/twilio/voice", sms_params("hello"), signature="")).status_code == 403

    run(main())
    assert agent.calls == [] and twilio.requests == []


def test_twilio_requests_never_carry_credentials_in_the_url(monkeypatch):
    phone_env(monkeypatch, token=True, key=True)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", SENDER)
    rec = Recorder()
    tool("send_text_message", make_ctx(http=http(rec)), to=OWNER, message="hi")
    line, settings, ctx_factory, twilio = start_line()

    async def main():
        await line.start(settings, None, ctx_factory)
        await line.send_sms(OWNER, "hello")
        await line.stop()

    run(main())
    requests = rec.requests + twilio.requests
    assert len(requests) == 4  # the text, the number lookup, the webhook update, the SMS reply
    for r in requests:
        assert r.url.userinfo == b"" and KEY_SECRET not in str(r.url) and TOKEN not in str(r.url)
        assert r.headers["authorization"] == KEY_AUTH
