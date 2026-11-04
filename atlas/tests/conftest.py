"""Shared fixtures. Everything here is offline: no test may reach a real host or touch the real .env/data."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

ATLAS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_DIR))

# Redirect the .env and data folder BEFORE any app module that reads them is imported
# (server.py calls Settings.load() at import time; memory.py binds DATA_DIR at import time).
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="atlas-tests-"))
import app.config as config  # noqa: E402

REAL_ENV_PATH = config.ENV_PATH
REAL_DATA_DIR = config.DATA_DIR
config.ENV_PATH = _SESSION_TMP / ".env"
config.DATA_DIR = _SESSION_TMP / "data"

from app import tools as tool_registry  # noqa: E402

tool_registry.load_all()

from app import util  # noqa: E402
from app.tools import ToolContext, memory, news, markets, weather  # noqa: E402

FAKE_PUBLIC_IP = "93.184.215.14"  # what every host name "resolves" to in tests unless a test says otherwise

# Every variable Settings.load() looks at; cleared per test so the developer's shell can't leak in.
SETTINGS_ENV_KEYS = sorted(
    config.EDITABLE_KEYS
    | {"AUTO_APPROVE", "ALLOW_SHELL", "ATLAS_HOST", "ATLAS_PORT", "OPENROUTER_BASE_URL", "ATLAS_OPEN_BROWSER"}
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Per-test temp .env + data dir, clean environment and empty module caches."""
    env_path = tmp_path / ".env"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    # Tool modules bind DATA_DIR at import (`from ..config import DATA_DIR`): redirect each one.
    for name, mod in list(sys.modules.items()):
        if name.startswith("app.") and hasattr(mod, "DATA_DIR"):
            monkeypatch.setattr(mod, "DATA_DIR", data_dir)
    for key in SETTINGS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    news._CACHE.clear()
    markets._CACHE.clear()
    weather._GEO_CACHE.clear()
    weather._WX_CACHE.clear()
    yield {"env_path": env_path, "data_dir": data_dir}
    news._CACHE.clear()
    markets._CACHE.clear()
    weather._GEO_CACHE.clear()
    weather._WX_CACHE.clear()


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """No real DNS lookups in tests. Map a name to a list of addresses (or an exception) to change it."""
    table: dict[str, object] = {}

    def resolve(host: str, port: int) -> list[str]:
        answer = table.get(host, [FAKE_PUBLIC_IP])
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)

    monkeypatch.setattr(util, "_getaddrinfo", resolve)
    return table


def run(coro):
    return asyncio.run(coro)


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network request in test: {request.method} {request.url}")


class Router:
    """httpx.MockTransport handler that dispatches on host+path and records requests."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.requests: list[httpx.Request] = []

    def add(self, key: str, response):
        self.routes[key] = response
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.url.host}{request.url.path}"
        for candidate in (str(request.url), key, request.url.host):
            if candidate in self.routes:
                resp = self.routes[candidate]
                if callable(resp) and not isinstance(resp, httpx.Response):
                    resp = resp(request)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def hits(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in str(r.url)]


def mock_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_response(data, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


def make_ctx(http=None, llm=None, **settings) -> ToolContext:
    s = config.Settings(**settings)
    return ToolContext(settings=s, http=http, llm=llm)


@pytest.fixture
def settings():
    return config.Settings()


def file_stamp(p: Path):
    return p.stat().st_mtime_ns if p.exists() else None


@contextlib.contextmanager
def serve_app(app):
    """Run an ASGI app under a real uvicorn server on a free loopback port (lifespan off). Yields its base URL."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn didn't start")
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
