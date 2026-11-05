"""The phone line wired into the main server: started in the background by the lifespan when PHONE_ENABLED, never
blocking or breaking startup, stopped on shutdown, restarted when /api/setup changes a phone or Twilio setting, and
reported in /api/state["phone"]. Offline throughout: Twilio is an httpx.MockTransport."""
import asyncio
import json
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, phone
from conftest import no_network
from test_phone import LINE_NUMBER, OWNER, SID, TOKEN, FakeTwilio

PUBLIC = "https://atlas-phone-test.example.com"
STATUS_KEYS = {"enabled", "number", "status", "public_url", "error"}
PHONE_ENV = (
    "PHONE_ENABLED", "PHONE_NUMBER", "PHONE_ALLOWED_NUMBERS", "PHONE_PUBLIC_URL", "PHONE_TUNNEL", "PHONE_VOICE",
    "PHONE_LANGUAGE", "PHONE_PORT", "PHONE_PIN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "USER_PHONE", "DEFAULT_COUNTRY_CODE",
)


@pytest.fixture(autouse=True)
def clean_phone_env(monkeypatch):
    for key in PHONE_ENV:
        monkeypatch.delenv(key, raising=False)


class FakePhone:
    """Stands in for app.phone's module API (start / stop / status) and records how the server drives it."""

    def __init__(self, monkeypatch, *, delay: float = 0.0, error: Exception | None = None):
        self.starts: list[tuple] = []
        self.stops = 0
        self.cancelled = 0
        self.delay, self.error = delay, error
        self.state = {"enabled": False, "number": "", "status": "off", "public_url": None, "error": None}
        monkeypatch.setattr(phone, "start", self.start)
        monkeypatch.setattr(phone, "stop", self.stop)
        monkeypatch.setattr(phone, "status", lambda: dict(self.state))

    async def start(self, settings, llm, ctx_factory):
        self.starts.append((settings, llm, ctx_factory))
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error is not None:
            raise self.error
        self.state = {"enabled": True, "number": LINE_NUMBER, "status": "online", "public_url": PUBLIC, "error": None}
        return dict(self.state)

    async def stop(self):
        self.stops += 1
        self.state = dict(self.state, status="off", public_url=None, error=None)


def offline(monkeypatch, handler=no_network):
    """The app's own HTTP clients reach only `handler` (Twilio's mock, or nothing at all)."""
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def wait_for(check, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)


@pytest.fixture
def app_client(isolated, monkeypatch):
    """Start the main app (lifespan on) with whatever .env / environment the test set up first."""
    from app import server

    offline(monkeypatch)

    def open_client():
        monkeypatch.setattr(server.S, "settings", config.Settings.load())
        client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        client.headers["X-Atlas-Token"] = server.S.token
        return client

    return server, open_client


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------ settings
def test_phone_settings_are_editable_and_only_the_pin_is_secret():
    keys = {"PHONE_ENABLED", "PHONE_NUMBER", "PHONE_ALLOWED_NUMBERS", "PHONE_PUBLIC_URL", "PHONE_TUNNEL",
            "PHONE_VOICE", "PHONE_LANGUAGE", "PHONE_PORT", "PHONE_PIN"}
    assert keys <= config.EDITABLE_KEYS
    assert {k for k in keys if k in config.SECRET_KEYS} == {"PHONE_PIN"}


