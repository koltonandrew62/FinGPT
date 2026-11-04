import json

import httpx
import pytest

from app.config import MODEL_PREFERENCES, Settings
from app.llm import LLMError, OpenRouter, _friendly_error
from conftest import json_response, run

BASE = "http://openrouter.test/api/v1"


def sse(*items) -> bytes:
    out = []
    for it in items:
        if isinstance(it, str):
            out.append(it)  # raw line, e.g. a comment or [DONE]
        else:
            out.append("data: " + json.dumps(it))
    return ("\n\n".join(out) + "\n\n").encode()


def delta(**d):
    return {"id": "gen-1", "model": "anthropic/claude-sonnet-4.5", "choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def finish(reason):
    return {"id": "gen-1", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


class Recorder:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.response(request) if callable(self.response) else self.response


def stream(body, status=200, key="sk-or-test", tools=None):
    rec = Recorder(httpx.Response(status, content=body, headers={"content-type": "text/event-stream"}))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as http:
            llm = OpenRouter(Settings(api_key=key, base_url=BASE), http)
            return [ev async for ev in llm.stream_chat([{"role": "user", "content": "hi"}], model="m/x", tools=tools)]

    return run(go()), rec


def test_stream_text_comments_usage_and_done():
    body = sse(
        ": OPENROUTER PROCESSING",
        ": OPENROUTER PROCESSING",
        delta(role="assistant", content=""),
        delta(content="Good "),
        delta(content="evening."),
        "data: {not json",
        "data: 42",
        "event: ping",
        finish("stop"),
        {"id": "gen-1", "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.00002}},
        "data: [DONE]",
        delta(content="IGNORED after DONE"),
    )
    events, rec = stream(body, tools=[{"type": "function", "function": {"name": "t"}}])
    assert events == [
        {"type": "text", "text": "Good "},
        {"type": "text", "text": "evening."},
        {"type": "usage", "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.00002}, "model": None},
        {"type": "finish", "reason": "stop"},
    ]
    req = rec.requests[0]
    assert str(req.url) == f"{BASE}/chat/completions" and req.method == "POST"
    assert req.headers["authorization"] == "Bearer sk-or-test"
    assert req.headers["x-title"] == "ATLAS Personal Assistant"
    payload = json.loads(req.content)
    assert payload["model"] == "m/x" and payload["stream"] is True and payload["usage"] == {"include": True}
    assert payload["tools"][0]["function"]["name"] == "t"


def test_stream_omits_tools_when_none():
    _, rec = stream(sse(delta(content="x"), "data: [DONE]"))
    assert "tools" not in json.loads(rec.requests[0].content)


def test_stream_tool_call_deltas_assembled_by_index():
    body = sse(
        delta(content="Let me check."),
        delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]),
        delta(tool_calls=[{"index": 1, "id": "call_b", "type": "function", "function": {"name": "get_news", "arguments": '{"cat'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"locat'}}]),
        delta(tool_calls=[{"index": 1, "function": {"arguments": 'egory": "top"}'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ion": "Austin"}'}}]),
        finish("tool_calls"),
        "data: [DONE]",
    )
    events, _ = stream(body)
    assert events[0] == {"type": "text", "text": "Let me check."}
    assert events[1] == {
        "type": "tool_calls",
        "calls": [
            {"id": "call_a", "name": "get_weather", "arguments": '{"location": "Austin"}'},
            {"id": "call_b", "name": "get_news", "arguments": '{"category": "top"}'},
        ],
    }
    assert events[2] == {"type": "finish", "reason": "tool_calls"}


def test_stream_tool_calls_without_index_or_id():
    body = sse(
        # provider that omits "index": a new id starts a new call
        delta(tool_calls=[{"id": "x1", "function": {"name": "get_time", "arguments": '{"timezone": '}}]),
        delta(tool_calls=[{"function": {"arguments": '"Asia/Tokyo"}'}}]),
        delta(tool_calls=[{"id": "x2", "function": {"name": "calculate", "arguments": '{"expression": "2+2"}'}}]),
        # provider that repeats the name in every delta
        delta(tool_calls=[{"index": 5, "function": {"name": "get_news", "arguments": "{}"}}]),
        delta(tool_calls=[{"index": 5, "function": {"name": "get_news", "arguments": ""}}]),
        "data: [DONE]",
    )
    events, _ = stream(body)
    calls = events[0]["calls"]
    assert [(c["name"], c["arguments"]) for c in calls] == [
        ("get_time", '{"timezone": "Asia/Tokyo"}'),
        ("calculate", '{"expression": "2+2"}'),
        ("get_news", "{}"),
    ]
    assert calls[0]["id"] == "x1" and calls[1]["id"] == "x2"
    assert calls[2]["id"].startswith("call_")  # generated when the provider sends none


def test_stream_mid_stream_error():
    body = sse(delta(content="Part"), {"id": "gen-1", "error": {"code": 502, "message": "Provider returned error"}, "choices": [{"delta": {}, "finish_reason": "error"}]})

    async def go():
        rec = Recorder(httpx.Response(200, content=body))
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as http:
            llm = OpenRouter(Settings(api_key="k", base_url=BASE), http)
            got = []
            with pytest.raises(LLMError, match="Model error: Provider returned error"):
                async for ev in llm.stream_chat([], model="m"):
                    got.append(ev)
            return got

    assert run(go()) == [{"type": "text", "text": "Part"}]


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (401, '{"error": {"message": "No auth credentials found", "code": 401}}', "OpenRouter rejected the API key. Check it in Settings."),
        (402, '{"error": {"message": "Insufficient credits"}}', "The OpenRouter account is out of credits. Add credits at openrouter.ai/settings/credits."),
        (429, '{"error": {"message": "Rate limit exceeded"}}', "OpenRouter is rate-limiting requests right now. Try again in a moment."),
        (404, '{"error": {"message": "No endpoints found that support tool use."}}', "This model can't use tools on OpenRouter. Pick another model in Settings. (No endpoints found that support tool use.)"),
        (404, '{"error": {"message": "Model not found"}}', "OpenRouter error 404: Model not found"),
        (500, "upstream exploded", "OpenRouter error 500: upstream exploded"),
        (400, '{"error": "plain string error"}', "OpenRouter error 400: plain string error"),
        (503, "", "OpenRouter error 503: "),
    ],
)
def test_stream_non_200_friendly_errors(status, body, expected):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body))) as http:
            llm = OpenRouter(Settings(api_key="k", base_url=BASE), http)
            with pytest.raises(LLMError) as info:
                async for _ in llm.stream_chat([], model="m"):
                    pass
            return str(info.value)

    assert run(go()) == expected
    assert _friendly_error(status, body) == expected


def test_missing_key_fails_before_any_request():
    events_or_error = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no request expected"))) as http:
            llm = OpenRouter(Settings(api_key="", base_url=BASE), http)
            with pytest.raises(LLMError, match="No OpenRouter API key"):
                async for ev in llm.stream_chat([], model="m"):
                    events_or_error.append(ev)
            with pytest.raises(LLMError, match="No OpenRouter API key"):
                await llm.complete([], model="m")

    run(go())
    assert events_or_error == []


MODELS = {
    "data": [
        {"id": "openai/gpt-4o", "name": "GPT-4o", "context_length": 128000, "pricing": {"prompt": "0.0000025", "completion": "0.00001"}, "supported_parameters": ["tools"], "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "google/gemini-2.5-flash", "name": "Gemini Flash", "pricing": {"prompt": "0.0000003", "completion": "bad"}, "supported_parameters": ["tools", "temperature"]},
        {"id": "meta/no-tools", "name": "No tools", "supported_parameters": ["temperature"]},
        {"id": "aaa/first-alpha", "supported_parameters": ["tools"]},
    ]
}


def with_models(fn, settings=None, response=None):
    rec = Recorder(response or json_response(MODELS))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as http:
            return await fn(OpenRouter(settings or Settings(api_key="k", base_url=BASE), http))

    return run(go()), rec


def test_tool_models_filters_and_prices():
    models, rec = with_models(lambda llm: llm.tool_models())
    assert [m["id"] for m in models] == ["aaa/first-alpha", "google/gemini-2.5-flash", "openai/gpt-4o"]
    gpt = models[2]
    assert gpt == {"id": "openai/gpt-4o", "name": "GPT-4o", "context": 128000, "prompt_price": 2.5, "completion_price": 10.0, "vision": True}
    assert models[1]["completion_price"] is None and models[1]["vision"] is False
    assert models[0]["name"] == "aaa/first-alpha"
    assert str(rec.requests[0].url) == f"{BASE}/models"


def test_models_are_cached():
    async def twice(llm):
        await llm.tool_models()
        await llm.resolve_model()
        return await llm.list_models()

    _, rec = with_models(twice)
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    "preferred, expected",
    [
        ("google/gemini-2.5-flash", "google/gemini-2.5-flash"),  # preferred and offered
        ("", "openai/gpt-4o"),  # first MODEL_PREFERENCES entry that is offered
        ("meta/no-tools", "openai/gpt-4o"),  # preferred lacks tool support -> fallback
        ("vendor/typo", "openai/gpt-4o"),
    ],
)
def test_resolve_model(preferred, expected):
    model, _ = with_models(lambda llm: llm.resolve_model(), Settings(api_key="k", base_url=BASE, model=preferred))
    assert model == expected


def test_resolve_model_when_no_preference_is_offered():
    only = {"data": [{"id": "zzz/last", "supported_parameters": ["tools"]}, {"id": "mmm/mid", "supported_parameters": ["tools"]}]}
    model, _ = with_models(lambda llm: llm.resolve_model(), response=json_response(only))
    assert model == "mmm/mid"
    model, _ = with_models(lambda llm: llm.resolve_model(), Settings(api_key="k", base_url=BASE, model="mine/x"), response=json_response(only))
    assert model == "mine/x"


def test_resolve_model_when_models_endpoint_fails():
    model, _ = with_models(lambda llm: llm.resolve_model(), response=httpx.Response(500, text="down"))
    assert model == MODEL_PREFERENCES[0]
    model, _ = with_models(lambda llm: llm.resolve_model(), Settings(api_key="k", base_url=BASE, model="mine/x"), response=httpx.Response(500))
    assert model == "mine/x"


def test_resolve_search_model():
    model, _ = with_models(lambda llm: llm.resolve_search_model())
    assert model == "google/gemini-2.5-flash"
    only_main = {"data": [{"id": "openai/gpt-4o", "supported_parameters": ["tools"]}]}
    model, _ = with_models(lambda llm: llm.resolve_search_model(), response=json_response(only_main))
    assert model == "openai/gpt-4o"


def test_complete():
    def handler(r):
        payload = json.loads(r.content)
        if payload["model"] == "bad":
            return json_response({"error": {"message": "context too long"}})
        return json_response({"choices": [{"message": {"content": "ok"}}], "echo": payload})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            llm = OpenRouter(Settings(api_key="k", base_url=BASE), http)
            good = await llm.complete([{"role": "user", "content": "q"}], model="m", plugins=[{"id": "web"}], max_tokens=5)
            with pytest.raises(LLMError, match="context too long"):
                await llm.complete([], model="bad")
            return good

    good = run(go())
    assert good["echo"]["plugins"] == [{"id": "web"}] and good["echo"]["max_tokens"] == 5
