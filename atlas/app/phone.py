"""The assistant's own phone line: call or text a Twilio number and talk to the assistant from any phone.

How it fits together
* A separate, tiny FastAPI app (``phone_app``) served by its own uvicorn server on 127.0.0.1:PHONE_PORT. It has only
  the Twilio webhook routes below: never the main ATLAS API, the HUD or its token. The tunnel that makes it
  reachable from the internet therefore exposes nothing else.
      POST /twilio/voice          incoming call: greeting (or the PIN prompt), then listen
      POST /twilio/voice/pin      the keypad PIN, when PHONE_PIN is set
      POST /twilio/voice/gather   what the caller said (Twilio speech recognition)
      POST /twilio/voice/wait     polled while a longer answer is still being worked out
      POST /twilio/sms            incoming text (the reply goes out through the Twilio REST API)
      GET  /twilio/health         {"ok": true}
* Reachability: PHONE_PUBLIC_URL (the user's own https tunnel or domain forwarding to 127.0.0.1:PHONE_PORT) or,
  when that's empty, a Cloudflare quick tunnel (``cloudflared tunnel --url ...``) started and stopped with the line.
* Once the public address is known, the Twilio number's Voice and Messaging webhooks are pointed at it.

Security (these routes are on the internet)
* Every POST must carry a valid X-Twilio-Signature (HMAC-SHA1 of the public URL plus the sorted form fields, keyed
  with TWILIO_AUTH_TOKEN); anything else gets 403. Bodies over MAX_BODY bytes are refused. Twilio signs webhooks with
  the Auth Token only, so the line needs it even when an API key (TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET) is set;
  the key is then used for the line's REST calls (number lookup, webhook set-up, SMS replies).
* Only numbers in PHONE_ALLOWED_NUMBERS (default: USER_PHONE) reach the assistant. Caller ID can be spoofed, so a
  phone or text turn runs with PHONE_SAFE_TOOLS only: look-ups plus additions to ATLAS's own lists. No email,
  texts, calls, files, shell, browser, screen, clipboard, smart home, long-term memory or anything that needs an
  approval card. Texts also go without fetch_url: whoever sends a text never sees the reply (it goes to the real
  number), so a page fetch is the one way a forged text could carry data out. As a second lock, a turn that
  tries any other tool is stopped before that tool can run.
* PHONE_PIN (optional, 4-12 digits): a call must enter it on the keypad before the assistant says anything else.
  Without it, anyone who fakes an allowed number's caller ID can hear calendar, tasks, notes and reminders. Five
  wrong PINs lock calls out for an hour. Calls whose carrier reports a failed caller-ID check are turned away.
* Per-caller turn rate limits (10 minutes and a day), a budget for additions to lists, capped call length, bounded
  in-memory history that expires when idle. Ended calls can't be resumed by replaying Twilio's requests. Secrets
  are never logged, spoken or texted.

SMS note: in the US, texting from a 10-digit long code needs A2P 10DLC registration (Twilio Console > Messaging >
Regulatory compliance). Carriers block texts from unregistered numbers; calls work without it.

Settings (environment or .env): PHONE_ENABLED, PHONE_NUMBER, PHONE_ALLOWED_NUMBERS, PHONE_PUBLIC_URL, PHONE_TUNNEL,
PHONE_VOICE, PHONE_PORT, PHONE_LANGUAGE (speech recognition locale, default en-GB) and PHONE_PIN (a secret: keep it
out of logs and the settings UI's plain view), plus TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and optionally
TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET. A Settings attribute of the same name in lower case (phone_enabled, ...)
wins when present.

Module API: ``await start(settings, llm, ctx_factory)``, ``await stop()``, ``status()``; ``phone_app`` is the ASGI app.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import tools as tool_registry
from .config import SECRET_KEYS, SENSITIVE_TOOLS, Settings
from .tools import ToolError, comms
from .util import clip

log = logging.getLogger("atlas.phone")

# ------------------------------------------------------------------ limits and defaults
DEFAULT_PORT = 8790
DEFAULT_VOICE = "Polly.Brian-Neural"
DEFAULT_LANGUAGE = "en-GB"
MAX_BODY = 32 * 1024  # Twilio voice/SMS webhooks are 1-4 KB
MAX_FIELDS = 200
BODY_TIMEOUT = 10.0  # seconds to receive a request body: a trickled body can't hold a connection open
TURN_LIMIT = 30  # agent turns per caller ...
TURN_WINDOW = 600.0  # ... per 10 minutes
DAILY_TURN_LIMIT = 200  # ... and per day (bounds model spend if caller ID is faked)
WRITE_LIMIT = 15  # reminders, tasks and notes added by phone or text per caller per TURN_WINDOW
WRITE_TOOLS = frozenset({"set_reminder", "add_task", "add_note"})
PIN_ATTEMPTS = 3  # tries per call
PIN_FAIL_LIMIT = 5  # wrong PINs, across all calls and numbers, ...
PIN_FAIL_WINDOW = 3600.0  # ... per hour before calls are locked out
ENDED_CALLS = 256  # recently ended call ids remembered, so a replayed request can't resume a call
GOODBYE_MAX = 120  # a goodbye is short: longer speech is always a request
TEXT_MAX = 12_000  # characters of an answer considered at all before it is shortened for speech or SMS
MAX_CALL_SECONDS = 20 * 60
SESSION_IDLE = 30 * 60.0
HISTORY_LIMIT = 20  # messages kept per caller
HISTORY_TOOL_LIMIT = 1500  # characters of an old tool result kept in history
MAX_SESSIONS = 32
MAX_CALLS = 16
MAX_ACTIVE_TURNS = 8
SPEECH_LIMIT = 900  # characters of an answer read out per turn
SPEECH_INPUT_LIMIT = 1000
SMS_INPUT_LIMIT = 1600
SMS_SEGMENTS = 2

TWILIO_API = "https://api.twilio.com/2010-04-01"

# Spoken / texted phrases. Plain and original: the assistant's name comes from ASSISTANT_NAME.
UNKNOWN_CALLER = "Sorry, this number can't take your call. Goodbye."
GOODBYE = "Goodbye. Speak soon."
SILENCE_END = "I'll let you go. Goodbye."
REPROMPT_FIRST = "Sorry, I didn't catch that. What can I do for you?"
REPROMPT_AFTER = "Is there anything else?"
RATE_LIMITED_VOICE = (
    "You've asked a lot in the last few minutes, so I'm pausing the phone line for a little while. "
    "Please try again shortly. Goodbye."
)
RATE_LIMITED_SMS = "You've sent a lot of messages in the last few minutes. I'll answer again shortly."
LONG_CALL = "We've been on for a while, so I'll end the call here. Call back any time. Goodbye."
TOO_SLOW = "Sorry, that's taking longer than it should. Try asking again, or ask me at the desktop."
LOST_JOB = "Sorry, I lost track of that one. Could you ask again?"
BUSY = "I'm a little busy at the moment. Please ask again in a few seconds."
BLOCKED = "I can't do that over the phone. Ask me at the desktop and I'll take care of it there."
BLOCKED_SMS = "I can't do that by text. Ask me at the desktop and I'll take care of it there."
WRITE_LIMITED = (
    "I've added quite a lot by phone in the last few minutes, so I'll leave the rest for now. Ask me again shortly or "
    "at the desktop."
)
PIN_PROMPT = "Please enter your PIN, then press the hash key."
PIN_RETRY = "That PIN wasn't right. Please try again, then press the hash key."
PIN_FAILED = "That PIN wasn't right. Goodbye."
PIN_LOCKED = "This line isn't taking calls at the moment. Please try again later. Goodbye."
FAILED = "Sorry, something went wrong on my side. Please try again."
NO_ANSWER = "Sorry, I don't have an answer for that."
MEDIA_ONLY = "I can only read the text of a message, not pictures or attachments."
HOLD_PHRASES = ("One moment.", "Still working on it.", "Nearly there.")

# Keywords Twilio's opt-out handling answers itself; they never reach the assistant.
OPT_KEYWORDS = {
    "STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "START", "YES", "UNSTOP", "HELP", "INFO", "REVOKE",
    "OPTOUT",
}

CLOUDFLARED_HELP = (
    "The phone line needs a public address so Twilio can reach this computer. Install Cloudflare's free tunnel "
    "(Windows: winget install Cloudflare.cloudflared; macOS: brew install cloudflared; Linux: see "
    "developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads) and restart ATLAS, or set "
    "PHONE_PUBLIC_URL to your own https address that forwards to 127.0.0.1:{port}."
)


AUTH_TOKEN_REQUIRED = (
    "The phone line needs your Twilio Auth Token to verify incoming calls really come from Twilio (an API Key can't "
    "do that). Texting and outbound requests can use the API Key. Add TWILIO_AUTH_TOKEN in Settings → Texting & "
    "calls (it's on the Twilio Console home page)."
)


class PhoneError(Exception):
    """A user-facing reason the phone line can't start. Never contains a secret."""