def test_state_reports_phone_settings_but_never_the_pin(app_client, monkeypatch):
    server, open_client = app_client
    FakePhone(monkeypatch)
    with open_client() as client:
        r = client.post("/api/setup", json={"values": {
            "PHONE_NUMBER": LINE_NUMBER, "PHONE_PORT": "8791", "PHONE_TUNNEL": "none", "PHONE_PUBLIC_URL": PUBLIC,
            "PHONE_VOICE": "Polly.Amy-Neural", "PHONE_LANGUAGE": "en-US", "PHONE_ALLOWED_NUMBERS": OWNER,
            "PHONE_PIN": "482916",
        }})
        assert r.status_code == 200
        body = client.get("/api/state").text
        s = json.loads(body)
        assert "482916" not in body
        assert s["secrets_configured"]["PHONE_PIN"] is True
        assert "PHONE_PIN" not in s["extra"]
        assert s["extra"]["PHONE_NUMBER"] == LINE_NUMBER and s["extra"]["PHONE_PORT"] == "8791"
        assert s["extra"]["PHONE_TUNNEL"] == "none" and s["extra"]["PHONE_PUBLIC_URL"] == PUBLIC
        assert s["extra"]["PHONE_VOICE"] == "Polly.Amy-Neural" and s["extra"]["PHONE_LANGUAGE"] == "en-US"
        assert s["extra"]["PHONE_ALLOWED_NUMBERS"] == OWNER and s["extra"]["PHONE_ENABLED"] == ""
        # a blank PIN in the form keeps the saved one
        assert client.post("/api/setup", json={"values": {"PHONE_PIN": ""}}).json()["updated"] == []
    assert config.load_env_file(config.ENV_PATH)["PHONE_PIN"] == "482916"


def test_state_phone_is_the_lines_status(app_client, monkeypatch):
    server, open_client = app_client
    with open_client() as client:
        st = client.get("/api/state").json()["phone"]
    assert set(st) == STATUS_KEYS
    assert st["status"] in {"off", "starting", "online", "error"}


# ------------------------------------------------------------------ lifespan
def test_lifespan_leaves_the_phone_line_off_when_disabled(app_client, monkeypatch):
    server, open_client = app_client
    fake = FakePhone(monkeypatch)
    with open_client() as client:
        assert client.get("/api/state").json()["phone"]["status"] == "off"
        assert fake.starts == []
    assert fake.starts == [] and fake.stops == 1  # stop() is safe to call any time


def test_lifespan_starts_the_phone_line_in_the_background_and_stops_it(app_client, monkeypatch):
    server, open_client = app_client
    monkeypatch.setenv("PHONE_ENABLED", "true")
    fake = FakePhone(monkeypatch)
    with open_client() as client:
        wait_for(lambda: fake.starts)
        settings, llm, ctx_factory = fake.starts[0]
        assert settings is server.S.settings and llm is server.S.llm
        assert ctx_factory == server.S.ctx  # called again for each phone turn, so reloaded settings apply
        assert ctx_factory().http is server.S.http
        st = wait_for(lambda: (p := client.get("/api/state").json()["phone"])["status"] == "online" and p)
        assert st == {"enabled": True, "number": LINE_NUMBER, "status": "online", "public_url": PUBLIC, "error": None}
    assert len(fake.starts) == 1 and fake.stops == 1


def test_a_slow_phone_start_never_holds_up_the_app_and_is_cancelled_on_shutdown(app_client, monkeypatch):
    server, open_client = app_client
    monkeypatch.setenv("PHONE_ENABLED", "true")
    fake = FakePhone(monkeypatch, delay=60)
    began = time.monotonic()
    with open_client() as client:
        wait_for(lambda: fake.starts)
        s = client.get("/api/state")
        assert s.status_code == 200
        assert s.json()["phone"]["status"] == "starting" and s.json()["phone"]["enabled"] is True
        assert client.get("/api/reminders").status_code == 200
    assert time.monotonic() - began < 10
    assert fake.cancelled == 1 and fake.stops == 1


def test_a_failing_phone_start_doesnt_break_the_app(app_client, monkeypatch):
    server, open_client = app_client
    monkeypatch.setenv("PHONE_ENABLED", "true")
    fake = FakePhone(monkeypatch, error=RuntimeError("tunnel exploded"))
    with open_client() as client:
        wait_for(lambda: server.S.phone_task is not None and server.S.phone_task.done())
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/setup", json={"values": {"USER_NAME": "Sam"}}).status_code == 200
    assert fake.stops == 1


