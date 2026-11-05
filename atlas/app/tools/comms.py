"""Texting and phone calls on the user's behalf.

* send_text_message: SMS through Twilio when it is configured, otherwise the Messages app (iMessage) on macOS.
* make_phone_call: opens the computer's calling app on a tel: link (Phone Link/Teams/Skype on Windows,
  FaceTime-through-iPhone on macOS, the desktop's tel: handler on Linux). The user finishes the call there.

Both act in the user's name, so both are listed in config.SENSITIVE_TOOLS: every text and call is shown on an
Approve/Deny card first, with the resolved number, the backend and the full message. As a second lock, the tools
refuse to run unless their approval preview was rendered for exactly these arguments (or the user put the tool in
AUTO_APPROVE), so a missing SENSITIVE_TOOLS entry fails closed instead of texting unasked.

Settings (environment or .env): TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, DEFAULT_COUNTRY_CODE, and
optionally TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET. Every Twilio REST request authenticates with the API key when
both halves are set (a revocable credential the user can make in the Twilio Console), else with the Account SID and
Auth Token. The Account SID is always the account in the URL. (Checking Twilio's webhook signatures, which the phone
line does, needs the Auth Token: an API key can't do that.)
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import json
import shutil
import subprocess
import time

import httpx

from ..config import load_env_file
from . import ToolError, tool
from .memory import contacts, find_contact

SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux" (tests patch this)

TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
TWILIO_API_KEY_KEYS = ("TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET")  # optional: used instead of the Auth Token
ACCOUNT_SID_FORMAT = re.compile(r"AC[0-9a-fA-F]{32}")
API_KEY_SID_FORMAT = re.compile(r"SK[0-9a-fA-F]{32}")
TWILIO_MAX_BODY = 1600  # Twilio's documented limit for a message body

TEXT_SETUP_HELP = (
    "Texting isn't set up yet. To send texts from this computer, create a Twilio account (twilio.com — a free "
    "trial can text numbers you verify), get a Twilio phone number, then add TWILIO_ACCOUNT_SID, "
    "TWILIO_AUTH_TOKEN (both on the Twilio Console home page) and TWILIO_FROM_NUMBER in ATLAS Settings. "
    "Instead of the Auth Token you can use an API key (TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET). "
    "On a Mac, ATLAS can also send iMessages through the Messages app with no setup."
)

# Twilio REST error codes that have a clearer explanation than Twilio's own message.
TWILIO_ERRORS = {
    21211: "Twilio says {to} isn't a valid phone number.",
    21408: (
        "Your Twilio account isn't allowed to text that country yet. Enable it in the Twilio Console under "
        "Messaging → Settings → Geo permissions."
    ),
    21606: "TWILIO_FROM_NUMBER isn't a text-capable number on your Twilio account for this destination.",
    21608: (
        "{to} isn't verified on your Twilio trial account. Trial accounts can only text verified numbers: add it "
        "in the Twilio Console under Phone Numbers → Manage → Verified Caller IDs, or upgrade the account."
    ),
    21610: "{to} has replied STOP to your Twilio number, so Twilio won't deliver texts there until they reply START.",
    21614: "{to} isn't a mobile number that can receive texts.",
}


def _setting(key: str, default: str = "") -> str:
    return str(os.environ.get(key) or load_env_file().get(key) or default).strip()


# ------------------------------------------------------------------ approval interlock
# The agent renders a tool's preview (the Approve/Deny card) before running any tool in SENSITIVE_TOOLS, and runs it
# only once the user approves. Remember which argument sets were previewed so the tools can refuse a call that
# skipped the card.
APPROVAL_TTL = 900.0
_PREVIEWED: dict[str, float] = {}


def _approval_key(name: str, *values) -> str:
    return json.dumps([name, *values], sort_keys=True, default=str)


def _mark_previewed(key: str) -> None:
    now = time.monotonic()
    for k, t in list(_PREVIEWED.items()):
        if now - t > APPROVAL_TTL:
            del _PREVIEWED[k]
    _PREVIEWED[key] = now
    while len(_PREVIEWED) > 500:
        _PREVIEWED.pop(next(iter(_PREVIEWED)))


def _require_approval(ctx, name: str, key: str, what: str) -> None:
    settings = getattr(ctx, "settings", None)
    if name in (getattr(settings, "auto_approve", None) or set()):
        return  # the user chose to skip the card for this tool
    t = _PREVIEWED.get(key)
    if t is None or time.monotonic() - t > APPROVAL_TTL:
        raise ToolError(
            f"Not done. {what} must be approved by the user on an Approve/Deny card first, and this request didn't go "
            "through one."
        )


# ------------------------------------------------------------------ phone numbers
_URI_PREFIX = re.compile(r"^\s*(?:tel|sms|callto):", re.I)
_EXTENSION = re.compile(r"\s*(?:,+|;\s*ext=|\bext\.?|\bextension|x)\s*(\d{1,6})\s*$", re.I)
_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-/]+$")
# National trunk prefix dropped before adding the country code. Most countries use "0"; Italy and San
# Marino keep their leading 0, Russia/Kazakhstan dial 8, Hungary 06.
_TRUNK_PREFIX = {"7": "8", "36": "06", "39": "", "378": ""}


def _split_extension(raw: str) -> tuple[str, str]:
    """('+1 555 234 5678', '12') for '+1 555 234 5678 ext. 12'. Also drops tel:/sms: prefixes."""
    text = _URI_PREFIX.sub("", str(raw or "")).strip()
    text = re.sub(r"^\(\s*\+", "+(", text)  # "(+44) 20 ..." -> "+(44) 20 ..."
    m = _EXTENSION.search(text)
    if m and _PHONE_CHARS.match(text[: m.start()].strip() or "x"):
        return text[: m.start()].strip(), m.group(1)
    return text, ""


def looks_like_phone(text: str) -> bool:
    number, _ = _split_extension(text)
    return bool(_PHONE_CHARS.match(number)) and sum(ch.isdigit() for ch in number) >= 3


def country_code(value: str | None = None) -> str:
    """Digits of DEFAULT_COUNTRY_CODE (default +1), e.g. '1' or '44'."""
    raw = _setting("DEFAULT_COUNTRY_CODE", "+1") if value is None else str(value).strip()
    digits = re.sub(r"[\s()+\-]", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if not re.fullmatch(r"[1-9]\d{0,2}", digits):
        raise ToolError(
            f"DEFAULT_COUNTRY_CODE '{raw}' isn't a country calling code. Set it in Settings to something like "
            "+1 (US/Canada), +44 (UK) or +61 (Australia)."
        )
    return digits


def normalize_phone(raw: str, default_country: str | None = None) -> str:
    """Return the number in E.164 form ('+15552345678'). Numbers without '+' (or 00/011) are national
    numbers of DEFAULT_COUNTRY_CODE. Raises ToolError with a reason the user can act on."""
    cc = country_code(default_country)
    number, _ext = _split_extension(raw)
    if not number or not _PHONE_CHARS.match(number):
        raise ToolError(f"'{raw}' doesn't look like a phone number.")
    if number.startswith("+") and not re.sub(r"\D", "", number).startswith(("39", "378")):
        number = re.sub(r"\(\s*0\s*\)", " ", number)  # "+44 (0)20 ..." -> the (0) is only dialled nationally
    digits = re.sub(r"\D", "", number)
    international = number.startswith("+")
    if not international and digits.startswith("00"):
        digits, international = digits[2:], True
    elif not international and cc == "1" and digits.startswith("011"):
        digits, international = digits[3:], True  # NANP international dialling prefix

    if not international:
        if len(digits) <= 6:
            raise ToolError(
                f"'{raw}' is a short or service number (like an emergency number or an SMS short code). I can only "
                "text or call full phone numbers. For emergencies, dial directly from your phone."
            )
        if cc == "1":
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 7:
                raise ToolError(f"'{raw}' is missing its area code. Give the full 10-digit number.")
            if len(digits) != 10:
                raise ToolError(
                    f"'{raw}' isn't a 10-digit US/Canada number. For other countries, start with + and the "
                    "country code (e.g. +44 20 7946 0958)."
                )
        else:
            trunk = _TRUNK_PREFIX.get(cc, "0")
            if trunk and digits.startswith(trunk):
                digits = digits[len(trunk):]
        digits = cc + digits

    if not digits or digits[0] == "0":
        raise ToolError(f"'{raw}' has no valid country code after the +.")
    if not 7 <= len(digits) <= 15:
        raise ToolError(f"'{raw}' is too {'short' if len(digits) < 7 else 'long'} to be a full international number.")
    if digits[0] == "1":  # North American Numbering Plan: +1 NXX NXX XXXX
        if len(digits) != 11:
            raise ToolError(f"'{raw}' should have 10 digits after +1.")
        if digits[1] in "01" or digits[4] in "01":
            raise ToolError(f"'{raw}' isn't a valid US/Canada number (area codes and exchanges can't start with 0 or 1).")
        if digits[2:4] == "11":
            raise ToolError(
                f"'{raw}' isn't a valid US/Canada number: {digits[1:4]} is a service code (like 911), not an area code. "
                "For emergencies, dial directly from your phone."
            )
    return "+" + digits


def pretty_phone(e164: str) -> str:
    d = e164.lstrip("+")
    if len(d) == 11 and d[0] == "1":
        return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:]}"
    return e164


def resolve_recipient(to: str) -> dict:
    """Turn a phone number or saved contact name into {'name', 'number' (E.164), 'extension', 'display'}."""
    if isinstance(to, (list, tuple)):
        if len(to) != 1:
            raise ToolError("Give one recipient at a time: send one text or place one call per person.")
        to = to[0]
    text = str(to or "").strip()
    if not text:
        raise ToolError("Who should I contact? Give a phone number or the name of a saved contact.")
    if looks_like_phone(text):
        number = normalize_phone(text)
        return {"name": None, "number": number, "extension": _split_extension(text)[1], "display": pretty_phone(number)}
    contact = find_contact(text)
    if contact is None:
        q = text.lower()
        similar = [
            c.get("name", "") for c in contacts()
            if q in str(c.get("name", "")).lower() or any(q in str(a).lower() for a in c.get("aliases") or [])
        ]
        if len(similar) > 1:
            raise ToolError(f"Several saved contacts match '{text}': {', '.join(similar[:6])}. Which one?")
        raise ToolError(
            f"I don't have a saved contact called '{text}'. Give me their number, or save them with save_contact "
            "(include their phone number)."
        )
    name = contact.get("name") or text
    phone = str(contact.get("phone") or "").strip()
    if not phone:
        raise ToolError(
            f"{name} is saved but has no phone number. Ask for it and add it with save_contact "
            "(same name and email, plus phone), or give me the number directly."
        )
    try:
        number = normalize_phone(phone)
    except ToolError as exc:
        raise ToolError(f"The number saved for {name} ('{phone}') can't be used: {exc}") from None
    return {"name": name, "number": number, "extension": _split_extension(phone)[1], "display": pretty_phone(number)}


def _clean_message(message) -> str:
    """The text as it will be sent: control characters (NUL etc., which can't be passed to osascript) removed."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(message or "")).strip()


# ------------------------------------------------------------------ Twilio credentials
def account_sid_problem(value: str) -> str | None:
    """Why `value` can't be TWILIO_ACCOUNT_SID, in words the user can act on, or None when it's fine."""
    v = str(value or "").strip()
    if ACCOUNT_SID_FORMAT.fullmatch(v):
        return None
    if v[:2].upper() == "SK":
        return (
            "That looks like an API Key SID — put it in API Key SID; the Account SID starts with AC. Your Account SID "
            "is on the Twilio Console home page."
        )
    return (
        "TWILIO_ACCOUNT_SID should be the 34-character Account SID that starts with 'AC' "
        "(shown on the Twilio Console home page)."
    )


def api_key_sid_problem(value: str) -> str | None:
    """Why `value` can't be TWILIO_API_KEY_SID, or None when it's fine."""
    v = str(value or "").strip()
    if API_KEY_SID_FORMAT.fullmatch(v):
        return None
    if v[:2].upper() == "AC":
        return (
            "That looks like your Account SID — put it in Account SID; an API Key SID starts with SK. Your API keys "
            "are under Account → API keys & tokens in the Twilio Console."
        )
    return (
        "TWILIO_API_KEY_SID should be the 34-character API Key SID that starts with 'SK' (Twilio Console → "
        "Account → API keys & tokens)."
    )


def twilio_auth(
    account_sid: str, auth_token: str = "", api_key_sid: str = "", api_key_secret: str = ""
) -> tuple[str, str]:
    """The (username, password) for basic auth on Twilio's REST API: (API Key SID, API Key Secret) when both are set,
    else (Account SID, Auth Token). The Account SID is still the account in every request's URL, so it's required
    either way. Raises ToolError with a fix the user can act on; the message never contains a secret."""
    account_sid, auth_token = str(account_sid or "").strip(), str(auth_token or "").strip()
    api_key_sid, api_key_secret = str(api_key_sid or "").strip(), str(api_key_secret or "").strip()
    if not account_sid:
        raise ToolError(
            "Add TWILIO_ACCOUNT_SID in ATLAS Settings: Twilio needs the Account SID (starts with AC, on the Twilio "
            "Console home page) in every request, even with an API key."
        )
    problem = account_sid_problem(account_sid)
    if problem:
        raise ToolError(problem)
    if api_key_sid and api_key_secret:
        problem = api_key_sid_problem(api_key_sid)
        if problem:
            raise ToolError(problem)
        return api_key_sid, api_key_secret
    if auth_token:
        return account_sid, auth_token
    if api_key_sid:
        raise ToolError(
            "TWILIO_API_KEY_SECRET is missing: add the secret Twilio showed when you created the API key (or use "
            "TWILIO_AUTH_TOKEN instead)."
        )
    if api_key_secret:
        raise ToolError("TWILIO_API_KEY_SID is missing: add the API key's SID (starts with SK) next to its secret.")
    raise ToolError(
        "Add TWILIO_AUTH_TOKEN (Twilio Console home page), or an API key (TWILIO_API_KEY_SID and "
        "TWILIO_API_KEY_SECRET), in ATLAS Settings."
    )


def uses_api_key(auth: tuple[str, str]) -> bool:
    return str(auth[0]).startswith("SK")


def credential_names(auth: tuple[str, str]) -> str:
    """The settings to check when Twilio rejects these credentials."""
    if uses_api_key(auth):
        return "TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET (the key must belong to the TWILIO_ACCOUNT_SID account)"
    return "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN (both on the Twilio Console home page)"


def twilio_backend(account_sid: str, auth_token: str, api_key_sid: str, api_key_secret: str, sender: str) -> dict:
    """A Twilio texting backend for _send_twilio. Raises ToolError when the credentials can't be used."""
    auth = twilio_auth(account_sid, auth_token, api_key_sid, api_key_secret)
    return {
        "name": "twilio",
        "sid": str(account_sid).strip(),
        "auth": auth,
        "from": str(sender or "").strip(),
        "label": f"Twilio SMS from {str(sender or '').strip()}",
    }


# ------------------------------------------------------------------ texting backends
def text_backend() -> dict:
    """Which service will carry a text: Twilio when fully configured, else Messages on macOS."""
    values = {k: _setting(k) for k in TWILIO_KEYS + TWILIO_API_KEY_KEYS}
    sid, token, sender = (values[k] for k in TWILIO_KEYS)
    key_sid, key_secret = (values[k] for k in TWILIO_API_KEY_KEYS)
    if sid and sender and (token or (key_sid and key_secret)):
        return twilio_backend(sid, token, key_sid, key_secret, sender)
    if any(values.values()):
        missing = [] if sid else ["TWILIO_ACCOUNT_SID"]
        hint = ""
        if not token and not (key_sid and key_secret):
            if key_sid:
                missing.append("TWILIO_API_KEY_SECRET")
            elif key_secret:
                missing.append("TWILIO_API_KEY_SID")
            else:
                missing.append("TWILIO_AUTH_TOKEN")
                hint = " (Or, instead of the Auth Token, an API key: TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET.)"
        if not sender:
            missing.append("TWILIO_FROM_NUMBER")
        raise ToolError(f"Twilio is only partly set up: add {', '.join(missing)} in ATLAS Settings.{hint}")
    if SYSTEM == "Darwin":
        return {"name": "messages", "label": "Messages app on this Mac (iMessage)"}
    raise ToolError(TEXT_SETUP_HELP)


def _twilio_sender(value: str) -> tuple[str, str]:
    """The form field and value that identify the sender: a number, a Messaging Service, or a sender ID."""
    if re.fullmatch(r"MG[0-9a-fA-F]{32}", value):
        return "MessagingServiceSid", value
    if re.fullmatch(r"\d{5,6}", value):
        return "From", value  # an SMS short code on the Twilio account
    if looks_like_phone(value):
        return "From", normalize_phone(value)
    if re.fullmatch(r"[A-Za-z0-9 ]{1,11}", value) and re.search(r"[A-Za-z]", value):
        return "From", value  # alphanumeric sender ID (supported in some countries)
    raise ToolError(f"TWILIO_FROM_NUMBER '{value}' should be your Twilio phone number, e.g. +15552345678.")


async def _send_twilio(ctx, backend: dict, number: str, message: str) -> dict:
    """POST one text to Twilio's Messages API. backend: {"sid": Account SID, "auth": (user, password), "from"}."""
    sid, auth = str(backend.get("sid") or "").strip(), tuple(backend["auth"])
    problem = account_sid_problem(sid)  # checked here too: the SID goes into the URL
    if problem:
        raise ToolError(problem)
    field, sender = _twilio_sender(backend["from"])
    try:
        r = await ctx.http.post(
            TWILIO_MESSAGES_URL.format(sid=sid),
            data={"To": number, field: sender, "Body": message},
            auth=auth,
            timeout=30,
        )
    except httpx.TimeoutException:
        raise ToolError("Twilio didn't answer in time, so I can't confirm the text went out. Check before resending.") from None
    except httpx.TransportError as exc:
        raise ToolError(f"Couldn't reach Twilio ({exc.__class__.__name__}). Check the internet connection.") from None
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if r.status_code == 401:
        raise ToolError(f"Twilio rejected the credentials (401). Check {credential_names(auth)} in Settings.")
    if r.status_code >= 400:
        try:
            code = int(payload.get("code"))
        except (TypeError, ValueError):
            code = None
        if code in TWILIO_ERRORS:
            raise ToolError(TWILIO_ERRORS[code].format(to=pretty_phone(number)) + f" (Twilio error {code})")
        if r.status_code == 403 and uses_api_key(auth):
            raise ToolError(
                "Twilio refused the API key (403). A restricted key needs permission to send messages; a standard "
                "key works. Check TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET in Settings."
            )
        if r.status_code == 404:
            also = " (the API key must be one made in that account)" if uses_api_key(auth) else ""
            raise ToolError(f"Twilio couldn't find that account. Check TWILIO_ACCOUNT_SID in Settings{also}.")
        detail = str(payload.get("message") or r.text or "").strip()[:300]
        tag = f"{r.status_code}, error {code}" if code else str(r.status_code)
        raise ToolError(f"Twilio couldn't send the text ({tag}): {detail or 'no details given'}")
    return {"sid": payload.get("sid"), "twilio_status": payload.get("status")}


# The recipient and message reach the script as argv of its run handler, never inside the script text, so
# quotes, backslashes, newlines or AppleScript in a message can't change what runs. The number (always "+...")
# is the first argument, so osascript stops option parsing before a message that happens to start with "-".
_MESSAGES_SCRIPT = (
    "on run argv",
    "set targetNumber to item 1 of argv",
    "set messageText to item 2 of argv",
    'tell application "Messages"',
    "set targetService to 1st account whose service type = iMessage",  # macOS 11+ dictionary
    "set targetBuddy to participant targetNumber of targetService",
    "send messageText to targetBuddy",
    "end tell",
    "end run",
)
_MESSAGES_SCRIPT_LEGACY = tuple(  # macOS 10.15 and older call these "service" and "buddy"
    line.replace("1st account", "1st service").replace("participant targetNumber", "buddy targetNumber")
    for line in _MESSAGES_SCRIPT
)


def _osascript_args(lines, *argv: str) -> list[str]:
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    return args + list(argv)


def _messages_error(stderr: str) -> str:
    if "-1743" in stderr or "Not authorized" in stderr:
        return (
            "macOS blocked ATLAS from controlling Messages. Allow it in System Settings → Privacy & Security → "
            "Automation (turn on Messages for the app running ATLAS), then try again."
        )
    if "-1728" in stderr or "Can't get" in stderr or "Can’t get" in stderr:
        return (
            "Messages couldn't find an iMessage account or that recipient. Make sure Messages is signed in to "
            "iMessage (Messages → Settings → iMessage). People without iMessage (e.g. Android phones) need Twilio."
        )
    return f"Messages couldn't send the text: {stderr or 'unknown AppleScript error'}"


def _send_messages_app(number: str, message: str) -> None:
    stderr = ""
    for script in (_MESSAGES_SCRIPT, _MESSAGES_SCRIPT_LEGACY):
        try:
            res = subprocess.run(
                _osascript_args(script, number, message),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
        except FileNotFoundError:
            raise ToolError("osascript isn't available, so I can't use the Messages app on this computer.") from None
        except OSError as exc:  # e.g. a message too long for the command line
            raise ToolError(f"Couldn't start the Messages app script ({exc.strerror or exc.__class__.__name__}).") from None
        except subprocess.TimeoutExpired:
            raise ToolError("Messages didn't respond within 30 seconds. Check the Messages app before resending.") from None
        if res.returncode == 0:
            return
        stderr = (res.stderr or res.stdout or "").strip()
        # A compile error means this macOS uses the older dictionary; nothing was sent, so try that instead.
        if not ("syntax error" in stderr.lower() or "-2741" in stderr or "-2740" in stderr):
            break
    raise ToolError(_messages_error(stderr))


def _text_preview(args: dict) -> dict:
    to = args.get("to")
    fields = [["To", str(to or "")]]
    resolved = None
    try:
        r = resolve_recipient(to)
        resolved = r["number"]
        number = r["number"] if r["display"] == r["number"] else f"{r['display']} ({r['number']})"
        fields = [["To", r["name"] or r["display"]], ["Number", number]]
    except ToolError as exc:
        fields.append(["Number", f"Can't resolve: {exc}"])
    # The approval covers this exact message to this exact number: if the contact changes before it runs, it's refused.
    _mark_previewed(_approval_key("send_text_message", to, args.get("message"), resolved))
    try:
        fields.append(["Send via", text_backend()["label"]])
    except ToolError as exc:
        fields.append(["Send via", f"Not available: {exc}"])
    return {"title": "Send text message", "fields": fields, "body": _clean_message(args.get("message"))}


@tool(
    "send_text_message",
    "Send a text message (SMS) to a phone number or to a saved contact by name (e.g. 'Mom'). Uses Twilio if "
    "configured, otherwise iMessage via the Messages app on a Mac. Write the complete, final message in the user's "
    "voice; the user sees it on an approval card before it goes out, so don't ask 'shall I send it?' first. "
    "Numbers without a leading + are read as national numbers of the user's default country. If the contact has "
    "no phone number saved, ask for it. Use send_email for email, not this.",
    {
        "to": {"type": "string", "description": "Phone number (e.g. '+44 7911 123456', '(555) 234-5678') or saved contact name."},
        "message": {"type": "string", "description": "The full text to send."},
    },
    ["to", "message"],
    label="Sending text message",
    preview=_text_preview,
    timeout=45,
)
async def send_text_message(ctx, to: str, message: str):
    raw_message = message
    message = _clean_message(message)
    if not message:
        raise ToolError("The message is empty.")
    r = resolve_recipient(to)
    _require_approval(ctx, "send_text_message", _approval_key("send_text_message", to, raw_message, r["number"]), "Texts")
    if r["extension"]:
        raise ToolError(f"{r['display']} has an extension, so it's probably a landline that can't receive texts.")
    backend = text_backend()
    who = r["name"] or r["display"]
    if backend["name"] == "twilio":
        if len(message) > TWILIO_MAX_BODY:
            raise ToolError(
                f"That message is {len(message)} characters; Twilio's limit is {TWILIO_MAX_BODY}. Shorten it or split it."
            )
        info = await _send_twilio(ctx, backend, r["number"], message)
        return {
            "status": f"Text to {who} accepted by Twilio",
            "backend": "twilio",
            "recipient": who,
            "to": r["number"],
            "sid": info["sid"],
            "twilio_status": info["twilio_status"],
            "note": "Twilio queues the text and the carrier usually delivers it within seconds.",
        }
    await asyncio.to_thread(_send_messages_app, r["number"], message)
    return {
        "status": f"Text to {who} sent via Messages",
        "backend": "messages",
        "recipient": who,
        "to": r["number"],
        "note": (
            "Handed to the Messages app as an iMessage. If the recipient doesn't use iMessage, Messages will mark "
            "it 'Not Delivered'."
        ),
    }


# ------------------------------------------------------------------ phone calls
def _calling_app() -> str:
    if SYSTEM == "Windows":
        return "your default calling app (Phone Link, Teams or Skype)"
    if SYSTEM == "Darwin":
        return "FaceTime (calls go through your iPhone)"
    return "your desktop's default phone (tel:) app"


def _open_tel(uri: str) -> None:
    if SYSTEM == "Windows":
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except OSError:
            raise ToolError(
                "No app on this PC is set up to place calls. Link your phone in Phone Link (or install Teams or "
                "Skype) and choose it for TEL links in Settings → Apps → Default apps."
            ) from None
        return
    if SYSTEM == "Darwin":
        try:
            res = subprocess.run(["open", uri], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"macOS couldn't open the calling app: {exc}") from None
        if res.returncode != 0:
            raise ToolError(
                "macOS couldn't open a calling app. To call through your iPhone, turn on 'Calls from iPhone' in "
                "FaceTime settings on the Mac and 'Calls on Other Devices' on the iPhone."
            )
        return
    opener = shutil.which("xdg-open")
    if not opener:
        raise ToolError("This computer has no xdg-open, so I can't hand the number to a calling app.")
    try:
        proc = subprocess.Popen(
            [opener, uri], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise ToolError(f"Couldn't run xdg-open to hand the number to a calling app ({exc.strerror or exc}).") from None
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return  # the handler is running in the foreground, so it did open
    if code != 0:
        raise ToolError(
            "No app on this computer handles phone (tel:) links. Install a calling app (e.g. GNOME Calls or a "
            "softphone), or call from your phone."
        )


def _call_preview(args: dict) -> dict:
    to = args.get("to")
    fields = [["Call", str(to or "")]]
    resolved = None
    try:
        r = resolve_recipient(to)
        resolved = r["number"]
        fields = [["Call", r["name"] or r["display"]], ["Number", r["display"]]]
        if r["extension"]:
            fields.append(["Extension", r["extension"]])
    except ToolError as exc:
        fields.append(["Number", f"Can't resolve: {exc}"])
    _mark_previewed(_approval_key("make_phone_call", to, resolved))
    fields.append(["Opens", _calling_app()])
    return {
        "title": "Place a phone call",
        "fields": fields,
        "body": "ATLAS opens your calling app with this number. You press Call and talk on your own device.",
    }


@tool(
    "make_phone_call",
    "Start a phone call to a number or a saved contact by name: opens the computer's calling app (Phone Link, Teams "
    "or Skype on Windows; FaceTime through the iPhone on a Mac) with the number filled in, and the user completes "
    "the call on their device. ATLAS can't speak or listen on the call itself. The user approves it on a card, so "
    "don't ask first. Don't use it for emergency numbers: tell the user to dial those directly.",
    {"to": {"type": "string", "description": "Phone number or saved contact name."}},
    ["to"],
    label="Placing call",
    preview=_call_preview,
    timeout=30,
)
async def make_phone_call(ctx, to: str):
    r = resolve_recipient(to)
    _require_approval(ctx, "make_phone_call", _approval_key("make_phone_call", to, r["number"]), "Calls")
    uri = "tel:" + r["number"]
    await asyncio.to_thread(_open_tel, uri)
    who = r["name"] or r["display"]
    app = _calling_app()
    note = f"The call isn't connected yet. {app[0].upper() + app[1:]} should now show {r['display']}: press Call there to complete it."
    if r["extension"]:
        note += f" Once it connects, dial extension {r['extension']}."
    return {"status": f"Opened a call to {who}", "opened": uri, "recipient": who, "number": r["number"], "app": app, "note": note}