# ------------------------------------------------------------------ tools allowed on a call or text
# Look-ups, plus additions to ATLAS's own lists. Caller ID can be spoofed, so nothing here sends anything, reads
# mail, files, the screen or the clipboard, opens anything on the PC or controls devices. `remember` is left out on
# purpose: long-term memory goes into every desktop conversation's instructions as trusted facts about the user, and
# it keeps only the newest 200, so a faked caller could plant instructions there or push real memories out.
_SAFE_CANDIDATES = (
    # news, search and reference
    "get_news", "web_search", "wikipedia", "fetch_url",
    # markets, companies, crypto, economy
    "get_market_data", "lookup_ticker", "get_stock_details", "get_technicals", "get_market_movers",
    "get_stock_news", "get_company_financials", "get_sec_filings", "get_earnings_calendar", "get_crypto",
    "get_market_sentiment", "get_yield_curve", "get_economic_data", "convert_currency",
    # sports
    "get_sports_scores", "get_team_schedule", "get_sports_standings", "get_sports_news",
    # everyday
    "get_weather", "get_time", "calculate",
    # agenda and notes
    "get_calendar_events", "check_availability", "list_tasks", "list_reminders", "search_notes",
    "set_reminder", "add_task", "add_note",
)
# Never on the phone line, whatever the candidates above say.
_NEVER_REMOTE = frozenset({"remember", "forget"})
# A forged text never sees the reply (it goes to the real number), so the only way it could carry data out is a
# request to an address it names: no page fetches by text.
_SMS_EXCLUDED = frozenset({"fetch_url"})


def phone_safe_tools() -> frozenset[str]:
    """The candidates that exist in this build, minus anything that is approval-gated in any way."""
    tool_registry.load_all()
    out = set()
    for name in _SAFE_CANDIDATES:
        t = tool_registry.TOOLS.get(name)
        if (
            t is None or name in SENSITIVE_TOOLS or name in _NEVER_REMOTE or t.gate is not None
            or t.preview is not None
        ):
            continue
        out.add(name)
    return frozenset(out)


PHONE_SAFE_TOOLS: frozenset[str] = phone_safe_tools()
SMS_SAFE_TOOLS: frozenset[str] = PHONE_SAFE_TOOLS - _SMS_EXCLUDED


def tools_for(channel: str) -> frozenset[str]:
    """The tools a phone ("phone") or text ("sms") turn may use."""
    return SMS_SAFE_TOOLS if channel == "sms" else PHONE_SAFE_TOOLS


# ------------------------------------------------------------------ settings
def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _get(settings, attr: str, key: str, default: str = "") -> str:
    """A Settings attribute when the integrator added one, else the environment / .env value."""
    value = getattr(settings, attr, None) if settings is not None else None
    if isinstance(value, (list, tuple, set, frozenset)):
        value = ",".join(str(v) for v in value)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    return comms._setting(key, default)


@dataclass
class PhoneConfig:
    enabled: bool = False
    number: str = ""  # PHONE_NUMBER in E.164
    allowed: frozenset[str] = frozenset()
    public_url: str = ""
    tunnel: str = "cloudflared"
    voice: str = DEFAULT_VOICE
    language: str = DEFAULT_LANGUAGE
    port: int = DEFAULT_PORT
    account_sid: str = ""
    auth_token: str = field(default="", repr=False)  # signs Twilio's webhooks: always needed to verify them
    api_key_sid: str = ""  # optional TWILIO_API_KEY_SID (SK...): used for REST calls instead of the Auth Token
    api_key_secret: str = field(default="", repr=False)
    pin: str = field(default="", repr=False)  # PHONE_PIN: digits a call must enter first; "" = no PIN
    problems: tuple[str, ...] = ()


def rest_auth(cfg: PhoneConfig) -> tuple[str, str]:
    """Basic-auth credentials for the line's Twilio REST calls: the API key when both halves are set, else the Account
    SID and Auth Token. Raises PhoneError (never containing a secret) when they can't be used."""
    try:
        return comms.twilio_auth(cfg.account_sid, cfg.auth_token, cfg.api_key_sid, cfg.api_key_secret)
    except ToolError as exc:
        raise PhoneError(str(exc)) from None


def load_config(settings) -> PhoneConfig:
    """Read the phone settings. Never raises: problems are collected for start() to report."""
    problems: list[str] = []
    enabled = _truthy(_get(settings, "phone_enabled", "PHONE_ENABLED", "false"))

    number = ""
    raw_number = _get(settings, "phone_number", "PHONE_NUMBER")
    if raw_number:
        try:
            number = comms.normalize_phone(raw_number)
        except ToolError as exc:
            problems.append(f"PHONE_NUMBER isn't usable: {exc}")

    allowed: set[str] = set()
    raw_allowed = _get(settings, "phone_allowed_numbers", "PHONE_ALLOWED_NUMBERS") or _get(
        settings, "user_phone", "USER_PHONE"
    )
    for item in re.split(r"[,;]", raw_allowed):
        if not item.strip():
            continue
        try:
            allowed.add(comms.normalize_phone(item))
        except ToolError as exc:
            problems.append(f"An allowed number isn't usable: {exc}")
    allowed.discard(number)  # the line must never answer itself (a text loop)

    voice = _get(settings, "phone_voice", "PHONE_VOICE", DEFAULT_VOICE)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,60}", voice):
        voice = DEFAULT_VOICE
    language = _get(settings, "phone_language", "PHONE_LANGUAGE", DEFAULT_LANGUAGE)
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}", language):
        language = DEFAULT_LANGUAGE
    raw_port = _get(settings, "phone_port", "PHONE_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        problems.append(f"PHONE_PORT '{raw_port}' isn't a port number (1-65535).")
        port = DEFAULT_PORT
    pin = re.sub(r"[\s-]", "", _get(settings, "phone_pin", "PHONE_PIN"))
    if pin and not re.fullmatch(r"[0-9]{4,12}", pin):
        # Never run without the PIN the user asked for: the line stays off until it's fixed.
        problems.append("PHONE_PIN should be 4 to 12 digits (6 or more is best), entered on the phone's keypad.")
    return PhoneConfig(
        enabled=enabled,
        number=number,
        allowed=frozenset(allowed),
        public_url=_get(settings, "phone_public_url", "PHONE_PUBLIC_URL"),
        tunnel=(_get(settings, "phone_tunnel", "PHONE_TUNNEL", "cloudflared") or "cloudflared").lower(),
        voice=voice,
        language=language,
        port=port,
        account_sid=comms._setting("TWILIO_ACCOUNT_SID"),
        auth_token=comms._setting("TWILIO_AUTH_TOKEN"),
        api_key_sid=comms._setting("TWILIO_API_KEY_SID"),
        api_key_secret=comms._setting("TWILIO_API_KEY_SECRET"),
        pin=pin,
        problems=tuple(problems),
    )


def clean_public_url(raw: str) -> str:
    url = str(raw or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        parts = None
    if (
        parts is None or parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment
        or parts.username or parts.password
    ):
        raise PhoneError(
            "PHONE_PUBLIC_URL should be an https:// address such as https://atlas.example.com (no query string), "
            "forwarding to the phone line's port on this computer."
        )
    return url


# ------------------------------------------------------------------ Twilio request signatures
def compute_signature(token: str, url: str, params) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(token, url + key1 + value1 + key2 + value2 ...)), keys sorted."""
    grouped: dict[str, list[str]] = {}
    for key, value in (params.items() if isinstance(params, dict) else params):
        grouped.setdefault(str(key), []).append(str(value))
    data = url + "".join(key + value for key in sorted(grouped) for value in sorted(grouped[key]))
    digest = hmac.new(token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _url_variants(url: str) -> list[str]:
    """Twilio may sign an https URL with or without its default port: accept both, as Twilio's own SDKs do."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    default = {"https": 443, "http": 80}.get(parts.scheme)
    try:
        port = parts.port
    except ValueError:
        return [url]
    rest = url[len(f"{parts.scheme}://{parts.netloc}"):]
    out = [url]
    if port is None and default:
        out.append(f"{parts.scheme}://{host}:{default}{rest}")
    elif port == default:
        out.append(f"{parts.scheme}://{host}{rest}")
    return out


def valid_signature(token: str, url: str, params, signature: str | None) -> bool:
    if not token or not signature:
        return False
    given = signature.strip().encode("latin-1", "replace")
    ok = False
    for candidate in _url_variants(url):
        expected = compute_signature(token, candidate, params).encode("ascii")
        ok |= hmac.compare_digest(expected, given)
    return ok


# ------------------------------------------------------------------ TwiML
_XML_INVALID = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")


def xml_escape(text) -> str:
    """Safe inside XML text or a double-quoted attribute: invalid characters dropped, markup escaped."""
    value = _XML_INVALID.sub("", str(text if text is not None else ""))
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    )


