import json
import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import load_env_file
from app.llm import LLMError, OpenRouter
from app.tools import TOOLS, memory
from conftest import REAL_ENV_PATH, file_stamp, no_network, run
from test_agent import FakeLLM, text, tool_calls

STATE_KEYS = {
    "assistant_name", "user_name", "address_as", "has_key", "key_hint", "model", "search_model", "home_city",
    "units", "news_region", "news_language", "watchlist", "email_address", "email_configured", "smtp_host",
    "smtp_port", "imap_host", "imap_port", "os", "capabilities", "news_categories", "tools",
    "user_phone", "home_address", "work_address", "extra", "secrets_configured",
}


@pytest.fixture
def srv(isolated, monkeypatch):
    from app import server

    real_async_client = httpx.AsyncClient

    def offline_client(*args, **kwargs):  # the app's own clients can never reach a real host
        if kwargs.get("transport") is None:  # also when a caller passes transport=None explicitly
            kwargs["transport"] = httpx.MockTransport(no_network)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", offline_client)
    monkeypatch.setattr(server.S, "settings", config.Settings.load())
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-Atlas-Token"] = server.S.token
        yield server, client


def sse_events(body: str) -> list[dict]:
    assert body.endswith("\n\n")
    return [json.loads(chunk[len("data: "):]) for chunk in body.split("\n\n") if chunk.strip()]


# ------------------------------------------------------------------ guards
@pytest.mark.parametrize("host", ["evil.com", "evil.com:8765", "127.0.0.1.evil.com", "localhost.attacker.net:8765", "", "0.0.0.0:8765"])
def test_host_guard_blocks_rebinding(srv, host):
    _, client = srv
    for path in ("/api/state", "/"):
        r = client.get(path, headers={"Host": host})
        assert r.status_code == 403 and r.json() == {"error": "forbidden host"}


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "LOCALHOST", "[::1]:8765", "[::1]"])
def test_host_guard_allows_loopback(srv, host):
    _, client = srv
    assert client.get("/api/state", headers={"Host": host}).status_code == 200


def test_token_guard(srv):
    server, client = srv
    assert client.get("/api/state", headers={"X-Atlas-Token": ""}).status_code == 401
    r = client.get("/api/state", headers={"X-Atlas-Token": "wrong"})
    assert r.status_code == 401 and r.json() == {"error": "bad token"}
    bare = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert bare.get("/api/state").status_code == 401
    assert bare.post("/api/setup", json={"values": {"USER_NAME": "x"}}).status_code == 401
    assert not config.ENV_PATH.exists()
    ok = client.get("/api/state")
    assert ok.status_code == 200
    assert ok.headers["x-frame-options"] == "DENY" and ok.headers["referrer-policy"] == "no-referrer"


def test_index_embeds_token_without_api_token(srv):
    server, _ = srv
    bare = TestClient(server.app, base_url="http://127.0.0.1:8765")
    r = bare.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "{{ATLAS_TOKEN}}" not in r.text
    if "{{ATLAS_TOKEN}}" in (config.WEB_DIR / "index.html").read_text(encoding="utf-8"):
        assert server.S.token in r.text


# ------------------------------------------------------------------ state / setup
def test_state_shape(srv):
    server, client = srv
    s = client.get("/api/state").json()
    assert set(s) == STATE_KEYS
    assert s["has_key"] is False and s["key_hint"] == "" and s["assistant_name"] == "ATLAS"
    assert s["smtp_port"] == "" and s["imap_port"] == "" and s["email_configured"] is False
    assert {"platform", "system_stats", "screen", "keyboard"} <= set(s["capabilities"])
    assert s["news_categories"][:2] == ["top", "world"]
    assert s["tools"] == sorted(TOOLS) and len(s["tools"]) >= 41
    assert s["watchlist"] == config.DEFAULT_WATCHLIST