def test_a_failing_phone_stop_doesnt_break_shutdown(app_client, monkeypatch):
    server, open_client = app_client
    fake = FakePhone(monkeypatch)

    async def broken_stop():
        raise RuntimeError("already gone")

    monkeypatch.setattr(phone, "stop", broken_stop)
    with open_client():
        pass
    assert server.S.http.is_closed and fake.starts == []


# ------------------------------------------------------------------ /api/setup restarts
def test_setup_restarts_the_phone_line_only_for_phone_settings(app_client, monkeypatch):
    server, open_client = app_client
    fake = FakePhone(monkeypatch)
    with open_client() as client:
        for values in ({"USER_NAME": "Sam"}, {"HOME_CITY": "Leeds"}, {"TWILIO_AUTH_TOKEN": ""}, {"PHONE_PIN": ""}):
            assert client.post("/api/setup", json={"values": values}).status_code == 200
        time.sleep(0.2)
        assert fake.starts == []
        expected = 0
        for values in (
            {"PHONE_ENABLED": "true"}, {"PHONE_NUMBER": LINE_NUMBER}, {"TWILIO_ACCOUNT_SID": SID},
            {"TWILIO_AUTH_TOKEN": TOKEN}, {"USER_PHONE": OWNER}, {"DEFAULT_COUNTRY_CODE": "+44"},
            {"PHONE_PIN": "123456"}, {"PHONE_LANGUAGE": "en-US"},
        ):
            assert client.post("/api/setup", json={"values": values}).status_code == 200
            expected += 1
            wait_for(lambda: len(fake.starts) == expected)
            # the restart sees the settings just saved
            assert fake.starts[-1][0] is server.S.settings
    assert fake.stops == 1


def test_a_newer_restart_replaces_one_still_starting(app_client, monkeypatch):
    server, open_client = app_client
    fake = FakePhone(monkeypatch, delay=60)
    with open_client() as client:
        client.post("/api/setup", json={"values": {"PHONE_ENABLED": "true"}})
        wait_for(lambda: len(fake.starts) == 1)
        assert client.get("/api/state").json()["phone"]["status"] == "starting"
        client.post("/api/setup", json={"values": {"PHONE_NUMBER": LINE_NUMBER}})
        wait_for(lambda: len(fake.starts) == 2 and fake.cancelled == 1)  # one start at a time, newest wins
        client.post("/api/setup", json={"values": {"PHONE_ENABLED": "false"}})
        wait_for(lambda: len(fake.starts) == 3 and fake.cancelled == 2)
        # switched off: the queued restart is heading for "off", not "starting"
        st = client.get("/api/state").json()["phone"]
        assert st["status"] == "off" and st["enabled"] is False
    assert fake.cancelled == 3 and fake.stops == 1


