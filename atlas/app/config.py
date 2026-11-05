"""Settings, .env handling and file locations for ATLAS."""
from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _user_dir() -> Path:
    """One per-user folder for settings and data, shared by every copy of ATLAS on this computer, so memories
    and settings survive reinstalls and updates. Override with ATLAS_HOME."""
    override = os.environ.get("ATLAS_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "ATLAS"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ATLAS"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "atlas"


USER_DIR = _user_dir()
ENV_PATH = USER_DIR / ".env"
DATA_DIR = USER_DIR / "data"
# Where earlier versions kept them (inside the program folder); migrate_legacy_files() moves them over.
LEGACY_ENV_PATH = ROOT / ".env"
LEGACY_DATA_DIR = ROOT / "data"

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
    "send_text_message",
    "make_phone_call",
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
    # personal details used for bookings, forms and directions
    "USER_PHONE",
    "HOME_ADDRESS",
    "WORK_ADDRESS",
    # browser agent
    "BROWSER_HEADLESS",
    "BROWSER_CHANNEL",
    "BROWSER_EXECUTABLE",
    "BROWSER_VISION_MODEL",
    # calendar
    "CALENDAR_ICS_URLS",
    "CALENDAR_PROVIDER",
    "CALENDAR_DAY_HOURS",
    # texting / calls
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY_SID",  # optional API key (SK...): REST calls use it instead of the Auth Token
    "TWILIO_API_KEY_SECRET",
    "TWILIO_FROM_NUMBER",
    "DEFAULT_COUNTRY_CODE",
    # the assistant's own phone line (app/phone.py): call or text a Twilio number to talk to it
    "PHONE_ENABLED",
    "PHONE_NUMBER",
    "PHONE_ALLOWED_NUMBERS",
    "PHONE_PUBLIC_URL",
    "PHONE_TUNNEL",
    "PHONE_VOICE",
    "PHONE_LANGUAGE",
    "PHONE_PORT",
    "PHONE_PIN",
    # smart home
    "HOME_ASSISTANT_URL",
    "HOME_ASSISTANT_TOKEN",
    "HOME_ASSISTANT_CONFIRM_DOMAINS",
    # live data: finance (Yahoo Finance, CoinGecko, SEC EDGAR, FRED) and sports (ESPN)
    "FINANCE_REGION",
    "FINANCE_USER_AGENT",
    "COINGECKO_API_KEY",
    "SEC_CONTACT_EMAIL",
    "FRED_API_KEY",
    "SPORTS_TIMEZONE",
    "SPORTS_PANEL_LEAGUES",
}

# Editable keys whose values are credentials: never sent back to the browser, and a blank value in
# the settings form means "keep the current one".
SECRET_KEYS = {
    "OPENROUTER_API_KEY",
    "EMAIL_PASSWORD",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY_SECRET",
    "PHONE_PIN",
    "HOME_ASSISTANT_TOKEN",
    "CALENDAR_ICS_URLS",
    "COINGECKO_API_KEY",
    "FRED_API_KEY",
}