def test_state_never_exposes_secret_values(srv):
    server, client = srv
    config.ENV_PATH.write_text(
        "TWILIO_AUTH_TOKEN=tok-secret-123\nHOME_ASSISTANT_TOKEN=ha-secret-456\n"
        "CALENDAR_ICS_URLS=https://calendar.google.com/calendar/ical/x/private-abc/basic.ics\n"
        "TWILIO_FROM_NUMBER=+15550001111\nBROWSER_HEADLESS=true\n"
    )
    r = client.get("/api/state")
    body = r.text
    for secret in ("tok-secret-123", "ha-secret-456", "private-abc"):
        assert secret not in body
    s = r.json()
    assert s["secrets_configured"]["TWILIO_AUTH_TOKEN"] is True
    assert s["secrets_configured"]["HOME_ASSISTANT_TOKEN"] is True
    assert s["secrets_configured"]["CALENDAR_ICS_URLS"] is True
    assert s["secrets_configured"]["OPENROUTER_API_KEY"] is False
    assert s["extra"]["TWILIO_FROM_NUMBER"] == "+15550001111" and s["extra"]["BROWSER_HEADLESS"] == "true"
    assert not set(s["extra"]) & config.SECRET_KEYS


def test_setup_blank_secret_keeps_new_secret_keys(srv):
    server, client = srv
    config.ENV_PATH.write_text("HOME_ASSISTANT_TOKEN=keep-me\n")
    r = client.post("/api/setup", json={"values": {"HOME_ASSISTANT_TOKEN": "", "HOME_ASSISTANT_URL": "http://ha.local:8123"}})
    assert r.status_code == 200
    values = load_env_file(config.ENV_PATH)
    assert values["HOME_ASSISTANT_TOKEN"] == "keep-me" and values["HOME_ASSISTANT_URL"] == "http://ha.local:8123"


def test_state_key_hint(srv, monkeypatch):
    server, client = srv
    monkeypatch.setattr(server.S.settings, "api_key", "sk-or-v1-0123456789abcdef")
    s = client.get("/api/state").json()
    assert s["has_key"] is True and s["key_hint"] == "sk-or-v1…cdef"
    assert "0123456789" not in json.dumps(s)


def test_setup_writes_only_to_temp_env(srv):
    server, client = srv
    real_before = file_stamp(REAL_ENV_PATH)
    r = client.post("/api/setup", json={"values": {"USER_NAME": "Kolton", "HOME_CITY": "Austin, Texas", "ASSISTANT_NAME": "Atlas", "EMAIL_PASSWORD": "abcd efgh ijkl mnop"}})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "updated": ["ASSISTANT_NAME", "EMAIL_PASSWORD", "HOME_CITY", "USER_NAME"]}
    assert file_stamp(REAL_ENV_PATH) == real_before
    values = load_env_file(config.ENV_PATH)
    assert values == {"USER_NAME": "Kolton", "HOME_CITY": "Austin, Texas", "ASSISTANT_NAME": "Atlas", "EMAIL_PASSWORD": "abcd efgh ijkl mnop"}
    state = client.get("/api/state").json()
    assert state["user_name"] == "Kolton" and state["assistant_name"] == "Atlas" and state["home_city"] == "Austin, Texas"
    assert server.S.settings.email_password == "abcd efgh ijkl mnop"
    assert "abcd efgh" not in json.dumps(state)  # the password never comes back out


def test_setup_rejects_unknown_keys_and_writes_nothing(srv):
    _, client = srv
    for key in ("AUTO_APPROVE", "ALLOW_SHELL", "OPENROUTER_BASE_URL", "ATLAS_HOST", "PATH"):
        r = client.post("/api/setup", json={"values": {"USER_NAME": "x", key: "y"}})
        assert r.status_code == 400 and key in r.json()["detail"]
    assert not config.ENV_PATH.exists()


def test_setup_blank_secret_keeps_old_value(srv):
    server, client = srv
    config.ENV_PATH.write_text('OPENROUTER_API_KEY=sk-or-old-key-1234567890\nEMAIL_PASSWORD="old pass word"\nHOME_CITY=Austin\n')
    r = client.post("/api/setup", json={"values": {"OPENROUTER_API_KEY": "", "EMAIL_PASSWORD": "   ", "HOME_CITY": "", "UNITS": "metric"}})
    assert r.json() == {"ok": True, "updated": ["HOME_CITY", "UNITS"]}
    values = load_env_file(config.ENV_PATH)
    assert values["OPENROUTER_API_KEY"] == "sk-or-old-key-1234567890"
    assert values["EMAIL_PASSWORD"] == "old pass word"
    assert values["HOME_CITY"] == "" and values["UNITS"] == "metric"
    assert server.S.settings.api_key == "sk-or-old-key-1234567890" and server.S.settings.units == "metric"