# ------------------------------------------------------------------ the real phone line, end to end (offline)
def test_the_real_phone_line_comes_online_restarts_and_stops_with_the_app(app_client, monkeypatch):
    server, open_client = app_client
    line = phone.PhoneLine()
    monkeypatch.setattr(phone, "LINE", line)  # a fresh line, not the module's shared one
    twilio = FakeTwilio()
    offline(monkeypatch, twilio)
    # In .env (not the environment) so /api/setup can change them.
    config.update_env_file({
        "PHONE_ENABLED": "true", "PHONE_NUMBER": LINE_NUMBER, "USER_PHONE": OWNER, "PHONE_PUBLIC_URL": PUBLIC,
        "PHONE_TUNNEL": "none", "PHONE_PORT": "0", "TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": TOKEN,
    })
    with open_client() as client:
        st = wait_for(lambda: (p := client.get("/api/state").json()["phone"])["status"] != "starting" and p)
        assert st == {"enabled": True, "number": LINE_NUMBER, "status": "online", "public_url": PUBLIC, "error": None}
        assert twilio.updates() == [{
            "VoiceUrl": f"{PUBLIC}/twilio/voice", "VoiceMethod": "POST",
            "SmsUrl": f"{PUBLIC}/twilio/sms", "SmsMethod": "POST",
        }]
        assert line.ctx_factory == server.S.ctx
        port = line.port
        assert httpx.get(f"http://127.0.0.1:{port}/twilio/health").json() == {"ok": True}
        # the phone port serves the webhooks only: never the HUD or its API
        for path in ("/api/state", "/", "/docs", "/static/app.js"):
            assert httpx.get(f"http://127.0.0.1:{port}{path}").status_code == 404
        # an unsigned webhook is refused
        assert httpx.post(f"http://127.0.0.1:{port}/twilio/sms", data={"From": OWNER, "Body": "hi"}).status_code == 403

        # changing a phone setting restarts the line with it
        assert client.post("/api/setup", json={"values": {"PHONE_LANGUAGE": "en-US"}}).status_code == 200
        wait_for(lambda: len(twilio.updates()) == 2 and line.status()["status"] == "online")
        assert line.cfg.language == "en-US"
        new_port = line.port

        # a configuration problem shows up in the status, and the app carries on
        client.post("/api/setup", json={"values": {"PHONE_PIN": "12"}})
        st = wait_for(lambda: (p := client.get("/api/state").json()["phone"])["status"] == "error" and p)
        assert "PHONE_PIN" in st["error"] and st["public_url"] is None
        assert not _port_open(new_port)
        client.post("/api/setup", json={"values": {"PHONE_PIN": "482916"}})
        wait_for(lambda: line.status()["status"] == "online")
        new_port = line.port

        # switching it off stops it
        client.post("/api/setup", json={"values": {"PHONE_ENABLED": "false"}})
        st = wait_for(lambda: (p := client.get("/api/state").json()["phone"])["status"] == "off" and p)
        assert st["enabled"] is False and st["public_url"] is None and st["error"] is None
        wait_for(lambda: server.S.phone_task.done())  # the state says "off" as soon as the stop is queued
        assert line.status()["status"] == "off" and not _port_open(new_port)

        client.post("/api/setup", json={"values": {"PHONE_ENABLED": "true"}})
        wait_for(lambda: line.status()["status"] == "online")
        last_port = line.port
        assert _port_open(last_port)
    assert line.status()["status"] == "off"
    assert not _port_open(last_port)
    assert all(r.url.host == "api.twilio.com" for r in twilio.requests)


def test_the_real_phone_line_reports_missing_twilio_keys_without_network(app_client, monkeypatch):
    server, open_client = app_client
    line = phone.PhoneLine()
    monkeypatch.setattr(phone, "LINE", line)
    monkeypatch.setenv("PHONE_ENABLED", "true")
    monkeypatch.setenv("PHONE_NUMBER", LINE_NUMBER)
    monkeypatch.setenv("USER_PHONE", OWNER)
    with open_client() as client:
        st = wait_for(lambda: (p := client.get("/api/state").json()["phone"])["status"] == "error" and p)
        assert "TWILIO_ACCOUNT_SID" in st["error"] and st["enabled"] is True
        assert client.get("/api/state").status_code == 200
    assert line.status()["status"] == "off"


def test_a_restart_left_on_another_event_loop_is_ignored(app_client, monkeypatch):
    # Without the lifespan (a bare TestClient), each request runs on its own short-lived loop: a restart task left on
    # an earlier loop can never finish, so neither the next restart nor /api/state may wait on it.
    server, open_client = app_client
    fake = FakePhone(monkeypatch, delay=60)
    monkeypatch.setattr(server.S, "settings", config.Settings.load())
    monkeypatch.setattr(server.S, "phone_task", None)
    bare = TestClient(server.app, base_url="http://127.0.0.1:8765")
    bare.headers["X-Atlas-Token"] = server.S.token
    began = time.monotonic()
    for _ in range(2):
        assert bare.post("/api/setup", json={"values": {"PHONE_ENABLED": "true"}}).status_code == 200
    assert bare.get("/api/state").json()["phone"]["status"] == "off"  # the fake's own status: nothing queued here
    assert time.monotonic() - began < 5
    monkeypatch.setattr(server.S, "phone_task", None)
