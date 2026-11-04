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
from conftest import REAL_ENV_PATH, file_stamp, no_network
from test_agent import FakeLLM, text, tool_calls

STATE_KEYS = {
    "assistant_name", "user_name", "address_as", "has_key", "key_hint", "model", "search_model", "home_city",
    "units", "news_region", "news_language", "watchlist", "email_address", "email_configured", "smtp_host",
    "smtp_port", "imap_host", "imap_port", "os", "capabilities", "news_categories", "tools",
}


@pytest.fixture
def srv(isolated, monkeypatch):
    from app import server

    real_async_client = httpx.AsyncClient

    def offline_client(*args, **kwargs):  # the app's own client can never reach a real host
        kwargs.setdefault("transport", httpx.MockTransport(no_network))
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
