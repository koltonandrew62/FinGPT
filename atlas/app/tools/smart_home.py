"""Smart-home control through the Home Assistant REST API (/api/states and /api/services/<domain>/<service>).

Settings (environment or .env):
  HOME_ASSISTANT_URL     e.g. http://homeassistant.local:8123 (a local-network address is normal here)
  HOME_ASSISTANT_TOKEN   a long-lived access token (HA profile → Security → Long-lived access tokens)
  HOME_ASSISTANT_CONFIRM_DOMAINS  optional comma list of extra domains, domain.service pairs or entity_ids that
                         must be approved first ("*" approves everything)

Routine actions (lights, switches, media, climate, scenes) run at once. Everything else goes through the tool's
approval gate, so the user sees an Approve/Deny card first: security-relevant devices (locks, alarm panels, covers such
as garage doors, valves, sirens, cameras), any unlock/disarm/open service, relays, buttons, scripts and scenes whose
name or members point at a door, gate, lock, alarm or valve (looked up live in Home Assistant), "all" targets outside a
few harmless domains, any domain that isn't everyday device control (notify, automation, update, shell_command, ...)
and the configured extras.

smart_home_control also re-checks for itself: a call the gate would put on a card is refused unless the gate was
consulted for exactly that call, so a missing gate wiring fails closed instead of unlocking a door unasked.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

import httpx

from ..config import load_env_file
from . import ToolError, tool

TOKEN_HELP = (
    "In Home Assistant, open your profile (click your name at the bottom left), go to the Security tab, and under "
    "Long-lived access tokens choose Create token. Paste it into ATLAS Settings as HOME_ASSISTANT_TOKEN."
)
SETUP_HELP = (
    "Smart-home control isn't set up yet. In ATLAS Settings add HOME_ASSISTANT_URL (your Home Assistant address, "
    "e.g. http://homeassistant.local:8123) and HOME_ASSISTANT_TOKEN. " + TOKEN_HELP
)

_NAME = re.compile(r"^[a-z0-9_]+$")

# Domains people usually mean by "devices"; listed first and shown when no filter is given.
CONTROL_DOMAINS = (
    "light", "switch", "fan", "climate", "cover", "lock", "media_player", "scene", "script", "alarm_control_panel",
    "vacuum", "humidifier", "water_heater", "valve", "siren", "camera", "input_boolean", "button",
)
SECURITY_DOMAINS = {
    "lock": "locks",
    "alarm_control_panel": "alarm systems",
    "cover": "doors, garage doors and other covers",
    "valve": "valves (water or gas)",
    "siren": "sirens",
    "camera": "cameras",
}
SAFE_SECURITY_SERVICES = {"cover": {"stop_cover", "stop_cover_tilt"}, "valve": {"stop_valve"}}
# Everyday device control: these domains run without a card (subject to the checks below). Anything else, e.g.
# notify (messages people), automation, update (firmware), shell_command, recorder or group, asks first.
ROUTINE_DOMAINS = {
    "light", "switch", "fan", "climate", "media_player", "scene", "script", "input_boolean", "input_number",
    "input_select", "input_text", "input_datetime", "input_button", "button", "vacuum", "lawn_mower", "humidifier",
    "water_heater", "remote", "number", "select", "counter", "timer", "todo", "homeassistant",
}
# entity_id "all" is only harmless where no member could be a door relay, gate button or security script.
SAFE_ALL_DOMAINS = {"light", "media_player", "fan", "climate", "humidifier", "vacuum"}
DOMAIN_REASONS = {
    "notify": "it sends a message or notification",
    "tts": "it speaks out loud on a speaker",
    "automation": "it changes or runs an automation",
    "update": "it installs or skips a software update",
    "group": "it controls a group, which can include locks, doors or covers",
    "device_tracker": "it changes someone's tracked location",
    "shell_command": "it runs a command on the Home Assistant machine",
    "rest_command": "it sends a web request from Home Assistant",
    "python_script": "it runs a script on Home Assistant",
    "recorder": "it changes Home Assistant's history",
    "backup": "it manages Home Assistant backups",
}
RISKY_SERVICE_WORDS = {
    "unlock": "it unlocks something",
    "disarm": "it disarms an alarm",
    "open": "it opens something (a door, gate, lock, valve or cover)",
}
ADMIN_DOMAINS = {"homeassistant", "hassio"}  # restart/stop/reload Home Assistant itself
GENERIC_SERVICES = {"turn_on", "turn_off", "toggle", "update_entity"}
# Garage doors, gates, door strikes and water/gas valves are often wired as plain relays, buttons, scripts or
# helpers rather than covers or locks, so these domains are gated when the entity's id, its friendly name (looked
# up live) or the request data says what they drive. Scenes are also checked for security devices among their members.
NAME_CHECK_DOMAINS = {
    "switch", "button", "input_button", "script", "scene", "input_boolean", "input_number", "input_select",
    "input_text", "number", "select", "remote",
}
RELAY_WORDS = {
    "garage", "gate", "door", "opener", "open", "lock", "unlock", "alarm", "disarm", "security", "valve", "gas",
    "buzzer", "intercom", "latch", "deadbolt", "siren", "camera",
}
# Buttons and helpers that can wipe or reflash a device.
DESTRUCTIVE_WORDS = {"factory", "firmware", "wipe", "erase"}
SCRIPT_SERVICES = {"turn_on", "turn_off", "toggle", "reload"}  # any other script.<x> service runs script <x>
KNOWN_DOMAINS = ROUTINE_DOMAINS | set(SECURITY_DOMAINS) | set(DOMAIN_REASONS) | {"alarm_control_panel", "person"}
# Targets other than entity_id would widen a call beyond what the user approved; only entity_id is allowed.
TARGET_KEYS = {"device_id", "area_id", "floor_id", "label_id", "target"}
SECRET_KEYS = {"code", "pin", "password", "passcode", "token", "api_key"}

ATTRIBUTES = (
    "brightness", "temperature", "current_temperature", "target_temp_low", "target_temp_high", "hvac_mode",
    "hvac_modes", "hvac_action", "preset_mode", "fan_mode", "percentage", "current_position", "media_title",
    "media_artist", "volume_level", "source", "battery_level", "battery", "device_class",
)
_STOPWORDS = {"the", "my", "a", "an", "all", "in", "of", "and", "at", "to", "is", "are"}


def _setting(key: str, default: str = "") -> str:
    return str(os.environ.get(key) or load_env_file().get(key) or default).strip()


def _ha_config() -> tuple[str, str]:
    url, token = _setting("HOME_ASSISTANT_URL"), _setting("HOME_ASSISTANT_TOKEN")
    if not url or not token:
        raise ToolError(SETUP_HELP)
    if "://" not in url:
        url = "http://" + url
    try:
        parsed = urlparse(url)
        ok = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
    except ValueError:  # e.g. a malformed or out-of-range port, or a bad IPv6 literal
        ok = False
    if not ok:
        raise ToolError(f"HOME_ASSISTANT_URL '{url}' isn't a valid address. Use something like http://homeassistant.local:8123.")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise ToolError("HOME_ASSISTANT_TOKEN has characters a Home Assistant token can't contain. Copy it again. " + TOKEN_HELP)
    # Home Assistant isn't served under a sub-path, so anything after the host (/api, /lovelace/0) is dropped.
    return f"{parsed.scheme}://{parsed.netloc}", token


def _display(base: str) -> str:
    """The address for messages, without any user:password@ part."""
    return re.sub(r"//[^/@]*@", "//", base)


def _error_detail(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])[:300]
    return (r.text or "").strip()[:300]


async def _ha(ctx, method: str, path: str, payload: dict | None = None, what: str = "the request"):
    base, token = _ha_config()
    shown = _display(base)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = await ctx.http.request(method, base + path, headers=headers, json=payload, timeout=15)
    except httpx.TimeoutException:
        raise ToolError(f"Home Assistant at {shown} didn't respond in time.") from None
    except httpx.TransportError as exc:
        port_hint = ""
        if not re.search(r":\d+$", urlparse(base).netloc):
            port_hint = " Home Assistant normally listens on port 8123, e.g. http://homeassistant.local:8123."
        raise ToolError(
            f"Couldn't reach Home Assistant at {shown} ({exc.__class__.__name__}). Check HOME_ASSISTANT_URL and that "
            f"this computer is on the same network.{port_hint}"
        ) from None
    except httpx.InvalidURL:
        raise ToolError(f"HOME_ASSISTANT_URL '{shown}' isn't a valid address. Use something like http://homeassistant.local:8123.") from None
    if r.status_code == 401:
        raise ToolError("Home Assistant rejected the access token (401). " + TOKEN_HELP)
    if r.status_code >= 400:
        detail = _error_detail(r)
        hint = " Check the device supports that service and the data fields are right." if r.status_code == 400 else ""
        raise ToolError(f"Home Assistant refused {what} ({r.status_code}{': ' + detail if detail else ''}).{hint}")
    try:
        return r.json()
    except ValueError:
        raise ToolError(
            f"{shown} answered, but not like the Home Assistant API. Check HOME_ASSISTANT_URL "
            "(e.g. http://homeassistant.local:8123)."
        ) from None


def _summary(entity: dict) -> dict:
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    item = {
        "entity_id": entity.get("entity_id"),
        "name": attrs.get("friendly_name") or entity.get("entity_id"),
        "state": entity.get("state"),
    }
    for key in ATTRIBUTES:
        if attrs.get(key) not in (None, "", []):
            item[key] = attrs[key]
    if isinstance(attrs.get("brightness"), (int, float)):
        item["brightness_pct"] = round(attrs["brightness"] / 255 * 100)
    if attrs.get("unit_of_measurement"):
        item["unit"] = attrs["unit_of_measurement"]
    return item


def _variants(word: str) -> set[str]:
    out = {word}
    if len(word) > 3 and word.endswith("s"):
        out.add(word[:-1])
    if len(word) > 4 and word.endswith("es"):
        out.add(word[:-2])
    return out


def _hits(entity: dict, words: list[str]) -> int:
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    hay = f"{attrs.get('friendly_name') or ''} {str(entity.get('entity_id', '')).replace('.', ' ').replace('_', ' ')}".lower()
    tokens = set(re.findall(r"[a-z0-9]+", hay))
    count = 0
    for w in words:
        if any(v in tokens or (len(v) >= 3 and v in hay) for v in _variants(w)):
            count += 1
    return count


def _resolve_domain(raw: str, present: set[str]) -> str:
    dom = raw.strip().lower().replace(" ", "_")
    if dom in present:
        return dom
    for cand in (dom[:-2] if dom.endswith("es") else "", dom[:-1] if dom.endswith("s") else ""):
        if cand and cand in present:
            return cand
    return dom


@tool(
    "smart_home_devices",
    "List the user's Home Assistant smart-home devices with their current state (on/off, brightness, temperature "
    "and thermostat mode, locked/unlocked, open/closed, what's playing, battery). Use it to answer questions like "
    "'is the front door locked?' or 'what's the thermostat set to?', and ALWAYS to find the exact entity_id before "
    "smart_home_control. Filter with query (words from the device name, e.g. 'kitchen light') and/or domain "
    "(light, switch, climate, lock, cover, media_player, fan, scene, script, sensor, ...). With no filter it lists "
    "controllable devices and counts the rest by domain.",
    {
        "query": {"type": "string", "description": "Words from the device's name or entity_id, e.g. 'living room lamp'."},
        "domain": {"type": "string", "description": "Device type (Home Assistant domain), e.g. 'light' or 'climate'."},
        "limit": {"type": "integer", "description": "Maximum devices to return (default 40)."},
    },
    label="Checking smart home",
    timeout=30,
)
async def smart_home_devices(ctx, query: str | None = None, domain: str | None = None, limit: int = 40):
    states = await _ha(ctx, "GET", "/api/states", what="the device list")
    if not isinstance(states, list):
        raise ToolError("Home Assistant sent an unexpected reply for the device list.")
    states = [s for s in states if isinstance(s, dict) and isinstance(s.get("entity_id"), str) and "." in s["entity_id"]]
    try:
        limit = max(1, min(int(limit or 40), 200))
    except (TypeError, ValueError):
        limit = 40
    present = {s["entity_id"].split(".", 1)[0] for s in states}
    domain = str(domain or "").strip()
    dom = _resolve_domain(domain, present) if domain else ""
    pool = [s for s in states if s["entity_id"].split(".", 1)[0] == dom] if dom else states
    words = [w for w in re.findall(r"[a-z0-9]+", str(query or "").lower()) if w not in _STOPWORDS]

    other: dict[str, int] = {}
    partial = False
    if words:
        scored = [(s, _hits(s, words)) for s in pool]
        matches = [s for s, n in scored if n == len(words)]
        if not matches:  # nothing has every word: offer the closest ones instead
            best = max((n for _, n in scored), default=0)
            matches = [s for s, n in scored if best and n == best]
            partial = bool(matches)
    elif dom:
        matches = pool
    else:
        matches = [s for s in pool if s["entity_id"].split(".", 1)[0] in CONTROL_DOMAINS]
        for s in pool:
            d = s["entity_id"].split(".", 1)[0]
            if d not in CONTROL_DOMAINS:
                other[d] = other.get(d, 0) + 1

    rank = {d: i for i, d in enumerate(CONTROL_DOMAINS)}
    matches.sort(key=lambda s: (rank.get(s["entity_id"].split(".", 1)[0], len(rank)), s["entity_id"]))
    devices = [_summary(s) for s in matches[:limit]]
    out = {"status": f"{len(matches)} device{'s' if len(matches) != 1 else ''}", "count": len(matches), "devices": devices}
    if len(matches) > limit:
        out["truncated"] = True
    if partial:
        out["note"] = "No device matched every word; these are the closest matches."
    if other:
        out["other_domains"] = dict(sorted(other.items(), key=lambda kv: -kv[1]))
    if not matches and dom and dom not in present:
        out["note"] = f"There are no '{dom}' devices. Domains available: {', '.join(sorted(present)) or 'none'}."
    return out


# ------------------------------------------------------------------ control
def _parse_data(data) -> dict:
    if data in (None, "", {}):
        return {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise ToolError('data must be a JSON object, e.g. {"brightness_pct": 50}.')
    bad = sorted(TARGET_KEYS & set(data))
    if bad:
        raise ToolError(f"Pick devices with entity_id only; remove {', '.join(bad)} from data.")
    out = {str(k): v for k, v in data.items() if k != "entity_id"}
    try:
        json.dumps(out)
    except (TypeError, ValueError):
        raise ToolError("data contains values that can't be sent to Home Assistant.") from None
    return out


def parse_call(entity_id, service, data=None) -> tuple[str, str, list[str], dict]:
    """Validate a control request. Returns (domain, service, entity_ids, payload with entity_id merged in)."""
    svc = str(service or "").strip().lower().replace(" ", "_")
    domain = ""
    if "." in svc:
        domain, _, svc = svc.partition(".")
    raw = entity_id if isinstance(entity_id, list) else re.split(r"[,\s]+", str(entity_id or ""))
    ids = [str(e).strip().lower() for e in raw if str(e).strip()]
    if not ids:
        raise ToolError("Say which device to control (its entity_id). Look it up with smart_home_devices.")
    if ids == ["all"]:
        if not domain:
            raise ToolError("To target every device of a kind, give the service with its domain, e.g. 'light.turn_off'.")
    else:
        for e in ids:
            d, dot, obj = e.partition(".")
            if not dot or not _NAME.match(d) or not _NAME.match(obj):
                raise ToolError(f"'{e}' isn't a valid entity_id (like light.kitchen). Look it up with smart_home_devices.")
        if not domain:
            kinds = sorted({e.split(".", 1)[0] for e in ids})
            if len(kinds) > 1:
                raise ToolError(
                    f"Those devices are different kinds ({', '.join(kinds)}). Control each kind separately, or use "
                    "a domain service such as 'homeassistant.turn_off'."
                )
            domain = kinds[0]
    if not _NAME.match(domain) or not _NAME.match(svc):
        raise ToolError(f"'{service}' isn't a valid service. Use a name like 'turn_on' or 'light.turn_on'.")
    payload = _parse_data(data)
    payload["entity_id"] = ids[0] if len(ids) == 1 else ids
    return domain, svc, ids, payload


def _confirm_list() -> set[str]:
    return {x.strip().lower() for x in re.split(r"[,\s]+", _setting("HOME_ASSISTANT_CONFIRM_DOMAINS")) if x.strip()}


_ENTITY = re.compile(r"^([a-z0-9_]+)\.([a-z0-9_]+)$")
MAX_LOOKUPS = 25  # entities the gate looks up per call; more than that is put on a card instead


def _strings(value, depth: int = 0):
    """Every string key and value inside service data (nested dicts and lists), for the safety checks."""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, depth + 1)


def _words(text) -> set[str]:
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])  # doors -> door, locks -> lock
    return out


def _extra_data(payload: dict) -> dict:
    return {k: v for k, v in (payload or {}).items() if k != "entity_id"}


def _reach(domain: str, service: str, ids: list[str], payload: dict, states: dict) -> list[str]:
    """Every entity a call can touch: its entity_ids, a script run as its own service (script.<name>), entity ids
    named inside the data (scene.apply's entities, script variables) and the members of looked-up groups/scenes."""
    found = [e for e in ids if "." in e]
    if domain == "script" and service not in SCRIPT_SERVICES:
        found.append(f"script.{service}")
    for text in _strings(_extra_data(payload)):
        m = _ENTITY.match(text.strip().lower())
        if m and m.group(1) in KNOWN_DOMAINS:
            found.append(m.group(0))
    i = 0
    found = list(dict.fromkeys(found))
    while i < len(found):  # members of scenes, switch groups etc. (attributes.entity_id), one level per lookup
        attrs = (states.get(found[i]) or {}).get("attributes")
        members = attrs.get("entity_id") if isinstance(attrs, dict) else None
        for m in members if isinstance(members, list) else []:
            m = str(m).strip().lower()
            if _ENTITY.match(m) and m not in found:
                found.append(m)
        i += 1
    return found


def _friendly(states: dict, entity: str) -> str:
    attrs = (states.get(entity) or {}).get("attributes")
    return str(attrs.get("friendly_name") or "") if isinstance(attrs, dict) else ""


def approval_reason(domain: str, service: str, ids: list[str], payload: dict | None = None, states: dict | None = None) -> str | None:
    """Why this call needs the user's approval, or None when it can run straight away. `states` holds live
    Home Assistant state objects for entities the gate looked up (friendly names, scene/group members)."""
    payload, states = payload or {}, states or {}
    reach = _reach(domain, service, ids, payload, states)
    involved = {domain} | {e.split(".", 1)[0] for e in reach}
    confirm = _confirm_list()
    if confirm & {"*", "all"}:
        return "you asked to approve every smart-home action (HOME_ASSISTANT_CONFIRM_DOMAINS)"
    listed = confirm & (involved | {f"{domain}.{service}"} | set(reach))
    if listed:
        return f"{sorted(listed)[0]} is on your approval list (HOME_ASSISTANT_CONFIRM_DOMAINS)"
    for word, why in RISKY_SERVICE_WORDS.items():
        if word in service:
            return why
    for d in sorted(involved & set(SECURITY_DOMAINS)):
        if service in SAFE_SECURITY_SERVICES.get(d, ()):
            continue
        return f"it controls {SECURITY_DOMAINS[d]}"
    if domain in ADMIN_DOMAINS and (service not in GENERIC_SERVICES or ids == ["all"]):
        return "it affects Home Assistant itself or every device"
    if ids == ["all"] and domain not in SAFE_ALL_DOMAINS:
        return f"it targets every {domain.replace('_', ' ')} device at once"
    safe_security = {d for d in SECURITY_DOMAINS if service in SAFE_SECURITY_SERVICES.get(d, ())}
    for d in sorted(involved - ROUTINE_DOMAINS - safe_security):
        return DOMAIN_REASONS.get(d) or (
            f"it uses '{d}', and ATLAS only runs everyday device controls (lights, switches, media, climate, scenes) "
            "without asking"
        )
    for e in reach:
        d, _, obj = e.partition(".")
        name = _friendly(states, e)
        label = f"{e} ({name})" if name else e
        if "unlock" in obj or "disarm" in obj or _words(name) & {"unlock", "disarm"}:
            return f"{label} sounds security-related"
        if d in NAME_CHECK_DOMAINS and (_words(obj) | _words(name)) & RELAY_WORDS:
            return f"{label} may operate a door, gate, lock, alarm or valve"
        if d in NAME_CHECK_DOMAINS and (_words(obj) | _words(name)) & DESTRUCTIVE_WORDS:
            return f"{label} may reset, wipe or reflash a device"
    if domain in NAME_CHECK_DOMAINS:
        hit = sorted(set().union(*(_words(t) for t in _strings(_extra_data(payload)))) & RELAY_WORDS)
        if hit:
            return f"the request mentions '{hit[0]}', so it may operate a door, gate, lock, alarm or valve"
    return None


class _NoLookup(Exception):
    """Home Assistant isn't set up or rejects the token: the call itself fails with that message, so skip."""


async def _fetch_states(ctx, entities: list[str]) -> dict[str, dict]:
    http = getattr(ctx, "http", None)
    if http is None:
        raise _NoLookup
    try:
        base, token = _ha_config()
    except ToolError:
        raise _NoLookup from None
    headers = {"Authorization": f"Bearer {token}"}

    async def one(entity: str) -> tuple[str, dict]:
        r = await http.get(f"{base}/api/states/{entity}", headers=headers, timeout=5)
        if r.status_code == 404:
            return entity, {}  # unknown entity: Home Assistant ignores it in a service call
        if r.status_code == 401:
            raise _NoLookup
        r.raise_for_status()
        data = r.json()
        return entity, data if isinstance(data, dict) else {}

    return dict(await asyncio.gather(*(one(e) for e in entities)))


async def _live_states(ctx, domain: str, service: str, ids: list[str], payload: dict) -> dict[str, dict]:
    """Look up the name-checked entities (and, one level down, their members). Raises on network trouble."""
    states: dict[str, dict] = {}
    for _ in range(2):
        need = [
            e for e in _reach(domain, service, ids, payload, states)
            if e.split(".", 1)[0] in NAME_CHECK_DOMAINS and e not in states
        ]
        if not need:
            break
        if len(states) + len(need) > MAX_LOOKUPS:
            raise ToolError(f"too many devices to check one by one ({len(states) + len(need)})")
        try:
            states.update(await _fetch_states(ctx, need))
        except _NoLookup:
            break
    return states


def _mask(value, depth: int = 0):
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: ("••••" if str(k).lower() in SECRET_KEYS else _mask(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, depth + 1) for v in value]
    return value


def _clip(text: str, n: int = 400) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _control_preview(domain: str, service: str, ids: list[str], payload: dict, reason: str) -> dict:
    fields = [["Device" if len(ids) == 1 else "Devices", ", ".join(ids)], ["Action", f"{domain}.{service}"]]
    extra = _mask(_extra_data(payload))
    if extra:
        fields.append(["Data", _clip(json.dumps(extra, ensure_ascii=False, default=str))])
    return {
        "title": f"Smart home: {service.replace('_', ' ')}",
        "fields": fields,
        "body": f"This needs your approval because {reason}.",
    }


async def _gate_card(ctx, args: dict) -> dict | None:
    args = args if isinstance(args, dict) else {}
    try:
        domain, service, ids, payload = parse_call(args.get("entity_id"), args.get("service"), args.get("data"))
    except ToolError:
        return None  # the call itself fails with the same message before doing anything; nothing to approve
    except Exception:  # fail closed on anything unexpected
        return {
            "title": "Smart home action",
            "fields": [[str(k), _clip(json.dumps(_mask(v), ensure_ascii=False, default=str), 300)] for k, v in args.items()],
            "body": "This needs your approval because the request couldn't be checked automatically.",
        }
    try:
        reason = approval_reason(domain, service, ids, payload)
        if not reason and any(e.split(".", 1)[0] in NAME_CHECK_DOMAINS for e in _reach(domain, service, ids, payload, {})):
            try:
                states = await _live_states(ctx, domain, service, ids, payload)
            except Exception as exc:  # unreachable, timeout, odd reply: ask rather than guess
                detail = str(exc) if isinstance(exc, ToolError) else exc.__class__.__name__
                reason = f"ATLAS couldn't check what this device is connected to ({detail})"
            else:
                reason = approval_reason(domain, service, ids, payload, states)
    except Exception:  # fail closed if the check itself breaks (e.g. an unreadable .env)
        reason = "the safety check couldn't be completed"
    return _control_preview(domain, service, ids, payload, reason) if reason else None


# Calls the gate has been consulted for. The agent runs a tool only after its gate (and the user's approval when
# the gate returns a card), so a call missing from here means the gate was bypassed or never wired.
GATE_TTL = 900.0
_GATE_SEEN: dict[str, float] = {}


def _call_key(domain: str, service: str, ids: list[str], payload: dict) -> str:
    return json.dumps([domain, service, ids, payload], sort_keys=True, default=str)


def _note_gate(key: str) -> None:
    now = time.monotonic()
    for k, t in list(_GATE_SEEN.items()):
        if now - t > GATE_TTL:
            del _GATE_SEEN[k]
    _GATE_SEEN[key] = now
    while len(_GATE_SEEN) > 500:
        _GATE_SEEN.pop(next(iter(_GATE_SEEN)))


def _gate_seen(key: str) -> bool:
    t = _GATE_SEEN.get(key)
    return t is not None and time.monotonic() - t <= GATE_TTL


async def control_gate(ctx, args: dict) -> dict | None:
    """Approval gate for smart_home_control: a preview card for security-relevant calls, None otherwise."""
    card = await _gate_card(ctx, args)
    try:
        a = args if isinstance(args, dict) else {}
        _note_gate(_call_key(*parse_call(a.get("entity_id"), a.get("service"), a.get("data"))))
    except Exception:
        pass  # invalid call: the tool rejects it anyway
    return card


@tool(
    "smart_home_control",
    "Control a Home Assistant device by calling a service on it. Examples: light.turn_on (data "
    '{"brightness_pct": 40} or {"color_name": "blue"}), light.turn_off, switch.toggle, climate.set_temperature '
    '({"temperature": 70}), climate.set_hvac_mode ({"hvac_mode": "heat"}), fan.set_percentage ({"percentage": 50}), '
    'media_player.media_pause, media_player.volume_set ({"volume_level": 0.3}), cover.open_cover, lock.lock, '
    "lock.unlock, scene.turn_on, script.turn_on. Get entity_ids from smart_home_devices first; never guess them. "
    "service may be 'turn_on' (domain taken from the entity) or 'domain.service'. Everyday actions (lights, "
    "switches, media, climate, scenes) run straight away. Locks, alarms, garage doors and covers, valves, sirens, "
    "cameras, any unlock/open/disarm action, door or gate relays, 'all' targets beyond lights/media/fans/climate, and "
    "anything beyond device control (notifications, automations, updates, system services) show the user an "
    "approval card automatically, so call it with the final arguments and don't ask first.",
    {
        "entity_id": {"type": "string", "description": "e.g. 'light.kitchen', or several of the same kind separated by commas."},
        "service": {"type": "string", "description": "e.g. 'turn_on', 'toggle', 'set_temperature' or 'scene.turn_on'."},
        "data": {"type": "object", "description": "Extra service fields, e.g. {\"brightness_pct\": 30}. Omit if none."},
    },
    ["entity_id", "service"],
    label="Controlling smart home",
    timeout=30,
    gate=control_gate,
)
async def smart_home_control(ctx, entity_id, service: str, data=None):
    domain, svc, ids, payload = parse_call(entity_id, service, data)
    settings = getattr(ctx, "settings", None)
    auto = "smart_home_control" in (getattr(settings, "auto_approve", None) or set())
    if not auto and not _gate_seen(_call_key(domain, svc, ids, payload)):
        # Reached without the approval gate (e.g. the gate isn't wired into the agent): run the same check here and
        # refuse anything that would have needed the user's approval.
        card = await _gate_card(ctx, {"entity_id": entity_id, "service": service, "data": data})
        if card:
            raise ToolError(
                f"Not done. {card['body']} This request didn't go through ATLAS's approval card, so nothing was sent "
                "to Home Assistant."
            )
    result = await _ha(ctx, "POST", f"/api/services/{domain}/{svc}", payload, what=f"{domain}.{svc}")
    changed = [_summary(e) for e in result if isinstance(e, dict) and e.get("entity_id")] if isinstance(result, list) else []
    changed.sort(key=lambda e: e["entity_id"] not in ids)  # the devices asked about first
    out = {"status": f"Called {domain}.{svc} on {', '.join(ids)}", "changed": changed[:15]}
    if not changed:
        out["note"] = "Home Assistant accepted the command; the device may take a moment to report its new state."
    return out
