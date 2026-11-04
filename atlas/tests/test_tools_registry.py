import asyncio
import json

import httpx
import pytest

from app import config
from app.tools import TOOLS, Tool, ToolError, call_tool, schemas
from conftest import make_ctx, run


def test_registry_is_complete_and_schemas_valid():
    assert len(TOOLS) >= 41
    names = [s["function"]["name"] for s in schemas()]
    assert len(names) == len(set(names))
    for s in schemas():
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"]), fn["name"]
        json.dumps(s)  # serialisable for the API payload
    for name in config.SENSITIVE_TOOLS:
        assert name in TOOLS, f"sensitive tool {name} is not registered"


def test_sensitive_tool_previews_render():
    samples = {
        "send_email": {"to": ["a@example.com"], "subject": "Hi", "body": "Hello"},
        "run_command": {"command": "echo hi", "working_directory": "/tmp"},
        "close_application": {"name": "Spotify"},
        "power_action": {"action": "restart"},
        "write_file": {"path": "/tmp/x.txt", "content": "abc", "mode": "append"},
        "read_file": {"path": "/tmp/x.txt"},
        "type_text": {"text": "hello"},
        "press_keys": {"keys": "ctrl+s"},
    }
    for name, args in samples.items():
        t = TOOLS[name]
        assert t.preview is not None, name
        p = t.preview(args)
        assert isinstance(p["title"], str) and p["title"]
        assert isinstance(p["fields"], list)
        assert all(isinstance(f, list) and len(f) == 2 for f in p["fields"])
        assert isinstance(p["body"], str)
    assert TOOLS["run_command"].preview(samples["run_command"])["code"] is True
    assert TOOLS["write_file"].preview(samples["write_file"])["title"] == "Append to file"


@pytest.fixture
def temp_tool(monkeypatch):
    def add(name, func, properties=None, required=None, timeout=60):
        t = Tool(
            name=name,
            description="test tool",
            parameters={"type": "object", "properties": properties or {}, "required": required or []},
            func=func,
            label=f"Testing {name}",
            timeout=timeout,
        )
        monkeypatch.setitem(TOOLS, name, t)
        return t

    return add


def test_call_tool_unknown():
    out = json.loads(run(call_tool("nope", {}, make_ctx())))
    assert out == {"error": "Unknown tool 'nope'."}


def test_call_tool_filters_args_and_checks_required(temp_tool):
    seen = {}

    async def fn(ctx, a, b=2):
        seen.update(a=a, b=b)
        return {"sum": a + b}

    temp_tool("t_add", fn, {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a"])
    out = json.loads(run(call_tool("t_add", {"a": 1, "b": 5, "evil": "x"}, make_ctx())))
    assert out == {"sum": 6} and seen == {"a": 1, "b": 5}
    missing = json.loads(run(call_tool("t_add", {"b": 1}, make_ctx())))
    assert missing == {"error": "Missing required argument(s): a"}
    none_args = json.loads(run(call_tool("t_add", None, make_ctx())))
    assert "Missing required" in none_args["error"]


def test_call_tool_error_mapping(temp_tool):
    async def tool_error(ctx):
        raise ToolError("friendly message")

    async def net_error(ctx):
        raise httpx.ConnectError("boom")

    async def crash(ctx):
        raise KeyError("x")

    def sync_tool(ctx):
        return "plain string result"

    async def huge(ctx):
        return {"n": 10**5000}

    temp_tool("t_te", tool_error)
    temp_tool("t_ne", net_error)
    temp_tool("t_crash", crash)
    temp_tool("t_sync", sync_tool)
    temp_tool("t_huge", huge)
    ctx = make_ctx()
    assert json.loads(run(call_tool("t_te", {}, ctx))) == {"error": "friendly message"}
    assert "Network error while running t_ne: ConnectError" in json.loads(run(call_tool("t_ne", {}, ctx)))["error"]
    assert json.loads(run(call_tool("t_crash", {}, ctx)))["error"].startswith("t_crash failed: KeyError")
    assert run(call_tool("t_sync", {}, ctx)) == "plain string result"
    # A result that can't be serialised must come back as an error, not crash the agent turn.
    assert "can't be shown" in json.loads(run(call_tool("t_huge", {}, ctx)))["error"]


def test_call_tool_serialises_non_json_types(temp_tool):
    from datetime import datetime

    async def fn(ctx):
        return {"when": datetime(2026, 1, 2, 3, 4), "name": "Café"}

    temp_tool("t_dt", fn)
    raw = run(call_tool("t_dt", {}, make_ctx()))
    assert "Café" in raw  # ensure_ascii=False
    assert json.loads(raw)["when"] == "2026-01-02 03:04:00"


def test_live_data_modules_are_registered_by_load_all():
    import sys

    from app import tools

    tools.load_all()
    for mod in ("app.tools.finance_data", "app.tools.data_sources", "app.tools.sports"):
        assert mod in sys.modules
    for name in ("get_stock_details", "get_market_movers", "get_stock_news", "get_technicals", "get_yield_curve",
                 "get_crypto", "get_sec_filings", "get_company_financials", "get_earnings_calendar",
                 "get_market_sentiment", "get_economic_data", "get_sports_scores", "get_sports_standings",
                 "get_team_schedule", "get_sports_news"):
        assert name in TOOLS, name
        assert TOOLS[name].func.__module__.startswith("app.tools."), name
    # the older market tools are still there alongside the new ones
    assert {"get_market_data", "lookup_ticker"} <= set(TOOLS)