def twiml(*verbs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


def say(text: str, voice: str) -> str:
    return f'<Say voice="{xml_escape(voice)}">{xml_escape(text)}</Say>'


def gather(action: str, language: str, inner: str = "") -> str:
    return (
        f'<Gather input="speech" speechTimeout="auto" timeout="6" action="{xml_escape(action)}" method="POST" '
        f'language="{xml_escape(language)}" actionOnEmptyResult="true">{inner}</Gather>'
    )


def keypad(action: str, inner: str = "") -> str:
    """Collect keypad digits only (a PIN is never spoken aloud), ended by # or a pause."""
    return (
        f'<Gather input="dtmf" finishOnKey="#" timeout="8" action="{xml_escape(action)}" method="POST" '
        f'actionOnEmptyResult="true">{inner}</Gather>'
    )


def pause(seconds: int) -> str:
    return f'<Pause length="{int(seconds)}"/>'


def redirect(url: str) -> str:
    return f'<Redirect method="POST">{xml_escape(url)}</Redirect>'


def hangup() -> str:
    return "<Hangup/>"


def message(text: str) -> str:
    return f"<Message>{xml_escape(text)}</Message>"


# ------------------------------------------------------------------ text for the ear and for SMS
# These run on the model's answer inside ATLAS's event loop, so every pattern is linear: line-anchored patterns
# match spaces and tabs only (a `\s*` there spans lines, and `\s*\|?\s*` made a run of blank lines cubic), and
# patterns that look ahead for a closing mark are bounded or stop at the next opening one.
_FENCE = re.compile(r"```[^\n`]*\n?(.*?)(?:```|\Z)", re.S)
_MD_LINK = re.compile(
    r"!?\[([^\[\]\n]{0,300})\]\([ \t]{0,20}<?([^)\s>\[]{0,2048})>?(?:[ \t]{1,20}\"[^\"\n]{0,300}\")?[ \t]{0,20}\)"
)
_URL = re.compile(r"\bhttps?://[^\s<>()\[\]{}\"']+", re.I)
_HEADER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_QUOTE = re.compile(r"^[ \t]{0,3}>+[ \t]?", re.M)
_BULLET = re.compile(r"^[ \t]*(?:[-*+•▪◦]|\d{1,3}[.)])[ \t]+", re.M)
_RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.M)
_TABLE_SEP = re.compile(r"^[ \t]*(?:\|[ \t]*)?:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*(?:\|[ \t]*)?$", re.M)
_EMPH2 = re.compile(r"(\*\*|__|~~)(?=\S)(.{1,400}?)(?<=\S)\1", re.S)
_EMPH1 = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_SPEECH_SUBS = (("°", " degrees "), ("→", " to "), ("←", " from "), ("↔", " to "), ("&", " and "))
_DROP_CATEGORIES = {"So", "Cs", "Co", "Cn", "Cf", "Cc", "Sk"}


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or "a link"


def _strip_markdown(text: str, *, keep_urls: bool) -> list[str]:
    # Only the start is ever read out or texted; the cap keeps the work small whatever the model wrote.
    t = str(text or "")[:TEXT_MAX].replace("\r\n", "\n").replace("\r", "\n")
    t = _FENCE.sub(lambda m: m.group(1), t)

    def link(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        if keep_urls and url and url != label:
            return f"{label} ({url})" if label else url
        return label or (_domain(url) if url else "")

    t = _MD_LINK.sub(link, t)
    if not keep_urls:
        t = _URL.sub(lambda m: _domain(m.group(0)), t)
    t = _TABLE_SEP.sub("", t)
    t = _RULE.sub("", t)
    t = _HEADER.sub("", t)
    t = _QUOTE.sub("", t)
    t = _BULLET.sub("", t)
    for _ in range(2):  # nested emphasis
        t = _EMPH2.sub(lambda m: m.group(2), t)
    t = _EMPH1.sub(lambda m: m.group(1) or m.group(2) or "", t)
    t = t.replace("`", "")
    lines = []
    for line in t.split("\n"):
        line = line.strip()
        if line.startswith("|") or line.endswith("|"):
            line = ", ".join(c.strip() for c in line.strip("|").split("|") if c.strip())
        if line:
            lines.append(line)
    return lines


def _drop_symbols(text: str, keep: str = "") -> str:
    return "".join(
        ch for ch in text
        if ch in keep or ch in "\n\t" or (unicodedata.category(ch) not in _DROP_CATEGORIES and ch not in "︎️")
    )


def _cut(text: str, limit: int, tail: str) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - len(tail)]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= len(cut) * 0.5:
        return cut[: end + 1]
    space = cut.rfind(" ")
    if space >= len(cut) * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + tail


def speakable(text: str, limit: int = SPEECH_LIMIT) -> str:
    """An answer as it should be read out: no markdown, links as their site name, no emoji, at most `limit` chars."""
    out: list[str] = []
    for line in _strip_markdown(text, keep_urls=False):
        if out and not re.search(r"[.!?:;,]$", out[-1]):
            out[-1] += "."
        out.append(line)
    spoken = " ".join(out)
    for old, new in _SPEECH_SUBS:
        spoken = spoken.replace(old, new)
    spoken = _drop_symbols(spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    spoken = re.sub(r"\s+([.,!?;:])", r"\1", spoken)
    return _cut(spoken, limit, ".")


GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩ"
    "ΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñ"
    "üà"
)
GSM_EXTENDED = frozenset("^{}\\[~]|€\f")  # two septets each
_SMS_ASCII = str.maketrans({
    "‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"', "„": '"', "–": "-",
    "—": "-", "−": "-", "…": "...", "•": "-", " ": " ", " ": " ", " ": " ",
    "°": "", "\t": " ",
})


def sms_units(text: str) -> tuple[bool, int]:
    """(GSM-7?, length in septets or UTF-16 code units)."""
    if all(ch in GSM_BASIC or ch in GSM_EXTENDED for ch in text):
        return True, sum(2 if ch in GSM_EXTENDED else 1 for ch in text)
    return False, len(text.encode("utf-16-le")) // 2


def fit_sms(text: str, segments: int = SMS_SEGMENTS) -> str:
    """Trim to at most `segments` SMS segments (GSM-7: 160 / 153 each; UCS-2: 70 / 67 each)."""
    gsm, units = sms_units(text)
    if gsm:
        limit = 160 if segments == 1 else 153 * segments
    else:
        limit = 70 if segments == 1 else 67 * segments
    if units <= limit:
        return text
    tail = "..." if gsm else "…"
    budget = limit - (3 if gsm else 1)
    kept, used = [], 0
    for ch in text:
        cost = (2 if ch in GSM_EXTENDED else 1) if gsm else (2 if ord(ch) > 0xFFFF else 1)
        if used + cost > budget:
            break
        kept.append(ch)
        used += cost
    cut = "".join(kept)
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space >= len(cut) * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-\n") + tail


def sms_text(text: str, segments: int = SMS_SEGMENTS) -> str:
    """An answer as a text message: plain text, links kept, GSM-friendly punctuation, at most two segments."""
    body = "\n".join(_strip_markdown(text, keep_urls=True)).translate(_SMS_ASCII)
    body = _drop_symbols(body, keep="€£¥¤§")
    body = re.sub(r"[ ]{2,}", " ", body).strip()
    return fit_sms(body or NO_ANSWER, segments)


# ------------------------------------------------------------------ small helpers
# Each part is a list of whole phrases, none of which can be split into others, so a phrase run has exactly one
# parse. (An earlier form let "bye bye" be one phrase or two, which backtracked exponentially: about 40 "bye"s and
# then another word froze ATLAS's event loop.) "bye-bye" and "byebye" are "bye" twice.
_BYE_SEP = r"[\s,.!-]*"
_BYE_LEAD = (
    r"(?:ok(?:ay)?|alright|all right|right|no|nope|great|cool|perfect|lovely|thanks?|thank you(?: very much)?|"
    r"cheers|that's great|brilliant)"
)
_BYE_CORE = (
    r"(?:good[\s-]?bye|bye|hang up|that's (?:all|it|everything)|that is (?:all|it|everything)|that'll be all|"
    r"that will be all|nothing else|end (?:the )?call|i'm done|we're done|see you(?: later)?|"
    r"talk (?:to you )?(?:later|soon))"
)
_BYE_TAIL = r"(?:thanks?|thank you(?: very much)?|cheers|for now|bye|goodbye|then|now|for today)"
_GOODBYE = re.compile(
    rf"^(?:{_BYE_LEAD}{_BYE_SEP})*{_BYE_CORE}(?:{_BYE_SEP}{_BYE_TAIL})*{_BYE_SEP}$", re.I
)