def test_setup_line_break_cannot_inject_settings(srv):
    server, client = srv
    r = client.post("/api/setup", json={"values": {"HOME_CITY": "Austin\rAUTO_APPROVE=run_command,send_email\nALLOW_SHELL=true"}})
    assert r.status_code == 200
    values = load_env_file(config.ENV_PATH)
    assert set(values) == {"HOME_CITY"}
    assert server.S.settings.needs_confirmation("run_command") and server.S.settings.needs_confirmation("send_email")


def test_setup_validation(srv):
    _, client = srv
    assert client.post("/api/setup", json={"values": {"SMTP_PORT": 587}}).status_code == 422
    assert client.post("/api/setup", json={}).status_code == 422
    r = client.post("/api/setup", json={"values": {}})
    assert r.json() == {"ok": True, "updated": []} and not config.ENV_PATH.exists()


def test_setup_keeps_port(srv):
    server, client = srv
    server.S.settings.port = 8777
    client.post("/api/setup", json={"values": {"USER_NAME": "x"}})
    assert server.S.settings.port == 8777


# ------------------------------------------------------------------ chat / confirm
def test_confirm_unknown_id(srv):
    _, client = srv
    r = client.post("/api/confirm/doesnotexist", json={"approved": True})
    assert r.status_code == 404 and "No pending action" in r.json()["detail"]
    assert client.post("/api/confirm/x", json={}).status_code == 422


def test_chat_streams_sse(srv, monkeypatch):
    server, client = srv
    monkeypatch.setattr(server.S, "llm", FakeLLM([text("Hello, "), text("Kolton.")]))
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["model", "token", "token", "done"]
    assert events[-1]["messages"] == [{"role": "assistant", "content": "Hello, Kolton."}]


def test_chat_tool_round_trip(srv, monkeypatch):
    server, client = srv
    llm = FakeLLM([tool_calls(("c1", "calculate", '{"expression": "6*7"}'))], [text("It's 42.")])
    monkeypatch.setattr(server.S, "llm", llm)
    events = sse_events(client.post("/api/chat", json={"messages": [{"role": "user", "content": "6 times 7?"}]}).text)
    assert [e["type"] for e in events] == ["model", "tool", "tool", "token", "done"]
    assert events[1] == {"type": "tool", "state": "start", "id": "c1", "name": "calculate", "label": "Computing", "args": {"expression": "6*7"}}
    assert events[2] == {"type": "tool", "state": "done", "id": "c1", "name": "calculate", "summary": "42"}
    tool_msg = llm.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"]) == {"expression": "6*7", "result": 42}


def test_chat_approval_over_http(srv, monkeypatch):
    server, client = srv
    ran = []

    async def fake_write(ctx, path, content, mode="overwrite"):
        ran.append((path, content))
        return {"written": path, "bytes": len(content)}

    monkeypatch.setattr(TOOLS["write_file"], "func", fake_write)
    llm = FakeLLM([tool_calls(("w1", "write_file", '{"path": "/tmp/notes.txt", "content": "hi"}'))], [text("Saved.")])
    monkeypatch.setattr(server.S, "llm", llm)
    results = {}

    def approver():
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = list(server.S.broker.pending)
            if pending:
                results["confirm"] = client.post(f"/api/confirm/{pending[0]}", json={"approved": True})
                return
            time.sleep(0.01)

    t = threading.Thread(target=approver)
    t.start()
    events = sse_events(client.post("/api/chat", json={"messages": [{"role": "user", "content": "save it"}]}).text)
    t.join()
    assert results["confirm"].json() == {"ok": True}
    confirm = next(e for e in events if e["type"] == "confirm")
    assert confirm["title"] == "Write file" and confirm["fields"][1] == ["Mode", "overwrite"] and confirm["body"] == "hi"
    assert next(e for e in events if e["type"] == "confirm_result")["approved"] is True
    assert ran == [("/tmp/notes.txt", "hi")]
    assert server.S.broker.pending == {}


