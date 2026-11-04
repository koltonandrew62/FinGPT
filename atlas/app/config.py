"""Settings, .env handling and file locations for ATLAS."""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

# Tried in order when ATLAS_MODEL is empty or not offered by OpenRouter.
MODEL_PREFERENCES = [
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-5",
    "google/gemini-2.5-pro",
    "openai/gpt-4o",
]
# Cheap, fast models for the web-search research sub-call.
SEARCH_MODEL_PREFERENCES = [
    "google/gemini-2.5-flash",
    "openai/gpt-4o-mini",
    "anthropic/claude-haiku-4.5",
]

# Tools that always show an Approve / Deny card before they run.
SENSITIVE_TOOLS = {
    "send_email",
    "run_command",
    "close_application",
    "power_action",
    "write_file",
    "read_file",
    "type_text",
    "press_keys",
}

DEFAULT_WATCHLIST = ["^GSPC", "^IXIC", "^DJI", "^VIX", "BTC-USD", "ETH-USD", "GC=F", "CL=F", "^TNX"]

# Keys the settings screen is allowed to write into .env.
EDITABLE_KEYS = {
    "OPENROUTER_API_KEY",
    "ATLAS_MODEL",
    "ATLAS_SEARCH_MODEL",
    "ASSISTANT_NAME",
    "USER_NAME",
    "USER_ADDRESS_AS",
    "HOME_CITY",
    "UNITS",
    "NEWS_REGION",
    "NEWS_LANGUAGE",
    "MARKET_WATCHLIST",
    "EMAIL_ADDRESS",
    "EMAIL_PASSWORD",
    "SMTP_HOST",
    "SMTP_PORT",
    "IMAP_HOST",
    "IMAP_PORT",
}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # strip trailing inline comments on unquoted values
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _quote(value: str) -> str:
    value = str(value).replace("\n", " ").strip()
    if value == "" or any(ch in value for ch in " #'\"=\t"):
        return '"' + value.replace('"', "'") + '"'
    return value


def load_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        values[key] = _unquote(val)
    return values


def update_env_file(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Set keys in .env, keeping comments, ordering and unrelated keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key in remaining:
                out.append(f"{key}={_quote(remaining.pop(key))}")
                continue
        out.append(raw)
    for key, val in remaining.items():
        out.append(f"{key}={_quote(val)}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    api_key: str = ""
    model: str = ""
    search_model: str = ""
    assistant_name: str = "ATLAS"
    user_name: str = ""
    address_as: str = ""
    home_city: str = ""
    units: str = "imperial"
    news_region: str = "US"
    news_language: str = "en"
    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    email_address: str = ""
    email_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 0
    imap_host: str = ""
    imap_port: int = 0
    auto_approve: set[str] = field(default_factory=set)
    allow_shell: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    base_url: str = "https://openrouter.ai/api/v1"
    open_browser: bool = True

    @property
    def os_name(self) -> str:
        system = platform.system()
        if system == "Darwin":
            return f"macOS {platform.mac_ver()[0]}".strip()
        if system == "Windows":
            return f"Windows {platform.release()}"
        return f"{system} {platform.release()}"

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password)

    def needs_confirmation(self, tool_name: str) -> bool:
        return tool_name in SENSITIVE_TOOLS and tool_name not in self.auto_approve

    @classmethod
    def load(cls) -> "Settings":
        file_values = load_env_file()

        def get(key: str, default: str = "") -> str:
            if key in os.environ and os.environ[key] != "":
                return os.environ[key]
            return file_values.get(key, default)

        def get_int(key: str, default: int) -> int:
            try:
                return int(get(key, str(default)) or default)
            except ValueError:
                return default

        watch = _split(get("MARKET_WATCHLIST"))
        return cls(
            api_key=get("OPENROUTER_API_KEY"),
            model=get("ATLAS_MODEL"),
            search_model=get("ATLAS_SEARCH_MODEL"),
            assistant_name=get("ASSISTANT_NAME", "ATLAS") or "ATLAS",
            user_name=get("USER_NAME"),
            address_as=get("USER_ADDRESS_AS"),
            home_city=get("HOME_CITY"),
            units=(get("UNITS", "imperial") or "imperial").lower(),
            news_region=(get("NEWS_REGION", "US") or "US").upper(),
            news_language=(get("NEWS_LANGUAGE", "en") or "en").lower(),
            watchlist=watch or list(DEFAULT_WATCHLIST),
            email_address=get("EMAIL_ADDRESS"),
            email_password=get("EMAIL_PASSWORD"),
            smtp_host=get("SMTP_HOST"),
            smtp_port=get_int("SMTP_PORT", 0),
            imap_host=get("IMAP_HOST"),
            imap_port=get_int("IMAP_PORT", 0),
            auto_approve=set(_split(get("AUTO_APPROVE"))),
            allow_shell=_bool(get("ALLOW_SHELL"), True),
            host=get("ATLAS_HOST", "127.0.0.1") or "127.0.0.1",
            port=get_int("ATLAS_PORT", 8765),
            base_url=(get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/"),
            open_browser=_bool(get("ATLAS_OPEN_BROWSER"), True),
        )