def is_goodbye(speech: str) -> bool:
    text = re.sub(r"\s+", " ", str(speech or "")[: GOODBYE_MAX * 4].replace("’", "'")).strip().lower()
    return bool(text) and len(text) <= GOODBYE_MAX and bool(_GOODBYE.match(text))


def _clean_input(text, limit: int) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or ""))
    return value.strip()[:limit]


def mask_number(number: str) -> str:
    return f"{number[:3]}•••{number[-2:]}" if len(number) > 6 else "•••"


def redact(text: str, secret_values) -> str:
    for value in sorted({v for v in secret_values if v and len(v) >= 6}, key=len, reverse=True):
        text = text.replace(value, "[hidden]")
    return text


def _secret_values(settings, cfg: PhoneConfig) -> list[str]:
    # (PHONE_PIN is never given to the model or put in a message, so there's nothing to blank for it.)
    values = {
        cfg.auth_token, cfg.api_key_secret, getattr(settings, "api_key", ""), getattr(settings, "email_password", ""),
    }
    for key in SECRET_KEYS:
        raw = comms._setting(key)
        values.add(raw)
        values.update(p.strip() for p in raw.split(","))
    return [v for v in values if v]


def final_text(messages) -> str | None:
    """The last assistant message that is a reply, not text written before tool calls."""
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "assistant" or m.get("tool_calls"):
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


class RateLimiter:
    """Sliding window: at most `limit` events per `window` seconds per key."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        for other in [k for k, v in self.hits.items() if k != key and (not v or now - v[-1] >= self.window)]:
            del self.hits[other]
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def blocked(self, key: str) -> bool:
        """Whether `key` is at its limit right now (records nothing)."""
        now = self.clock()
        q = self.hits.get(key)
        return bool(q) and sum(1 for t in q if now - t < self.window) >= self.limit


class _DeclineBroker:
    """Approval broker for remote turns: nobody can press Approve on a phone, so any card is declined at once."""

    def __init__(self) -> None:
        self.pending: dict = {}

    def create(self):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(False)
        return secrets.token_urlsafe(16), fut

    def resolve(self, cid, approved) -> bool:
        return False

    def discard(self, cid) -> None:
        pass


@dataclass
class _Session:
    history: list = field(default_factory=list)
    last: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Call:
    sid: str
    caller: str
    started: float
    silences: int = 0
    answered: bool = False
    verified: bool = False  # PHONE_PIN entered (always False when there's no PIN)
    pin_tries: int = 0


@dataclass
class _Job:
    id: str
    call_sid: str
    task: asyncio.Task
    started: float  # time.monotonic(): Twilio's deadlines are real time
    holds: int = 0


# ------------------------------------------------------------------ cloudflared quick tunnel
_TRYCLOUDFLARE = re.compile(r"https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.trycloudflare\.com(?![\w.-])", re.I)


def parse_tunnel_url(text: str) -> str | None:
    """The quick tunnel's address from cloudflared's log output (not api.trycloudflare.com from an error line)."""
    for m in _TRYCLOUDFLARE.finditer(str(text or "")):
        name = m.group(1).lower()
        if name != "api":
            return f"https://{name}.trycloudflare.com"
    return None


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates: list[Path] = []
    try:
        if os.name == "nt":
            for env in ("ProgramFiles", "ProgramFiles(x86)"):
                base = os.environ.get(env)
                if base:
                    candidates.append(Path(base) / "cloudflared" / "cloudflared.exe")
            local = os.environ.get("LOCALAPPDATA")
            if local:
                winget = Path(local) / "Microsoft" / "WinGet"
                candidates.append(winget / "Links" / "cloudflared.exe")
                if (winget / "Packages").is_dir():
                    candidates.extend(sorted((winget / "Packages").glob("Cloudflare.cloudflared*/cloudflared.exe")))
        else:
            candidates += [
                Path("/opt/homebrew/bin/cloudflared"),
                Path("/usr/local/bin/cloudflared"),
                Path("/usr/bin/cloudflared"),
                Path.home() / ".local" / "bin" / "cloudflared",
            ]
        for path in candidates:
            if path.is_file():
                return str(path)
    except OSError:
        pass
    return None


# cloudflared's own config file for the quick tunnel. Without --config, cloudflared reads ~/.cloudflared/config.yml
# (or /etc/cloudflared/...) if the user has one for another tunnel, and ingress rules found there take precedence over
# --url: the quick tunnel would then serve whatever those rules point at (another local app) instead of the phone
# line. This file has no ingress rules, so --url is all it serves.
TUNNEL_CONFIG = (
    "# Written by ATLAS for its phone line's quick tunnel and deleted when the line stops.\n"
    "# No ingress rules here: the tunnel serves only the --url on its command line.\n"
    "no-autoupdate: true\n"
)


class CloudflaredTunnel:
    """`cloudflared tunnel --config <empty> --no-autoupdate --url http://127.0.0.1:PORT`: a temporary
    https://*.trycloudflare.com address. Output is read on a thread (works on any event loop) and kept only as a
    short tail for errors."""

    def __init__(self, binary: str, port: int, on_exit: Callable[[], None] | None = None) -> None:
        self.binary, self.port, self.on_exit = binary, port, on_exit
        self.url: str | None = None
        self.tail: deque[str] = deque(maxlen=30)
        self.proc: subprocess.Popen | None = None
        self.config_path: str | None = None
        self._event = threading.Event()

    @property
    def args(self) -> list[str]:
        config = ["--config", self.config_path] if self.config_path else []
        return [self.binary, "tunnel", *config, "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]

    def start(self) -> None:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True  # Ctrl+C in ATLAS's console doesn't kill it before we stop it
        fd, self.config_path = tempfile.mkstemp(prefix="atlas-phone-tunnel-", suffix=".yml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(TUNNEL_CONFIG)
            self.proc = subprocess.Popen(
                self.args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs,
            )
        except BaseException:
            self._remove_config()
            raise
        threading.Thread(target=self._read, name="cloudflared-output", daemon=True).start()

    def _remove_config(self) -> None:
        path, self.config_path = self.config_path, None
        if path:
            with contextlib.suppress(OSError):
                os.remove(path)

    def _read(self) -> None:
        proc = self.proc
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self.tail.append(line[:300])
                if self.url is None:
                    url = parse_tunnel_url(line)
                    if url:
                        self.url = url
                        self._event.set()
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError, AttributeError):
                proc.stdout.close()  # type: ignore[union-attr]
            self._event.set()
            if self.on_exit:
                try:
                    self.on_exit()
                except Exception:
                    pass

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def wait_url(self, timeout: float) -> str:
        await asyncio.to_thread(self._event.wait, timeout)
        if self.url:
            return self.url
        errors = [ln for ln in self.tail if " ERR " in ln or "error" in ln.lower()]
        detail = clip((errors or list(self.tail) or [""])[-1], 200)
        if not self.running():
            raise PhoneError(
                "cloudflared stopped before it opened a tunnel"
                + (f" ({detail})" if detail else "")
                + ". Check this computer's internet connection, or set PHONE_PUBLIC_URL to your own address."
            )
        raise PhoneError(
            f"cloudflared didn't report a tunnel address within {int(timeout)} seconds. Check the internet "
            "connection (and that a firewall allows cloudflared), or set PHONE_PUBLIC_URL to your own address."
        )

    def stop(self) -> None:
        proc = self.proc
        try:
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.terminate()
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(5)
            except OSError:
                pass
        finally:
            self._remove_config()


# ------------------------------------------------------------------ Twilio number configuration
async def _twilio_request(http, method: str, url: str, auth: tuple[str, str], **kwargs) -> dict:
    """One Twilio REST call. auth: (Account SID, Auth Token) or (API Key SID, API Key Secret)."""
    try:
        r = await http.request(method, url, auth=tuple(auth), timeout=20, **kwargs)
    except httpx.TimeoutException:
        raise PhoneError(
            "Twilio didn't answer in time while the phone line was being set up. Check the internet connection and "
            "restart the line."
        ) from None
    except httpx.HTTPError as exc:
        raise PhoneError(f"Couldn't reach Twilio ({exc.__class__.__name__}). Check the internet connection.") from None
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if r.status_code in (401, 403):
        extra = (
            " A restricted API key also needs permission to read and update phone numbers and send messages."
            if r.status_code == 403 and comms.uses_api_key(auth) else ""
        )
        raise PhoneError(f"Twilio rejected the credentials. Check {comms.credential_names(auth)} in Settings.{extra}")
    if r.status_code == 404:
        raise PhoneError("Twilio couldn't find that account or number. Check TWILIO_ACCOUNT_SID and PHONE_NUMBER.")
    if r.status_code >= 400:
        code = payload.get("code")
        detail = clip(str(payload.get("message") or "").strip(), 200)
        tag = f"{r.status_code}, error {code}" if code else str(r.status_code)
        raise PhoneError(f"Twilio refused the phone line setup ({tag}): {detail or 'no details given'}")
    return payload


async def configure_twilio_number(http, sid: str, auth: tuple[str, str], number: str, base_url: str) -> dict:
    """Point the number's Voice and Messaging webhooks at base_url. Returns {"sid", "voice", "sms"}. `sid` is the
    Account SID (in the URL); `auth` the basic-auth pair from rest_auth()."""
    sid = str(sid or "").strip()
    problem = comms.account_sid_problem(sid)  # never build a Twilio URL from an unchecked SID
    if problem:
        raise PhoneError(problem)
    list_url = f"{TWILIO_API}/Accounts/{sid}/IncomingPhoneNumbers.json"
    payload = await _twilio_request(http, "GET", list_url, auth, params={"PhoneNumber": number})
    items = payload.get("incoming_phone_numbers")
    match = next(
        (n for n in items if isinstance(n, dict) and n.get("phone_number") == number),
        None,
    ) if isinstance(items, list) else None
    if match is None:
        raise PhoneError(
            f"{comms.pretty_phone(number)} isn't a phone number on this Twilio account. Buy one in the Twilio Console "
            "(Phone Numbers > Buy a number) and put it in PHONE_NUMBER, or correct PHONE_NUMBER."
        )
    pn_sid = str(match.get("sid") or "")
    if not re.fullmatch(r"PN[0-9a-fA-F]{32}", pn_sid):
        raise PhoneError("Twilio returned an unexpected id for the phone number, so the line wasn't set up.")
    if match.get("voice_application_sid") or match.get("sms_application_sid") or match.get("trunk_sid"):
        raise PhoneError(
            f"{comms.pretty_phone(number)} is connected to a TwiML App or SIP trunk in the Twilio Console, which "
            "overrides webhooks. Disconnect it (Phone Numbers > Active numbers > the number > Configure) and restart "
            "the phone line."
        )
    caps = match.get("capabilities") if isinstance(match.get("capabilities"), dict) else {}
    if caps.get("voice") is False and caps.get("sms") is False:
        raise PhoneError(f"{comms.pretty_phone(number)} can't take calls or texts on Twilio.")
    data = {
        "VoiceUrl": f"{base_url}/twilio/voice",
        "VoiceMethod": "POST",
        "SmsUrl": f"{base_url}/twilio/sms",
        "SmsMethod": "POST",
    }
    await _twilio_request(
        http, "POST", f"{TWILIO_API}/Accounts/{sid}/IncomingPhoneNumbers/{pn_sid}.json", auth, data=data
    )
    return {"sid": pn_sid, "voice": caps.get("voice", True) is not False, "sms": caps.get("sms", True) is not False}


# ------------------------------------------------------------------ the line
try:  # uvicorn is a hard dependency of ATLAS; imported lazily-safe for tools that only parse settings
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None  # type: ignore[assignment]


if uvicorn is not None:

    class _EmbeddedServer(uvicorn.Server):
        """Runs inside ATLAS's own event loop: leaves Ctrl+C / SIGTERM to the main server."""

        @contextlib.contextmanager
        def capture_signals(self):
            yield

        def install_signal_handlers(self) -> None:  # uvicorn < 0.29
            pass


