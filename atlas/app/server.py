"""FastAPI server: serves the HUD and the local API. Binds to 127.0.0.1 only."""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import tools as tool_registry
from .agent import ConfirmationBroker, run_turn
from .config import EDITABLE_KEYS, WEB_DIR, Settings, update_env_file
from .llm import LLMError, OpenRouter
from .tools import ToolContext
from .tools.markets import watchlist_snapshot
from .tools.memory import delete_memory, memories, pending_reminders, pop_due_reminders
from .tools.news import CATEGORIES, headlines_for_panel
from .tools.pc import capabilities, system_snapshot
from .tools.weather import get_weather_data

tool_registry.load_all()


class State:
    settings: Settings
    http: httpx.AsyncClient
    llm: OpenRouter
    broker: ConfirmationBroker
    token: str
    port: int

    def ctx(self) -> ToolContext:
        return ToolContext(settings=self.settings, http=self.http, llm=self.llm)

    def reload(self) -> None:
        port = self.settings.port if hasattr(self, "settings") else None
        self.settings = Settings.load()
        if port:
            self.settings.port = port
        self.llm = OpenRouter(self.settings, self.http)


S = State()
S.token = secrets.token_urlsafe(24)
S.broker = ConfirmationBroker()
S.settings = Settings.load()
S.port = S.settings.port


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.http = httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": "ATLAS-assistant/1.0"})
    S.llm = OpenRouter(S.settings, S.http)
    try:
        import psutil

        psutil.cpu_percent(interval=None)  # prime the CPU counter
    except Exception:
        pass
    yield
    await S.http.aclose()


app = FastAPI(title="ATLAS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # Block DNS-rebinding: only answer to localhost names.
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"error": "forbidden host"}, status_code=403)
    # Every API call must carry the per-launch token embedded in the page.
    if request.url.path.startswith("/api/") and not secrets.compare_digest(
        request.headers.get("x-atlas-token", ""), S.token
    ):
        return JSONResponse({"error": "bad token"}, status_code=401)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("{{ATLAS_TOKEN}}", S.token), headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ------------------------------------------------------------------ state/setup
@app.get("/api/state")
async def state():
    s = S.settings
    return {
        "assistant_name": s.assistant_name,
        "user_name": s.user_name,
        "address_as": s.address_as,
        "has_key": bool(s.api_key),
        "key_hint": (s.api_key[:8] + "…" + s.api_key[-4:]) if len(s.api_key) > 16 else "",
        "model": s.model,
        "search_model": s.search_model,
        "home_city": s.home_city,
        "units": s.units,
        "news_region": s.news_region,
        "news_language": s.news_language,
        "watchlist": s.watchlist,
        "email_address": s.email_address,
        "email_configured": s.email_configured,
        "smtp_host": s.smtp_host,
        "smtp_port": s.smtp_port or "",
        "imap_host": s.imap_host,
        "imap_port": s.imap_port or "",
        "os": s.os_name,
        "capabilities": capabilities(),
        "news_categories": CATEGORIES,
        "tools": sorted(tool_registry.TOOLS),
    }


class SetupBody(BaseModel):
    values: dict[str, str]


@app.post("/api/setup")
async def setup(body: SetupBody):
    updates = {}
    for key, value in body.values.items():
        if key not in EDITABLE_KEYS:
            raise HTTPException(400, f"Setting '{key}' can't be changed here.")
        value = (value or "").strip()
        # Blank secret fields mean "keep the current value".
        if key in {"OPENROUTER_API_KEY", "EMAIL_PASSWORD"} and not value:
            continue
        updates[key] = value
    if updates:
        update_env_file(updates)
    S.reload()
    return {"ok": True, "updated": sorted(updates)}


@app.get("/api/models")
async def models():
    try:
        items = await S.llm.tool_models()
        current = await S.llm.resolve_model()
    except LLMError as exc:
        return {"models": [], "current": S.settings.model, "error": str(exc)}
    except httpx.HTTPError as exc:
        return {"models": [], "current": S.settings.model, "error": f"Can't reach OpenRouter: {exc}"}
    return {"models": items, "current": current}


# ------------------------------------------------------------------ chat
class ChatBody(BaseModel):
    messages: list[dict]


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request):
    async def events():
        try:
            async for ev in run_turn(body.messages, S.settings, S.llm, S.broker, S.ctx()):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Internal error: {exc}'})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfirmBody(BaseModel):
    approved: bool


@app.post("/api/confirm/{cid}")
async def confirm(cid: str, body: ConfirmBody):
    if not S.broker.resolve(cid, body.approved):
        raise HTTPException(404, "No pending action with that id (it may have expired).")
    return {"ok": True}


# ------------------------------------------------------------------ dashboard panels
@app.get("/api/headlines")
async def headlines(category: str = "top"):
    try:
        return {"category": category, "articles": await headlines_for_panel(S.ctx(), category)}
    except Exception as exc:
        return {"category": category, "articles": [], "error": str(exc)}


@app.get("/api/markets")
async def markets():
    try:
        return {"quotes": await watchlist_snapshot(S.ctx())}
    except Exception as exc:
        return {"quotes": [], "error": str(exc)}


@app.get("/api/weather")
async def weather():
    try:
        return await get_weather_data(S.ctx())
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/system")
async def system():
    return await asyncio.to_thread(system_snapshot)


@app.get("/api/reminders")
async def reminders():
    return {"reminders": pending_reminders()}


@app.get("/api/reminders/due")
async def reminders_due():
    return {"due": pop_due_reminders()}


@app.get("/api/memory")
async def memory():
    return {"memories": memories()}


@app.delete("/api/memory/{mem_id}")
async def memory_delete(mem_id: str):
    return {"ok": delete_memory(mem_id)}


# ------------------------------------------------------------------ entry point
def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port found from {start} to {start + 19}.")


def main() -> None:
    import uvicorn

    host = "127.0.0.1" if S.settings.host not in {"127.0.0.1", "localhost"} else S.settings.host
    port = _free_port(host, S.settings.port)
    S.port = S.settings.port = port
    url = f"http://127.0.0.1:{port}/"
    print(
        "\n  ┌──────────────────────────────────────────────┐\n"
        f"  │  {S.settings.assistant_name:<10} online  →  {url:<27}│\n"
        "  │  Press Ctrl+C to shut down.                  │\n"
        "  └──────────────────────────────────────────────┘\n"
    )
    if not S.settings.api_key:
        print("  No OpenRouter key yet — the page will ask for it.\n")
    if S.settings.open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
