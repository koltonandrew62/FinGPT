"""FastAPI server: serves the HUD and the local API. Binds to 127.0.0.1 only."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import threading
import unicodedata
import webbrowser
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import tools as tool_registry
from .agent import ConfirmationBroker, run_turn
from .config import EDITABLE_KEYS, SECRET_KEYS, WEB_DIR, Settings, load_env_file, update_env_file
from .llm import LLMError, OpenRouter
from .tools import ToolContext
from .tools.calendar_tools import agenda_for_panel
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
    try:
        from .tools import browser

        await browser.shutdown()
    except Exception:
        pass
    await S.http.aclose()


app = FastAPI(title="ATLAS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # Block DNS-rebinding: only answer to localhost names.
    raw_host = (request.headers.get("host") or "").strip().lower()
    if raw_host.startswith("["):  # [::1] or [::1]:8765
        host = raw_host[1:].partition("]")[0]
    else:
        host = raw_host.rsplit(":", 1)[0] if raw_host.count(":") == 1 else raw_host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"error": "forbidden host"}, status_code=403)
    # Refuse anything another site sent the browser to (its link, redirect, form or script). ATLAS's own browser
    # agent can be steered to any address by the pages it visits; if it loaded the HUD here, it would hold the token
    # and could press Approve on its own cards. Typed addresses and bookmarks send "none", the HUD "same-origin".
    if request.headers.get("sec-fetch-site", "").strip().lower() in {"cross-site", "same-site"}:
        return JSONResponse(
            {"error": "cross-site request: open ATLAS by typing its address, not through a link on another site"},
            status_code=403,
        )
    # Every API call must carry the per-launch token embedded in the page. Compared as bytes: compare_digest
    # raises on non-ASCII text, which a stray header byte would otherwise turn into a 500.
    if request.url.path.startswith("/api/") and not secrets.compare_digest(
        request.headers.get("x-atlas-token", "").encode("latin-1", "replace"), S.token.encode()
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
        "user_phone": s.user_phone,
        "home_address": s.home_address,
        "work_address": s.work_address,
        # Every other editable setting: plain values for normal keys, only a "configured" flag for secrets.
        "extra": _extra_settings(),
        "secrets_configured": {k: bool(_raw_setting(k)) for k in sorted(SECRET_KEYS)},
    }


def _raw_setting(key: str) -> str:
    import os

    return os.environ.get(key) or load_env_file().get(key, "")


_STATE_KEYS = {
    "BROWSER_HEADLESS", "BROWSER_CHANNEL", "BROWSER_EXECUTABLE", "BROWSER_VISION_MODEL",
    "CALENDAR_PROVIDER", "CALENDAR_DAY_HOURS", "TWILIO_ACCOUNT_SID", "TWILIO_FROM_NUMBER",
    "DEFAULT_COUNTRY_CODE", "HOME_ASSISTANT_URL", "HOME_ASSISTANT_CONFIRM_DOMAINS", "MARKET_WATCHLIST",
}


# A password written into an address (https://user:pass@ha.example.com) is a secret too: shown masked, and a saved
# value that still carries the mask keeps the stored password.
_URL_PASSWORD = re.compile(r"^([a-z][a-z0-9+.-]*://[^/?#@:]*):([^/?#]*)@", re.I)  # to the last @, as URL parsers do
_MASK = "••••"


def _mask_password(value: str) -> str:
    return _URL_PASSWORD.sub(lambda m: f"{m.group(1)}:{_MASK}@", value, count=1)


def _keep_password(value: str, current: str) -> str:
    new, old = _URL_PASSWORD.match(value), _URL_PASSWORD.match(current or "")
    if not new or new.group(2) != _MASK:
        return value
    return value[: new.start(2)] + (old.group(2) if old else "") + value[new.end(2):]


def _extra_settings() -> dict:
    return {k: _mask_password(_raw_setting(k)) for k in sorted(_STATE_KEYS - SECRET_KEYS)}


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
        if key in SECRET_KEYS and not value:
            continue
        updates[key] = _keep_password(value, _raw_setting(key))
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
        turn = run_turn(body.messages, S.settings, S.llm, S.broker, S.ctx())
        try:
            async for ev in turn:
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Internal error: {exc}'})}\n\n"
        finally:
            # Stopping early (client gone) must still run the turn's cleanup now, e.g. withdraw a pending
            # approval card, instead of whenever the abandoned generator happens to be collected.
            await turn.aclose()

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


@app.get("/api/agenda")
async def agenda(days: int = 2):
    return await agenda_for_panel(S.ctx(), days=max(1, min(days, 14)))


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


def _cell_width(text: str) -> int:
    """Terminal columns a string takes: wide (CJK/emoji) characters count 2, combining marks 0."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def banner(name: str, url: str) -> str:
    """The startup box, sized to its longest line so the right border always lines up."""
    pad = max(0, 10 - _cell_width(name))
    rows = [f"{name}{' ' * pad} online  →  {url}", "Press Ctrl+C to shut down."]
    inner = max(42, *(_cell_width(r) for r in rows))
    rule = "─" * (inner + 4)
    lines = [f"┌{rule}┐", *(f"│  {r}{' ' * (inner - _cell_width(r))}  │" for r in rows), f"└{rule}┘"]
    return "\n" + "\n".join("  " + line for line in lines) + "\n"


def _print_banner(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:  # e.g. a Windows console on a legacy code page with output redirected
        table = str.maketrans({"┌": "+", "┐": "+", "└": "+", "┘": "+", "─": "-", "│": "|", "→": ">"})
        print(text.translate(table).encode("ascii", "replace").decode("ascii"))


def main() -> None:
    import uvicorn

    host = "127.0.0.1" if S.settings.host not in {"127.0.0.1", "localhost"} else S.settings.host
    port = _free_port(host, S.settings.port)
    S.port = S.settings.port = port
    url = f"http://127.0.0.1:{port}/"
    _print_banner(banner(S.settings.assistant_name, url))
    if not S.settings.api_key:
        print("  No OpenRouter key yet — the page will ask for it.\n")
    if S.settings.open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