def _uvicorn_config(app, port: int):
    wanted = dict(
        app=app, host="127.0.0.1", port=port, lifespan="off", log_config=None, log_level="warning",
        access_log=False, server_header=False, date_header=False, proxy_headers=False, ws="none",
        timeout_keep_alive=5, timeout_graceful_shutdown=2, limit_concurrency=32, h11_max_incomplete_event_size=16384,
    )
    accepted = inspect.signature(uvicorn.Config).parameters
    return uvicorn.Config(**{k: v for k, v in wanted.items() if k in accepted})


class PhoneLine:
    """One phone line: its webhook app, server, tunnel, sessions and jobs."""

    first_wait = 8.0  # seconds a /gather request waits for the answer before putting the caller on hold
    poll_wait = 8.0  # ... and each /wait request after that (Twilio gives a webhook 15 s)
    job_deadline = 60.0  # total time for one spoken answer
    job_grace = 5.0  # ... after which the turn ends by itself even if nobody polls for it (the caller hung up)
    sms_deadline = 150.0
    tunnel_timeout = 30.0

    def __init__(self, *, run_turn: Callable | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._run_turn = run_turn
        self._clock = clock
        self.cfg = PhoneConfig()
        self.settings: Settings | None = None
        self.llm = None
        self.ctx_factory: Callable[[], Any] | None = None
        self.public_url: str | None = None
        self.port: int | None = None
        self._status = "off"
        self._error: str | None = None
        self._stopping = False
        self._server = None
        self._server_task: asyncio.Task | None = None
        self._tunnel: CloudflaredTunnel | None = None
        self._starter: asyncio.Task | None = None
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._calls: OrderedDict[str, _Call] = OrderedDict()
        self._jobs: dict[str, _Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._turns = RateLimiter(TURN_LIMIT, TURN_WINDOW, clock)
        self._daily = RateLimiter(DAILY_TURN_LIMIT, 86_400.0, clock)
        self._writes = RateLimiter(WRITE_LIMIT, TURN_WINDOW, clock)
        # Wrong PINs are counted for the whole line, not per number: a faker can claim any allowed number.
        # Deliberately not cleared by a restart, so restarting can't reset an attacker's budget.
        self._pin_fails = RateLimiter(PIN_FAIL_LIMIT, PIN_FAIL_WINDOW, clock)
        self._limit_noted: dict[str, float] = {}
        self._sms_seen: OrderedDict[str, None] = OrderedDict()
        self._ended: OrderedDict[str, None] = OrderedDict()
        self.app = create_app(self)

    # ---------------------------------------------------------------- lifecycle
    def status(self) -> dict:
        return {
            "enabled": bool(self.cfg.enabled),
            "number": self.cfg.number,
            "status": self._status,
            "public_url": self.public_url if self._status in ("starting", "online") else None,
            "error": self._error,
        }

    def configure(self, settings, llm, ctx_factory) -> PhoneConfig:
        """Load and check the settings without starting anything. Raises PhoneError when the line can't run."""
        self.settings, self.llm, self.ctx_factory = settings, llm, ctx_factory
        self.cfg = cfg = load_config(settings)
        if not cfg.enabled:
            return cfg
        if not cfg.account_sid:
            raise PhoneError(
                "The phone line needs your Twilio account: add TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in Settings "
                "(both are on the Twilio Console home page)."
            )
        problem = comms.account_sid_problem(cfg.account_sid)
        if problem:
            raise PhoneError(problem)
        if not cfg.auth_token:
            # Twilio signs its webhooks with the Auth Token; without it no incoming call or text can be verified.
            raise PhoneError(AUTH_TOKEN_REQUIRED)
        rest_auth(cfg)  # a malformed API key (both halves set, SID not SK...) is reported now, before anything starts
        if cfg.problems:
            raise PhoneError(cfg.problems[0])
        if not cfg.number:
            raise PhoneError(
                "Set PHONE_NUMBER to the Twilio number the assistant answers (e.g. +15552345678). Buy one in the "
                "Twilio Console under Phone Numbers > Buy a number."
            )
        if not cfg.allowed:
            raise PhoneError(
                "Nobody is allowed to call the line yet: set USER_PHONE (your own mobile number) or "
                "PHONE_ALLOWED_NUMBERS in Settings."
            )
        if cfg.tunnel not in ("cloudflared", "none"):
            raise PhoneError("PHONE_TUNNEL should be 'cloudflared' or 'none'.")
        main_port = getattr(settings, "port", None)
        if cfg.port and isinstance(main_port, int) and cfg.port == main_port:
            # The tunnel forwards the internet to PHONE_PORT: it must never be the main ATLAS server's port.
            raise PhoneError(
                f"PHONE_PORT can't be {main_port}, the port ATLAS itself uses. Set PHONE_PORT to another free port "
                f"(the default is {DEFAULT_PORT})."
            )
        if cfg.public_url:
            clean_public_url(cfg.public_url)
        elif cfg.tunnel == "none":
            raise PhoneError(
                "PHONE_TUNNEL is 'none', so set PHONE_PUBLIC_URL to the https address of your own tunnel or domain "
                f"that forwards to 127.0.0.1:{cfg.port}."
            )
        self._check_agent()
        return cfg

    async def start(self, settings, llm, ctx_factory) -> dict:
        """(Re)start the line with these settings. Returns status(). Never raises for configuration problems:
        they end up in status()["error"]. Can take up to ~35 s (tunnel + Twilio): run it as a background task."""
        await self.stop()
        self._starter = asyncio.current_task()
        self._error = None
        try:
            cfg = self.configure(settings, llm, ctx_factory)
            if not cfg.enabled:
                self._status = "off"
                return self.status()
            self._status = "starting"
            await self._start_server()
            self.public_url = await self._open_public_url()
            ctx = self.ctx_factory() if self.ctx_factory else None
            http = getattr(ctx, "http", None)
            if http is None:
                raise PhoneError("The phone line has no HTTP client to reach Twilio with.")
            await configure_twilio_number(http, cfg.account_sid, rest_auth(cfg), cfg.number, self.public_url)
            self._status = "online"
            log.warning("Phone line online: %s via %s", cfg.number, self.public_url)
        except asyncio.CancelledError:
            await self._teardown()
            self._status = "off"
            raise
        except PhoneError as exc:
            await self._teardown()
            self._fail(redact(str(exc), _secret_values(settings, self.cfg)))
        except Exception as exc:  # keep ATLAS running whatever happens here
            await self._teardown()
            self._fail(f"The phone line couldn't start ({exc.__class__.__name__}).")
        finally:
            if self._starter is asyncio.current_task():
                self._starter = None
        return self.status()

    async def stop(self) -> None:
        starter = self._starter
        if starter is not None and starter is not asyncio.current_task() and not starter.done():
            starter.cancel()
            await asyncio.wait({starter}, timeout=10)
        await self._teardown()
        self._status = "off"
        self._error = None

    def _fail(self, error: str) -> None:
        self._status = "error"
        self._error = error
        log.warning("Phone line: %s", error)

    def _check_agent(self) -> None:
        fn = self._get_run_turn()
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        has_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        if not (has_kwargs or {"channel", "allowed_tools"} <= set(params)):
            raise PhoneError("This version of ATLAS can't limit tools on phone calls, so the phone line stays off.")

    def _get_run_turn(self):
        if self._run_turn is not None:
            return self._run_turn
        from . import agent

        return agent.run_turn

    async def _start_server(self) -> None:
        if uvicorn is None:  # pragma: no cover
            raise PhoneError("uvicorn isn't installed, so the phone line can't run.")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name != "nt":  # on Windows SO_REUSEADDR would let two servers share the port
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.cfg.port))
        except OSError:
            sock.close()
            raise PhoneError(
                f"Port {self.cfg.port} on this computer is already in use. Set PHONE_PORT to a free port."
            ) from None
        self.port = sock.getsockname()[1]
        server = _EmbeddedServer(_uvicorn_config(self.app, self.port))
        self._server = server
        self._server_task = asyncio.create_task(self._serve(server, sock))
        deadline = time.monotonic() + 10
        while not server.started:
            if self._server_task.done() or time.monotonic() > deadline:
                raise PhoneError("The phone line's local web server didn't start.")
            await asyncio.sleep(0.02)

    async def _serve(self, server, sock) -> None:
        try:
            await server.serve(sockets=[sock])
        except (SystemExit, Exception) as exc:  # uvicorn exits the process on some startup errors: not ours
            log.warning("Phone line web server stopped (%s)", exc.__class__.__name__)
        finally:
            with contextlib.suppress(OSError):
                sock.close()
            if self._server is server and not self._stopping:
                # Close the tunnel too: left open, it would forward the internet to whatever program takes this
                # port next.
                tunnel = self._tunnel
                if tunnel is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(tunnel.stop)
                if self._status == "online":
                    self._fail("The phone line's local web server stopped. Restart the phone line.")

    async def _open_public_url(self) -> str:
        if self.cfg.public_url:
            return clean_public_url(self.cfg.public_url)
        binary = find_cloudflared()
        if not binary:
            raise PhoneError(CLOUDFLARED_HELP.format(port=self.port))
        loop = asyncio.get_running_loop()
        tunnel = CloudflaredTunnel(binary, self.port or self.cfg.port)

        def exited() -> None:  # on the reader thread
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._tunnel_exited, tunnel)

        tunnel.on_exit = exited
        try:
            tunnel.start()
        except OSError as exc:
            raise PhoneError(f"Couldn't start cloudflared ({exc.strerror or exc.__class__.__name__}).") from None
        self._tunnel = tunnel
        return await tunnel.wait_url(self.tunnel_timeout)

    def _tunnel_exited(self, tunnel: CloudflaredTunnel) -> None:
        if tunnel is self._tunnel and not self._stopping and self._status == "online":
            self._fail(
                "The Cloudflare tunnel closed, so calls and texts can't reach ATLAS. Restart the phone line to open a "
                "new one."
            )

    async def _teardown(self) -> None:
        self._stopping = True
        try:
            for job in list(self._jobs.values()):
                job.task.cancel()
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.wait(set(self._tasks), timeout=5)
            server, task = self._server, self._server_task
            if server is not None:
                server.should_exit = True
                if task is not None and not task.done():
                    await asyncio.wait({task}, timeout=5)
                    if not task.done():
                        server.force_exit = True
                        task.cancel()
                        await asyncio.wait({task}, timeout=2)
            tunnel = self._tunnel
            if tunnel is not None:
                await asyncio.to_thread(tunnel.stop)
        finally:
            self._server = self._server_task = self._tunnel = None
            self.public_url = None
            self.port = None
            self._jobs.clear()
            self._tasks.clear()
            self._calls.clear()
            self._sessions.clear()
            self._sms_seen.clear()
            self._limit_noted.clear()
            self._stopping = False

    # ---------------------------------------------------------------- request checks
    def _current(self) -> tuple[Any, Any]:
        """(settings, ctx) as of now: the main app may have reloaded its settings since start()."""
        ctx = None
        if self.ctx_factory is not None:
            try:
                ctx = self.ctx_factory()
            except Exception:
                ctx = None
        settings = getattr(ctx, "settings", None) or self.settings or Settings()
        return settings, ctx

    async def _verified(self, request: Request) -> tuple[dict | None, Response | None]:
        """The request's form fields if it really comes from Twilio for this account, else an error response."""
        forbidden = Response("forbidden", status_code=403, media_type="text/plain")
        if not self.cfg.auth_token or not self.public_url:
            return None, forbidden
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return None, forbidden
        too_big = Response("too large", status_code=413, media_type="text/plain")
        length = request.headers.get("content-length")
        if length and (not (length.isascii() and length.isdigit()) or int(length) > MAX_BODY):
            return None, too_big

        async def read() -> bytes | None:
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BODY:
                    return None
            return bytes(body)

        try:  # a deadline for the whole body: a client trickling bytes can't hold a connection slot open
            body = await asyncio.wait_for(read(), BODY_TIMEOUT)
        except asyncio.TimeoutError:
            return None, Response("timeout", status_code=408, media_type="text/plain")
        except Exception:  # the client went away mid-body
            return None, Response("bad request", status_code=400, media_type="text/plain")
        if body is None:
            return None, too_big
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except (UnicodeDecodeError, ValueError):
            return None, forbidden
        query = request.scope.get("query_string", b"").decode("latin-1")
        url = self.public_url + request.url.path + (f"?{query}" if query else "")
        if not valid_signature(self.cfg.auth_token, url, pairs, request.headers.get("x-twilio-signature")):
            return None, forbidden
        params: dict[str, str] = {}
        for key, value in pairs:
            params.setdefault(key, value)
        account = params.get("AccountSid", "")
        if account and not hmac.compare_digest(account.encode("utf-8", "replace"), self.cfg.account_sid.encode()):
            return None, forbidden
        return params, None

    def _caller(self, params: dict) -> str | None:
        number = str(params.get("From") or "").strip()
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number) or number not in self.cfg.allowed:
            return None
        return number

    @staticmethod
    def _caller_id_failed(params: dict) -> bool:
        """The carrier checked the caller ID (STIR/SHAKEN) and it failed: the number was very likely faked.
        Absent or "No-TN-Validation" (common outside the US) proves nothing either way and is let through."""
        return str(params.get("StirVerstat") or "").strip().lower().startswith("tn-validation-failed")

    # ---------------------------------------------------------------- sessions, calls, jobs
    def _session(self, number: str) -> _Session:
        now = self._clock()
        for key, s in list(self._sessions.items()):
            if key != number and now - s.last > SESSION_IDLE and not s.lock.locked():
                del self._sessions[key]
        session = self._sessions.get(number)
        if session is not None and now - session.last > SESSION_IDLE and not session.lock.locked():
            session = None  # idle too long: start afresh
        if session is None:
            session = _Session(last=now)
            self._sessions[number] = session
        self._sessions.move_to_end(number)
        while len(self._sessions) > MAX_SESSIONS:
            oldest = next(iter(self._sessions))
            if oldest == number:
                break
            del self._sessions[oldest]
        return session

    def _call(self, params: dict, caller: str, *, create: bool = True) -> _Call | None:
        """This request's call. None when the call ended (a replayed request can't resume it), or when it isn't
        known and `create` is False (with a PIN, every call must begin at /twilio/voice)."""
        now = self._clock()
        sid = str(params.get("CallSid") or "")[:64]
        for key, c in list(self._calls.items()):  # by start time only, so a pause can't reset the length cap
            if now - c.started > MAX_CALL_SECONDS + 600:
                del self._calls[key]
                self._note_ended(key)
        if not sid or sid in self._ended:
            return None
        call = self._calls.get(sid)
        if call is not None and call.caller != caller:
            return None
        if call is None:
            if not create:
                return None
            call = _Call(sid=sid, caller=caller, started=now)
            self._calls[sid] = call
            while len(self._calls) > MAX_CALLS:
                old, _ = self._calls.popitem(last=False)
                self._note_ended(old)
        return call

    def _note_ended(self, sid: str) -> None:
        self._ended[sid] = None
        self._ended.move_to_end(sid)
        while len(self._ended) > ENDED_CALLS:
            self._ended.popitem(last=False)

    def _end_call(self, call: _Call) -> None:
        self._calls.pop(call.sid, None)
        self._note_ended(call.sid)
        for job_id, job in list(self._jobs.items()):
            if job.call_sid == call.sid:
                job.task.cancel()
                del self._jobs[job_id]

    def _prune_jobs(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if now - job.started > self.job_deadline + 120:
                job.task.cancel()
                del self._jobs[job_id]

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _busy(self) -> bool:
        return sum(1 for t in self._tasks if not t.done()) >= MAX_ACTIVE_TURNS

    def _allow_turn(self, caller: str) -> bool:
        return self._turns.allow(caller) and self._daily.allow(caller)

    # ---------------------------------------------------------------- agent turns
    async def agent_reply(self, caller: str, text: str, channel: str) -> str:
        """Run one restricted turn for `caller` and return the reply text (already free of secrets)."""
        session = self._session(caller)
        async with session.lock:
            session.last = self._clock()
            try:
                reply, new_messages = await self._turn(caller, session.history, text, channel)
            finally:
                session.last = self._clock()
            self._remember(session, text, new_messages, reply)
        settings, _ = self._current()
        return redact(reply, _secret_values(settings, self.cfg))

    async def _voice_reply(self, caller: str, speech: str) -> str:
        """A spoken turn that ends by itself even if nobody polls for it any more (the caller hung up)."""
        try:
            limit = self.job_deadline + self.job_grace
            return await asyncio.wait_for(self.agent_reply(caller, speech, "phone"), limit)
        except asyncio.TimeoutError:
            return TOO_SLOW

    async def _turn(self, caller: str, history: list, text: str, channel: str) -> tuple[str, list | None]:
        settings, ctx = self._current()
        llm = getattr(ctx, "llm", None) or self.llm
        allowed = set(tools_for(channel))
        run_turn = self._get_run_turn()
        done: list | None = None
        error: str | None = None
        blocked = False
        over_budget = False
        try:
            gen = run_turn(
                [*history, {"role": "user", "content": text}], settings, llm, _DeclineBroker(), ctx,
                channel=channel, allowed_tools=allowed,
            )
        except TypeError:
            return FAILED, None  # fail closed: never retry without the tool restriction
        try:
            async for ev in gen:
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("type")
                if kind == "done":
                    done = ev.get("messages") if isinstance(ev.get("messages"), list) else []
                elif kind == "error":
                    error = str(ev.get("text") or "")
                elif kind == "confirm" or (
                    kind == "tool" and ev.get("state") == "start" and ev.get("name") not in allowed
                ):
                    # Second lock: stop the turn here, before that tool can run (the agent should already refuse it).
                    blocked = True
                    log.warning("Phone line stopped a %s turn that tried %s", channel, clip(str(ev.get("name")), 60))
                    break
                elif kind == "tool" and ev.get("state") == "start" and ev.get("name") in WRITE_TOOLS:
                    # Every call of a step is announced before any of them runs, so stopping here stops it.
                    if not self._writes.allow(caller):
                        over_budget = True
                        log.warning("Phone line stopped a %s turn: too many additions from one number", channel)
                        break
        finally:
            with contextlib.suppress(Exception):
                await gen.aclose()
        if blocked:
            return (BLOCKED_SMS if channel == "sms" else BLOCKED), None
        if over_budget:
            return WRITE_LIMITED, None
        reply = final_text(done)
        if reply:
            return reply, done
        if error:
            return f"Sorry, I couldn't finish that: {clip(error, 160)}", None
        return NO_ANSWER, None

    def _remember(self, session: _Session, text: str, new_messages: list | None, reply: str) -> None:
        added: list[dict] = [{"role": "user", "content": text}]
        if new_messages:
            for m in new_messages:
                if not isinstance(m, dict):
                    continue
                m = dict(m)
                if m.get("role") == "tool" and isinstance(m.get("content"), str):
                    m["content"] = clip(m["content"], HISTORY_TOOL_LIMIT)
                added.append(m)
        else:
            added.append({"role": "assistant", "content": reply})
        history = (session.history + added)[-HISTORY_LIMIT:]
        while history and history[0].get("role") != "user":
            history.pop(0)
        session.history = history

    # ---------------------------------------------------------------- TwiML helpers
    def _url(self, path: str) -> str:
        return f"{self.public_url}{path}"

    def _xml(self, *verbs: str) -> Response:
        return Response(twiml(*verbs), media_type="text/xml", headers={"Cache-Control": "no-store"})

    def _listen(self, prompt: str = "") -> Response:
        voice, lang = self.cfg.voice, self.cfg.language
        inner = say(prompt, voice) if prompt else ""
        return self._xml(
            gather(self._url("/twilio/voice/gather"), lang, inner), redirect(self._url("/twilio/voice/gather"))
        )

    def _goodbye(self, text: str) -> Response:
        return self._xml(say(text, self.cfg.voice), hangup())

    def _ask_pin(self, prompt: str) -> Response:
        action = self._url("/twilio/voice/pin")
        return self._xml(keypad(action, say(prompt, self.cfg.voice)), redirect(action))

    def greeting(self, settings) -> str:
        name = getattr(settings, "assistant_name", "") or "your assistant"
        who = getattr(settings, "address_as", "") or getattr(settings, "user_name", "")
        return f"Hello{', ' + who if who else ''}. {name} here. How can I help?"

    async def _answer_or_hold(self, job: _Job, wait: float, call: _Call | None) -> Response:
        remaining = job.started + self.job_deadline - time.monotonic()
        if remaining > 0 and not job.task.done():
            await asyncio.wait({job.task}, timeout=min(wait, remaining))
        if job.task.done():
            self._jobs.pop(job.id, None)
            try:
                reply = job.task.result()
            except (asyncio.CancelledError, Exception):
                reply = FAILED
            if call is not None:
                call.answered = True
            return self._listen(speakable(reply) or NO_ANSWER)
        if time.monotonic() >= job.started + self.job_deadline:
            job.task.cancel()
            self._jobs.pop(job.id, None)
            return self._listen(TOO_SLOW)
        phrase = HOLD_PHRASES[min(job.holds, len(HOLD_PHRASES) - 1)]
        job.holds += 1
        return self._xml(
            say(phrase, self.cfg.voice), pause(2), redirect(self._url(f"/twilio/voice/wait?job={job.id}"))
        )

    # ---------------------------------------------------------------- webhooks
    async def handle_voice(self, request: Request) -> Response:
        params, error = await self._verified(request)
        if error is not None:
            return error
        caller = self._caller(params)
        if caller is None:
            log.warning("Phone line turned away a call from %s", mask_number(str(params.get("From") or "")))
            return self._goodbye(UNKNOWN_CALLER)
        if self._caller_id_failed(params):
            log.warning("Phone line turned away a call whose caller ID failed the carrier's check")
            return self._goodbye(UNKNOWN_CALLER)
        call = self._call(params, caller)
        if call is None:
            return self._xml(hangup())
        if self.cfg.pin and not call.verified:
            if self._pin_fails.blocked("*"):
                self._end_call(call)
                return self._goodbye(PIN_LOCKED)
            return self._ask_pin(PIN_PROMPT)
        settings, _ = self._current()
        return self._listen(self.greeting(settings))

    async def handle_pin(self, request: Request) -> Response:
        params, error = await self._verified(request)
        if error is not None:
            return error
        caller = self._caller(params)
        if caller is None:
            return self._goodbye(UNKNOWN_CALLER)
        call = self._call(params, caller, create=False)
        if call is None or not self.cfg.pin:
            return self._xml(hangup())
        if not call.verified:
            if self._pin_fails.blocked("*"):  # checked before the guess, so a locked line reveals nothing
                self._end_call(call)
                return self._goodbye(PIN_LOCKED)
            digits = str(params.get("Digits") or "").strip()
            call.pin_tries += 1
            if not digits:
                if call.pin_tries >= PIN_ATTEMPTS:
                    self._end_call(call)
                    return self._goodbye(SILENCE_END)
                return self._ask_pin(PIN_PROMPT)
            if not hmac.compare_digest(digits.encode("utf-8", "replace"), self.cfg.pin.encode()):
                self._pin_fails.allow("*")
                log.warning("Phone line: wrong PIN on a call from %s", mask_number(caller))
                if self._pin_fails.blocked("*"):
                    self._end_call(call)
                    return self._goodbye(PIN_LOCKED)
                if call.pin_tries >= PIN_ATTEMPTS:
                    self._end_call(call)
                    return self._goodbye(PIN_FAILED)
                return self._ask_pin(PIN_RETRY)
            call.verified = True
        settings, _ = self._current()
        return self._listen(self.greeting(settings))

    def _live_call(self, params: dict, caller: str) -> _Call | None:
        """The call a gather / wait request belongs to, if it may talk to the assistant."""
        call = self._call(params, caller, create=not self.cfg.pin)
        if call is None or (self.cfg.pin and not call.verified):
            return None
        return call

    async def handle_gather(self, request: Request) -> Response:
        params, error = await self._verified(request)
        if error is not None:
            return error
        caller = self._caller(params)
        if caller is None:
            return self._goodbye(UNKNOWN_CALLER)
        call = self._live_call(params, caller)
        if call is None:
            return self._xml(hangup())
        if self._clock() - call.started > MAX_CALL_SECONDS:
            self._end_call(call)
            return self._goodbye(LONG_CALL)
        speech = _clean_input(params.get("SpeechResult"), SPEECH_INPUT_LIMIT)
        if not speech:
            call.silences += 1
            if call.silences >= 2:
                self._end_call(call)
                return self._goodbye(SILENCE_END)
            return self._listen(REPROMPT_AFTER if call.answered else REPROMPT_FIRST)
        call.silences = 0
        if is_goodbye(speech):
            self._end_call(call)
            return self._goodbye(GOODBYE)
        if not self._allow_turn(caller):
            self._end_call(call)
            return self._goodbye(RATE_LIMITED_VOICE)
        self._prune_jobs()
        if self._busy():
            return self._listen(BUSY)
        for job_id, old in list(self._jobs.items()):  # one question at a time per call
            if old.call_sid == call.sid:
                old.task.cancel()
                del self._jobs[job_id]
        job = _Job(
            id=secrets.token_urlsafe(12), call_sid=call.sid,
            task=self._spawn(self._voice_reply(caller, speech)), started=time.monotonic(),
        )
        self._jobs[job.id] = job
        return await self._answer_or_hold(job, self.first_wait, call)

    async def handle_wait(self, request: Request) -> Response:
        params, error = await self._verified(request)
        if error is not None:
            return error
        caller = self._caller(params)
        if caller is None:
            return self._goodbye(UNKNOWN_CALLER)
        call = self._live_call(params, caller)
        if call is None:
            return self._xml(hangup())
        job = self._jobs.get(request.query_params.get("job", ""))
        if job is None or not hmac.compare_digest(job.call_sid.encode(), call.sid.encode()):
            return self._listen(LOST_JOB)
        return await self._answer_or_hold(job, self.poll_wait, call)

    async def handle_sms(self, request: Request) -> Response:
        params, error = await self._verified(request)
        if error is not None:
            return error
        empty = self._xml()
        caller = self._caller(params)
        if caller is None:
            log.warning("Phone line ignored a text from %s", mask_number(str(params.get("From") or "")))
            return empty
        sid = str(params.get("MessageSid") or params.get("SmsSid") or "")[:64]
        if sid:
            if sid in self._sms_seen:
                return empty
            self._sms_seen[sid] = None
            while len(self._sms_seen) > 500:
                self._sms_seen.popitem(last=False)
        body = _clean_input(params.get("Body"), SMS_INPUT_LIMIT)
        if not body:
            try:
                media = int(params.get("NumMedia") or 0)
            except ValueError:
                media = 0
            return self._xml(message(MEDIA_ONLY)) if media else empty
        if body.upper().strip(" .!") in OPT_KEYWORDS:
            return empty  # Twilio's opt-out handling answers these
        if not self._allow_turn(caller):
            now = self._clock()
            if now - self._limit_noted.get(caller, -TURN_WINDOW) >= TURN_WINDOW:
                self._limit_noted[caller] = now
                return self._xml(message(RATE_LIMITED_SMS))
            return empty
        if self._busy():
            return self._xml(message(BUSY))
        self._spawn(self._sms_reply(caller, body))
        return empty

    async def handle_health(self, request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def _sms_reply(self, caller: str, body: str) -> None:
        try:
            reply = await asyncio.wait_for(self.agent_reply(caller, body, "sms"), self.sms_deadline)
        except asyncio.TimeoutError:
            reply = TOO_SLOW
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Phone line text turn failed (%s)", exc.__class__.__name__)
            reply = FAILED
        await self.send_sms(caller, sms_text(reply))

    async def send_sms(self, to: str, text: str) -> bool:
        """Send `text` from PHONE_NUMBER to `to` with the Twilio Messages API. Only ever an allowed number."""
        if to not in self.cfg.allowed:
            return False
        _, ctx = self._current()
        if getattr(ctx, "http", None) is None:
            log.warning("Phone line can't send a text: no HTTP client")
            return False
        cfg = self.cfg
        try:
            backend = comms.twilio_backend(
                cfg.account_sid, cfg.auth_token, cfg.api_key_sid, cfg.api_key_secret, cfg.number
            )
            await comms._send_twilio(ctx, backend, to, text)
        except ToolError as exc:
            hidden = [cfg.auth_token, cfg.api_key_secret]
            log.warning("Phone line text to %s failed: %s", mask_number(to), redact(str(exc), hidden))
            return False
        except Exception as exc:  # never let an odd failure escape a background task with request details
            log.warning("Phone line text to %s failed (%s)", mask_number(to), exc.__class__.__name__)
            return False
        return True


def create_app(line: PhoneLine) -> FastAPI:
    """The internet-facing app: Twilio webhooks only. No docs, no OpenAPI schema, nothing from the main app."""
    app = FastAPI(title="phone line", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_api_route("/twilio/voice", line.handle_voice, methods=["POST"], include_in_schema=False)
    app.add_api_route("/twilio/voice/pin", line.handle_pin, methods=["POST"], include_in_schema=False)
    app.add_api_route("/twilio/voice/gather", line.handle_gather, methods=["POST"], include_in_schema=False)
    app.add_api_route("/twilio/voice/wait", line.handle_wait, methods=["POST"], include_in_schema=False)
    app.add_api_route("/twilio/sms", line.handle_sms, methods=["POST"], include_in_schema=False)
    app.add_api_route("/twilio/health", line.handle_health, methods=["GET"], include_in_schema=False)
    return app


# ------------------------------------------------------------------ module API (one line per ATLAS process)
LINE = PhoneLine()
phone_app = LINE.app


async def start(settings, llm, ctx_factory) -> dict:
    """Start (or restart) the phone line. ctx_factory() -> ToolContext (its .http reaches Twilio, its .settings and
    .llm are used for each turn). Returns status(); problems are reported there, never raised."""
    return await LINE.start(settings, llm, ctx_factory)


async def stop() -> None:
    """Stop the webhook server and tunnel and forget all phone sessions. Safe to call any time."""
    await LINE.stop()


def status() -> dict:
    """{"enabled", "number", "status": "off"|"starting"|"online"|"error", "public_url", "error"}."""
    return LINE.status()
