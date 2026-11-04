"""Tool registry. Each tool is an async function `fn(ctx, **args)`."""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..config import Settings


@dataclass
class ToolContext:
    settings: Settings
    http: httpx.AsyncClient
    llm: Any  # OpenRouter (kept loose to avoid an import cycle)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Awaitable[Any]]
    label: str
    preview: Callable[[dict], dict] | None = None
    timeout: float = 60
    # Optional dynamic approval check: async gate(ctx, args) -> preview dict if the user must approve
    # this particular call, or None to run it straight away. Used for e.g. "Book now" clicks in the browser.
    gate: Callable[[Any, dict], Awaitable[dict | None]] | None = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


class ToolError(Exception):
    """Raised by tools for expected, user-facing failures."""


TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    properties: dict | None = None,
    required: list[str] | None = None,
    label: str | None = None,
    preview: Callable[[dict], dict] | None = None,
    timeout: float = 60,
    gate: Callable[[Any, dict], Awaitable[dict | None]] | None = None,
):
    def deco(fn):
        TOOLS[name] = Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties or {}, "required": required or []},
            func=fn,
            label=label or name.replace("_", " ").title(),
            preview=preview,
            timeout=timeout,
            gate=gate,
        )
        return fn

    return deco


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS.values()]


async def call_tool(name: str, args: dict, ctx: ToolContext) -> str:
    t = TOOLS.get(name)
    if not t:
        return json.dumps({"error": f"Unknown tool '{name}'."})
    allowed = set(t.parameters.get("properties", {}))
    clean = {k: v for k, v in (args or {}).items() if k in allowed}
    missing = [r for r in t.parameters.get("required", []) if r not in clean]
    if missing:
        return json.dumps({"error": f"Missing required argument(s): {', '.join(missing)}"})
    try:
        result = t.func(ctx, **clean)
        if inspect.isawaitable(result):
            result = await result
    except ToolError as exc:
        return json.dumps({"error": str(exc)})
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"Network error while running {name}: {exc.__class__.__name__}: {exc}"})
    except Exception as exc:  # keep the assistant alive whatever a tool does
        return json.dumps({"error": f"{name} failed: {exc.__class__.__name__}: {exc}"})
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:  # e.g. circular data or an int too long to print
        return json.dumps({"error": f"{name} returned a result that can't be shown: {exc}"})


def load_all() -> None:
    # Importing registers the tools.
    from . import general, news, web, markets, weather, email_tools, pc, memory  # noqa: F401
    from . import browser, calendar_tools, comms, smart_home, errands  # noqa: F401
    # live finance, economy and sports data (read-only public sources)
    from . import finance_data, data_sources, sports  # noqa: F401
