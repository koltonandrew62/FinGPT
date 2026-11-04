"""Minimal async OpenRouter client: model discovery, streaming chat with tools."""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from .config import MODEL_PREFERENCES, SEARCH_MODEL_PREFERENCES, Settings


class LLMError(Exception):
    pass


def _friendly_error(status: int, body: str) -> str:
    detail = body
    try:
        parsed = json.loads(body)
        err = parsed.get("error", parsed)
        detail = err.get("message", body) if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        pass
    detail = (detail or "").strip()[:400]
    if status == 401:
        return "OpenRouter rejected the API key. Check it in Settings."
    if status == 402:
        return "The OpenRouter account is out of credits. Add credits at openrouter.ai/settings/credits."
    if status == 429:
        return "OpenRouter is rate-limiting requests right now. Try again in a moment."
    if status == 404 and "tool" in detail.lower():
        return f"This model can't use tools on OpenRouter. Pick another model in Settings. ({detail})"
    return f"OpenRouter error {status}: {detail}"


class OpenRouter:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self._models: list[dict] = []
        self._models_at = 0.0

    def _headers(self) -> dict[str, str]:
        if not self.settings.api_key:
            raise LLMError("No OpenRouter API key configured. Open Settings and paste your key.")
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "HTTP-Referer": "http://localhost/atlas",
            "X-Title": "ATLAS Personal Assistant",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ models
    async def list_models(self, force: bool = False) -> list[dict]:
        if self._models and not force and time.time() - self._models_at < 3600:
            return self._models
        r = await self.http.get(f"{self.settings.base_url}/models", timeout=20)
        if r.status_code != 200:
            raise LLMError(_friendly_error(r.status_code, r.text))
        self._models = r.json().get("data", [])
        self._models_at = time.time()
        return self._models

    async def tool_models(self) -> list[dict]:
        out = []
        for m in await self.list_models():
            params = m.get("supported_parameters") or []
            if "tools" not in params:
                continue
            pricing = m.get("pricing") or {}
            out.append(
                {
                    "id": m.get("id"),
                    "name": m.get("name") or m.get("id"),
                    "context": m.get("context_length"),
                    "prompt_price": _per_million(pricing.get("prompt")),
                    "completion_price": _per_million(pricing.get("completion")),
                    "vision": "image" in ((m.get("architecture") or {}).get("input_modalities") or []),
                }
            )
        out.sort(key=lambda m: m["id"] or "")
        return out

    async def _pick(self, preferred: str, fallbacks: list[str]) -> str:
        try:
            available = {m["id"] for m in await self.tool_models()}
        except Exception:
            return preferred or fallbacks[0]
        if preferred and preferred in available:
            return preferred
        for candidate in fallbacks:
            if candidate in available:
                return candidate
        return preferred or (sorted(available)[0] if available else fallbacks[0])

    async def resolve_model(self) -> str:
        return await self._pick(self.settings.model, MODEL_PREFERENCES)

    async def resolve_search_model(self) -> str:
        main = await self.resolve_model()
        return await self._pick(self.settings.search_model, SEARCH_MODEL_PREFERENCES + [main])

    # -------------------------------------------------------------------- chat
    async def complete(self, messages: list[dict], model: str, **extra: Any) -> dict:
        payload = {"model": model, "messages": messages, **extra}
        r = await self.http.post(
            f"{self.settings.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=90,
        )
        if r.status_code != 200:
            raise LLMError(_friendly_error(r.status_code, r.text))
        data = r.json()
        if "error" in data:
            raise LLMError(_friendly_error(500, json.dumps(data)))
        return data

    async def stream_chat(
        self, messages: list[dict], model: str, tools: list[dict] | None = None
    ) -> AsyncIterator[dict]:
        """Yields {"type": "text"|"tool_calls"|"usage"|"finish", ...} events."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": 0.6,
            "max_tokens": 4096,
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools
        calls: dict[int, dict] = {}
        finish = None
        async with self.http.stream(
            "POST",
            f"{self.settings.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=httpx.Timeout(120, connect=20),
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                raise LLMError(_friendly_error(r.status_code, body))
            async for line in r.aiter_lines():
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    err = chunk["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise LLMError(f"Model error: {msg}")
                if chunk.get("usage"):
                    yield {"type": "usage", "usage": chunk["usage"], "model": chunk.get("model")}
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield {"type": "text", "text": text}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index")
                        if idx is None:
                            # Some providers omit the index: a new id starts a new call, otherwise
                            # the delta continues the latest one.
                            idx = max(calls) if calls else 0
                            if tc.get("id") and calls.get(idx, {}).get("id") not in (None, "", tc["id"]):
                                idx += 1
                        slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        # Most providers send the name once; some repeat it in every delta.
                        if fn.get("name") and fn["name"] != slot["name"]:
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        if calls:
            ordered = [calls[i] for i in sorted(calls)]
            for n, c in enumerate(ordered):
                c["id"] = c["id"] or f"call_{int(time.time() * 1000)}_{n}"
            yield {"type": "tool_calls", "calls": ordered}
        yield {"type": "finish", "reason": finish}


def _per_million(price: Any) -> float | None:
    try:
        return round(float(price) * 1_000_000, 3)
    except (TypeError, ValueError):
        return None