def test_chat_internal_error_event(srv, monkeypatch):
    server, client = srv

    class Broken(FakeLLM):
        async def resolve_model(self):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(server.S, "llm", Broken())
    events = sse_events(client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).text)
    assert events == [{"type": "error", "text": "Internal error: kaboom"}]


def test_chat_with_malformed_history_still_answers(srv, monkeypatch):
    server, client = srv
    monkeypatch.setattr(server.S, "llm", FakeLLM([text("ok")]))
    history = [{"role": "assistant", "tool_calls": [{"id": 1, "function": None}]}, {"role": "tool"}, {"role": "user", "content": "hi"}]
    events = sse_events(client.post("/api/chat", json={"messages": history}).text)
    assert events[-1]["type"] == "done"


def test_chat_end_to_end_with_real_openrouter_client(srv, monkeypatch):
    """Server -> agent -> OpenRouter client -> mocked OpenRouter HTTP API, including a tool round."""
    server, client = srv
    chat_payloads = []

    def sse(*chunks):
        return httpx.Response(200, content=("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"})

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "anthropic/claude-sonnet-4.5", "supported_parameters": ["tools"]}]})
        payload = json.loads(request.content)
        chat_payloads.append(payload)
        if payload["messages"][-1]["role"] == "user":
            return sse(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "calculate", "arguments": '{"expression": '}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"2^10"}'}}]}, "finish_reason": "tool_calls"}]},
            )
        return sse({"choices": [{"delta": {"content": "That's 1024."}}]}, {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.001}})

    s = config.Settings(api_key="sk-or-test", base_url="http://openrouter.test/api/v1")
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server.S, "settings", s)
    monkeypatch.setattr(server.S, "llm", OpenRouter(s, mock))
    events = sse_events(client.post("/api/chat", json={"messages": [{"role": "user", "content": "2^10?"}]}).text)
    assert [e["type"] for e in events] == ["model", "tool", "tool", "token", "usage", "done"]
    assert events[0]["model"] == "anthropic/claude-sonnet-4.5"
    assert events[2]["summary"] == "1024"
    assert events[4] == {"type": "usage", "prompt_tokens": 50, "completion_tokens": 5, "cost": 0.001}
    assert events[-1]["messages"][-1] == {"role": "assistant", "content": "That's 1024."}
    second = chat_payloads[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"] == {"name": "calculate", "arguments": '{"expression": "2^10"}'}
    assert json.loads(second[-1]["content"])["result"] == 1024


# ------------------------------------------------------------------ panels
def test_reminders_endpoints(srv):
    _, client = srv
    now = datetime.now().astimezone()
    memory.save(
        "reminders",
        [
            {"id": "due1", "message": "Stand up", "due": (now - timedelta(seconds=5)).isoformat(timespec="seconds"), "fired": False},
            {"id": "later", "message": "Call mom", "due": (now + timedelta(hours=2)).isoformat(timespec="seconds"), "fired": False},
        ],
    )
    assert [r["id"] for r in client.get("/api/reminders").json()["reminders"]] == ["due1", "later"]
    due = client.get("/api/reminders/due").json()
    assert [r["message"] for r in due["due"]] == ["Stand up"]
    assert client.get("/api/reminders/due").json() == {"due": []}
    assert [r["id"] for r in client.get("/api/reminders").json()["reminders"]] == ["later"]


def test_memory_endpoints(srv):
    _, client = srv
    memory.save("memory", [{"id": "m1", "fact": "Likes tea", "saved": "2026-11-04T10:00"}])
    assert client.get("/api/memory").json() == {"memories": [{"id": "m1", "fact": "Likes tea", "saved": "2026-11-04T10:00"}]}
    assert client.delete("/api/memory/m1").json() == {"ok": True}
    assert client.delete("/api/memory/m1").json() == {"ok": False}
    assert client.get("/api/memory").json() == {"memories": []}


def test_models_endpoint(srv, monkeypatch):
    server, client = srv

    class Models:
        def __init__(self, exc=None):
            self.exc = exc

        async def tool_models(self):
            if self.exc:
                raise self.exc
            return [{"id": "a/b", "name": "B", "context": 1, "prompt_price": 1.0, "completion_price": 2.0, "vision": False}]

        async def resolve_model(self):
            return "a/b"

    monkeypatch.setattr(server.S, "llm", Models())
    assert client.get("/api/models").json() == {"models": [{"id": "a/b", "name": "B", "context": 1, "prompt_price": 1.0, "completion_price": 2.0, "vision": False}], "current": "a/b"}
    monkeypatch.setattr(server.S, "llm", Models(LLMError("OpenRouter error 500: x")))
    assert client.get("/api/models").json() == {"models": [], "current": "", "error": "OpenRouter error 500: x"}
    monkeypatch.setattr(server.S, "llm", Models(httpx.ConnectError("no route")))
    assert client.get("/api/models").json()["error"] == "Can't reach OpenRouter: no route"


def test_panels_degrade_gracefully_offline(srv):
    _, client = srv
    h = client.get("/api/headlines?category=world").json()
    assert h["category"] == "world" and h["articles"] == [] and "unreachable" in h["error"]
    assert client.get("/api/markets").json() == {"quotes": []}
    w = client.get("/api/weather").json()
    assert set(w) == {"error"}
    sysinfo = client.get("/api/system").json()
    assert {"os", "cpu_percent", "memory", "disk", "battery", "uptime_hours"} <= set(sysinfo) or "error" in sysinfo


# ------------------------------------------------------------------ live data panels
MOVERS_KEYS = {"status", "market_state", "gainers", "losers", "most_active", "trending", "errors", "as_of"}


def test_market_pulse_offline_reports_each_part_separately(srv):
    _, client = srv
    r = client.get("/api/market_pulse")
    assert r.status_code == 200 and len(r.content) < 50_000
    body = r.json()
    assert set(body) == {"movers", "crypto", "sentiment"}
    movers, crypto, sentiment = body["movers"], body["crypto"], body["sentiment"]
    assert set(movers) == MOVERS_KEYS and movers["status"] == "unavailable"
    assert movers["gainers"] == movers["losers"] == movers["most_active"] == movers["trending"] == []
    assert movers["errors"] and all(isinstance(e, str) for e in movers["errors"])
    assert movers["market_state"] in {"open", "pre-market", "after-hours", "closed"}
    assert crypto["coins"] == [] and crypto["global"] is None and crypto["error"]
    assert sentiment["fear_greed"] is None and sentiment["vix"] is None and sentiment["error"]
    # friendly messages only: no raw exception text (the offline transport's message holds the URL) or HTML
    for leak in ("unexpected network request", "https://", "Traceback", "<html"):
        assert leak not in r.text


def test_market_pulse_parts_fail_independently(srv, monkeypatch):
    import asyncio

    from app.tools import data_sources, finance_data

    server, client = srv
    movers = {
        "status": "ok", "market_state": "open", "gainers": [{"symbol": "NVDA", "name": "NVIDIA", "price": 140.1, "change_pct": 4.2}],
        "losers": [], "most_active": [], "trending": [{"symbol": "TSLA"}], "errors": [], "as_of": "2026-11-04T15:30+00:00",
    }

    async def fake_movers(ctx):
        return movers

    async def broken_crypto(ctx):
        raise RuntimeError("boom https://api.coingecko.com/x?api_key=cg-secret-123")

    async def slow_sentiment(ctx):
        await asyncio.sleep(5)

    monkeypatch.setattr(server, "PANEL_PART_TIMEOUT", 0.2)
    monkeypatch.setattr(finance_data, "market_pulse", fake_movers)
    monkeypatch.setattr(data_sources, "crypto_pulse", broken_crypto)
    monkeypatch.setattr(data_sources, "sentiment_pulse", slow_sentiment)
    r = client.get("/api/market_pulse")
    assert r.status_code == 200 and "cg-secret-123" not in r.text and "coingecko.com" not in r.text
    body = r.json()
    assert body["movers"] == movers
    assert body["crypto"] == {"coins": [], "global": None, "source": "CoinGecko", "error": "Crypto data is unavailable right now (RuntimeError)."}
    assert body["sentiment"] == {"fear_greed": None, "vix": None, "error": "Sentiment data didn't arrive in time."}


def test_market_pulse_keeps_the_movers_shape_when_movers_fail(srv, monkeypatch):
    from app.tools import data_sources, finance_data

    server, client = srv
    crypto = {"coins": [{"symbol": "BTC", "name": "Bitcoin", "price": 97000.0, "change_24h_pct": 1.2, "change_7d_pct": -3.0}],
              "global": None, "source": "CoinGecko", "as_of": "2026-11-04T15:30+00:00"}

    async def broken_movers(ctx):
        raise ValueError("bad")

    async def odd_sentiment(ctx):
        return ["not", "a", "dict"]

    async def fake_crypto(ctx):
        return crypto

    monkeypatch.setattr(finance_data, "market_pulse", broken_movers)
    monkeypatch.setattr(data_sources, "crypto_pulse", fake_crypto)
    monkeypatch.setattr(data_sources, "sentiment_pulse", odd_sentiment)
    body = client.get("/api/market_pulse").json()
    assert set(body["movers"]) == MOVERS_KEYS and body["movers"]["status"] == "unavailable"
    assert body["movers"]["errors"] == ["Market movers data is unavailable right now (ValueError)."]
    assert body["crypto"] == crypto
    assert body["sentiment"] == {"fear_greed": None, "vix": None, "error": "Sentiment data is unavailable right now."}


def test_sports_panel(srv, monkeypatch):
    server, client = srv
    assert client.get("/api/sports").json() == {"leagues": []}  # offline: every league fails, so none are shown
    leagues = [{"league": "NBA", "key": "nba", "status": "NBA today: 1 game", "games": [{"matchup": "BOS @ NY", "status": "live", "text": "Celtics 78, Knicks 74"}]}]

    async def fake(ctx):
        return leagues

    monkeypatch.setattr(server, "scores_for_panel", fake)
    assert client.get("/api/sports").json() == {"leagues": leagues}

    async def broken(ctx):
        raise RuntimeError("https://site.api.espn.com/secret")

    monkeypatch.setattr(server, "scores_for_panel", broken)
    out = client.get("/api/sports").json()
    assert out == {"leagues": [], "error": "Scores are unavailable right now (RuntimeError)."}


def test_live_data_settings_in_state_and_setup(srv):
    server, client = srv
    config.ENV_PATH.write_text(
        "FINANCE_REGION=GB\nSEC_CONTACT_EMAIL=me@example.com\nSPORTS_TIMEZONE=America/Chicago\n"
        "SPORTS_PANEL_LEAGUES=nba,epl\nCOINGECKO_API_KEY=cg-secret-123\nFRED_API_KEY=fred-secret-456\n"
    )
    r = client.get("/api/state")
    assert "cg-secret-123" not in r.text and "fred-secret-456" not in r.text
    s = r.json()
    assert s["extra"]["FINANCE_REGION"] == "GB" and s["extra"]["SEC_CONTACT_EMAIL"] == "me@example.com"
    assert s["extra"]["SPORTS_TIMEZONE"] == "America/Chicago" and s["extra"]["SPORTS_PANEL_LEAGUES"] == "nba,epl"
    assert s["extra"]["FINANCE_USER_AGENT"] == ""
    assert s["secrets_configured"]["COINGECKO_API_KEY"] is True and s["secrets_configured"]["FRED_API_KEY"] is True
    assert {"get_stock_details", "get_crypto", "get_economic_data", "get_sports_scores"} <= set(s["tools"])
    # the settings screen can write every new key; blank secrets keep the stored value
    r = client.post("/api/setup", json={"values": {
        "FINANCE_REGION": "CA", "FINANCE_USER_AGENT": "", "SEC_CONTACT_EMAIL": "you@example.com",
        "SPORTS_TIMEZONE": "Europe/London", "SPORTS_PANEL_LEAGUES": "nfl,f1", "COINGECKO_API_KEY": "", "FRED_API_KEY": "fred-new",
    }})
    assert r.status_code == 200
    values = load_env_file(config.ENV_PATH)
    assert values["FINANCE_REGION"] == "CA" and values["SEC_CONTACT_EMAIL"] == "you@example.com"
    assert values["SPORTS_PANEL_LEAGUES"] == "nfl,f1" and values["SPORTS_TIMEZONE"] == "Europe/London"
    assert values["COINGECKO_API_KEY"] == "cg-secret-123" and values["FRED_API_KEY"] == "fred-new"


def test_lifespan_closes_the_yahoo_session(isolated, monkeypatch):
    from app import server
    from app.tools import browser, finance_data

    calls = []

    async def browser_down():
        raise RuntimeError("browser shutdown failed")  # must not stop the rest of the teardown

    async def yahoo_down():
        calls.append("yahoo")

    monkeypatch.setattr(browser, "shutdown", browser_down)
    monkeypatch.setattr(finance_data, "shutdown", yahoo_down)
    monkeypatch.setattr(server.S, "settings", config.Settings.load())
    with TestClient(server.app, base_url="http://127.0.0.1:8765"):
        assert calls == []
    assert calls == ["yahoo"] and server.S.http.is_closed


# ------------------------------------------------------------------ startup banner
@pytest.mark.parametrize(
    "name, url",
    [
        ("ATLAS", "http://127.0.0.1:8765/"),
        ("ATLAS", "http://127.0.0.1:65535/"),
        ("Jarvis-Assistant-Prime", "http://127.0.0.1:8770/"),
        ("アトラス", "http://127.0.0.1:8765/"),  # double-width characters
        ("", "http://127.0.0.1:8765/"),
    ],
)
def test_banner_right_border_lines_up(name, url):
    from app.server import _cell_width, banner

    lines = [line for line in banner(name, url).splitlines() if line]
    assert len(lines) == 4
    widths = {_cell_width(line) for line in lines}
    assert len(widths) == 1, lines  # every row, borders included, is the same width
    assert lines[0].strip()[0] == "┌" and lines[-1].strip()[-1] == "┘"
    assert all(line.rstrip().endswith("│") for line in lines[1:-1])
    assert url in lines[1] and "Press Ctrl+C to shut down." in lines[2]


def test_print_banner_falls_back_to_ascii(monkeypatch):
    from app import server

    out = []

    def strict_print(text):
        text.encode("ascii")  # a legacy console encoding raises UnicodeEncodeError for box characters
        out.append(text)

    monkeypatch.setattr("builtins.print", strict_print)
    server._print_banner(server.banner("ATLAS", "http://127.0.0.1:8765/"))
    assert out and "+------" in out[0] and "| ATLAS" not in out[0] and "|  ATLAS      online  >  http://127.0.0.1:8765/  |" in out[0]


# ------------------------------------------------------------------ SSE stream cleanup
def test_chat_stream_closes_the_turn_when_the_client_goes_away(monkeypatch):
    from app import server

    state = {"closed": False, "resumed": False}

    async def fake_turn(history, settings, llm, broker, ctx):
        try:
            yield {"type": "confirm", "id": "abc"}
            state["resumed"] = True
            yield {"type": "done", "messages": []}
        finally:
            state["closed"] = True  # e.g. run_turn withdrawing its pending approval card

    class GoneRequest:
        async def is_disconnected(self):
            return True

    monkeypatch.setattr(server, "run_turn", fake_turn)
    monkeypatch.setattr(server.S, "http", None, raising=False)
    monkeypatch.setattr(server.S, "llm", None, raising=False)

    async def go():
        resp = await server.chat(server.ChatBody(messages=[]), GoneRequest())
        chunks = [chunk async for chunk in resp.body_iterator]
        return chunks, dict(state)  # checked at once, not after asyncio.run() finalizes stray generators

    chunks, seen = run(go())
    assert chunks == ['data: {"type": "confirm", "id": "abc"}\n\n']
    assert seen == {"closed": True, "resumed": False}