def _unquote(value: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"'):
        q = value[0]
        i = 1
        while i < len(value):
            if q == '"' and value[i] == "\\" and value[i + 1 : i + 2] == '"':
                i += 2  # escaped quote inside a double-quoted value
                continue
            if value[i] == q:
                break
            i += 1
        rest = value[i + 1 :].strip()
        if i < len(value) and (not rest or rest.startswith("#")):
            inner = value[1:i]
            return inner.replace('\\"', '"') if q == '"' else inner
        if len(value) >= 2 and value[-1] == q:
            return value[1:-1]
    # strip trailing inline comments on unquoted values
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


# Every character str.splitlines() treats as a line break. A value containing one would otherwise
# smuggle extra KEY=value lines into .env (e.g. AUTO_APPROVE, which the settings screen can't set).
_LINE_BREAKS = re.compile("[\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]+")


def _quote(value: str) -> str:
    value = _LINE_BREAKS.sub(" ", str(value)).strip()
    if value == "" or any(ch in value for ch in " #'\"=\t"):
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _env_key(line: str) -> tuple[str, str] | None:
    """Return (key, prefix) for a KEY=value line, or None for blanks/comments."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.partition("=")[0].strip()
    prefix = ""
    if key.startswith("export "):
        key, prefix = key[len("export "):].strip(), "export "
    return key, prefix


def load_env_file(path: Path | None = None) -> dict[str, str]:
    # Resolved at call time (not as a default argument) so ENV_PATH can be redirected, e.g. in tests.
    path = ENV_PATH if path is None else path
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _env_key(raw)
        if parsed is None:
            continue
        values[parsed[0]] = _unquote(raw.strip().partition("=")[2])
    return values


def update_env_file(updates: dict[str, str], path: Path | None = None) -> None:
    """Set keys in .env, keeping comments, ordering and unrelated keys."""
    path = ENV_PATH if path is None else path
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    out: list[str] = []
    for raw in lines:
        parsed = _env_key(raw)
        if parsed and parsed[0] in updates:
            key, prefix = parsed
            # Rewrite every occurrence: load_env_file keeps the last one, so skipping a
            # duplicate would silently undo the change.
            out.append(f"{prefix}{key}={_quote(updates[key])}")
            written.add(key)
            continue
        out.append(raw)
    for key, val in updates.items():
        if key not in written:
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
    user_phone: str = ""
    home_address: str = ""
    work_address: str = ""
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
            user_phone=get("USER_PHONE"),
            home_address=get("HOME_ADDRESS"),
            work_address=get("WORK_ADDRESS"),
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


_MERGE_LISTS = {"memory.json": "fact", "contacts.json": "name", "notes.json": "id", "tasks.json": "id", "reminders.json": "id"}


def migrate_legacy_files() -> list[str]:
    """Bring settings and data from an older copy (atlas/.env, atlas/data/) into the per-user folder.

    Never deletes anything. Files missing in the user folder are copied; for list files (memories, contacts,
    notes, tasks, reminders) entries the user folder doesn't have yet are merged in, so memories saved by two
    different copies of ATLAS end up together. Returns a short description of what was done.
    """
    import json
    import shutil

    done: list[str] = []
    try:
        USER_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return done
    try:
        if LEGACY_ENV_PATH.is_file() and LEGACY_ENV_PATH.resolve() != ENV_PATH.resolve():
            if not ENV_PATH.exists():
                shutil.copy2(LEGACY_ENV_PATH, ENV_PATH)
                done.append("settings")
            else:  # keep current values; add keys only the old file has
                old_vals, cur_vals = load_env_file(LEGACY_ENV_PATH), load_env_file(ENV_PATH)
                missing = {k: v for k, v in old_vals.items() if k not in cur_vals and v}
                if missing:
                    update_env_file(missing, ENV_PATH)
                    done.append(f"{len(missing)} setting(s)")
    except OSError:
        pass
    if not LEGACY_DATA_DIR.is_dir() or LEGACY_DATA_DIR.resolve() == DATA_DIR.resolve():
        return done
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for src in LEGACY_DATA_DIR.iterdir():
        dst = DATA_DIR / src.name
        try:
            if src.name in _MERGE_LISTS and src.is_file() and dst.is_file():
                key = _MERGE_LISTS[src.name]
                old_items = json.loads(src.read_text(encoding="utf-8")) or []
                cur_items = json.loads(dst.read_text(encoding="utf-8")) or []
                if isinstance(old_items, list) and isinstance(cur_items, list):
                    seen = {str(i.get(key, "")).strip().lower() for i in cur_items if isinstance(i, dict)}
                    extra = [i for i in old_items if isinstance(i, dict) and str(i.get(key, "")).strip().lower() not in seen]
                    if extra:
                        dst.write_text(json.dumps(cur_items + extra, indent=2, ensure_ascii=False), encoding="utf-8")
                        done.append(f"{len(extra)} {src.stem} entr{'y' if len(extra) == 1 else 'ies'}")
            elif not dst.exists():
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                done.append(src.name)
        except (OSError, ValueError, shutil.Error):
            continue
    return done
