"""Persistent personal state: long-term memory, contacts, notes and reminders (JSON files in data/)."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from ..config import DATA_DIR
from . import ToolError, tool

_LOCK = threading.Lock()


def _path(name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{name}.json"


def load(name: str, default):
    p = _path(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def save(name: str, value) -> None:
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _now() -> datetime:
    return datetime.now().astimezone()


# ---------------------------------------------------------------------- memory
def memories() -> list[dict]:
    return load("memory", [])


@tool(
    "remember",
    "Save a lasting fact or preference about the user to long-term memory (e.g. 'Prefers metric units', "
    "'Sister is named Maya', 'Works at Acme as a data analyst'). Don't store secrets or passwords.",
    {"fact": {"type": "string"}},
    ["fact"],
    label="Committing to memory",
)
async def remember(ctx, fact: str):
    fact = fact.strip()
    if not fact:
        raise ToolError("Nothing to remember.")
    with _LOCK:
        items = memories()
        if any(m["fact"].lower() == fact.lower() for m in items):
            return {"status": "already known", "fact": fact}
        items.append({"id": uuid.uuid4().hex[:8], "fact": fact, "saved": _now().isoformat(timespec="minutes")})
        save("memory", items[-200:])
    return {"status": "remembered", "fact": fact}


@tool(
    "forget",
    "Remove facts from long-term memory that contain the given text.",
    {"match": {"type": "string"}},
    ["match"],
    label="Erasing memory",
)
async def forget(ctx, match: str):
    match = match.strip()
    if not match:  # "" is a substring of every fact and would wipe the whole memory
        raise ToolError("Say which fact to forget.")
    with _LOCK:
        items = memories()
        keep = [m for m in items if match.lower() not in m["fact"].lower()]
        save("memory", keep)
    return {"removed": len(items) - len(keep)}


def delete_memory(mem_id: str) -> bool:
    with _LOCK:
        items = memories()
        keep = [m for m in items if m["id"] != mem_id]
        save("memory", keep)
    return len(keep) != len(items)


# -------------------------------------------------------------------- contacts
def contacts() -> list[dict]:
    return load("contacts", [])


def find_contact(name: str) -> dict | None:
    name = name.strip().lower()
    items = contacts()
    if not name:
        return None
    for c in items:
        if c["name"].lower() == name:
            return c
    # An exact alias ("mom") beats a partial name match ("Tom Mom...").
    by_alias = [c for c in items if name in [a.lower() for a in c.get("aliases") or []]]
    if len(by_alias) == 1:
        return by_alias[0]
    matches = [c for c in items if name in c["name"].lower() or c in by_alias]
    return matches[0] if len(matches) == 1 else None


@tool(
    "save_contact",
    "Save or update a contact so the user can refer to them by name (e.g. 'Mom', 'Coach Rivera').",
    {
        "name": {"type": "string"},
        "email": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}, "description": "Other names, e.g. ['mom', 'mother']"},
        "phone": {"type": "string"},
    },
    ["name", "email"],
    label="Updating contacts",
)
async def save_contact(ctx, name: str, email: str, aliases=None, phone: str | None = None):
    if "@" not in email:
        raise ToolError("That doesn't look like an email address.")
    if isinstance(aliases, str):  # models sometimes send "mom, mother" instead of a list
        aliases = [a.strip() for a in aliases.split(",") if a.strip()]
    with _LOCK:
        items = [c for c in contacts() if c["name"].lower() != name.lower()]
        items.append({"name": name, "email": email, "aliases": aliases or [], "phone": phone or ""})
        save("contacts", sorted(items, key=lambda c: c["name"].lower()))
    return {"status": "saved", "name": name, "email": email}


@tool(
    "list_contacts",
    "List saved contacts, optionally filtered by text.",
    {"query": {"type": "string"}},
    label="Reading contacts",
)
async def list_contacts(ctx, query: str | None = None):
    items = contacts()
    if query:
        q = query.lower()
        items = [c for c in items if q in json.dumps(c).lower()]
    return {"contacts": items}


# ----------------------------------------------------------------------- notes
@tool(
    "add_note",
    "Save a note, idea, list or snippet to the user's notebook.",
    {"text": {"type": "string"}, "title": {"type": "string"}},
    ["text"],
    label="Writing note",
)
async def add_note(ctx, text: str, title: str | None = None):
    with _LOCK:
        items = load("notes", [])
        note = {"id": uuid.uuid4().hex[:8], "title": title or text[:48], "text": text, "created": _now().isoformat(timespec="minutes")}
        items.append(note)
        save("notes", items)
    return {"status": "saved", "note": note}


@tool(
    "search_notes",
    "Find notes in the user's notebook. Omit query to list the most recent.",
    {"query": {"type": "string"}, "limit": {"type": "integer"}},
    label="Searching notes",
)
async def search_notes(ctx, query: str | None = None, limit: int = 10):
    items = load("notes", [])
    if query:
        q = query.lower()
        items = [n for n in items if q in n["text"].lower() or q in n["title"].lower()]
    return {"notes": items[::-1][: max(1, min(int(limit or 10), 50))]}


@tool(
    "delete_note",
    "Delete a note by id.",
    {"id": {"type": "string"}},
    ["id"],
    label="Deleting note",
)
async def delete_note(ctx, id: str):
    with _LOCK:
        items = load("notes", [])
        keep = [n for n in items if n["id"] != id]
        save("notes", keep)
    return {"removed": len(items) - len(keep)}


# ------------------------------------------------------------------- reminders
def _parse_when(minutes: float | None, at: str | None) -> datetime:
    now = _now()
    if minutes is not None:
        return now + timedelta(minutes=float(minutes))
    if at:
        try:
            dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise ToolError("Use ISO format for 'at', e.g. 2026-11-05T09:30.")
        if dt.tzinfo is None:
            # Interpret as local wall-clock time on *that* date (DST-aware), not today's UTC offset.
            dt = dt.astimezone()
        return dt
    raise ToolError("Give either minutes_from_now or at.")


@tool(
    "set_reminder",
    "Set a reminder or timer. ATLAS will announce it aloud and show a notification when due. "
    "Use minutes_from_now for timers ('in 20 minutes') or an ISO local datetime in 'at'.",
    {
        "message": {"type": "string"},
        "minutes_from_now": {"type": "number"},
        "at": {"type": "string", "description": "Local ISO datetime, e.g. '2026-11-05T09:30'"},
    },
    ["message"],
    label="Scheduling reminder",
)
async def set_reminder(ctx, message: str, minutes_from_now: float | None = None, at: str | None = None):
    due = _parse_when(minutes_from_now, at)
    if due < _now() - timedelta(minutes=1):
        raise ToolError("That time is in the past.")
    with _LOCK:
        items = load("reminders", [])
        r = {"id": uuid.uuid4().hex[:8], "message": message, "due": due.isoformat(timespec="seconds"), "fired": False}
        items.append(r)
        save("reminders", items)
    return {"status": "scheduled", "reminder": r, "due_human": due.strftime("%a %b %d, %I:%M %p")}


def _parse_due(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def pending_reminders() -> list[dict]:
    # Sort by the actual instant: ISO strings with different UTC offsets don't sort as text.
    def key(r):
        due = _parse_due(r.get("due"))
        return due.timestamp() if due else float("inf")

    return sorted([r for r in load("reminders", []) if not r.get("fired")], key=key)


@tool(
    "list_reminders",
    "List upcoming reminders and timers.",
    {},
    label="Reading reminders",
)
async def list_reminders(ctx):
    return {"reminders": pending_reminders(), "now": _now().isoformat(timespec="minutes")}


@tool(
    "cancel_reminder",
    "Cancel a reminder by id.",
    {"id": {"type": "string"}},
    ["id"],
    label="Cancelling reminder",
)
async def cancel_reminder(ctx, id: str):
    with _LOCK:
        items = load("reminders", [])
        keep = [r for r in items if r["id"] != id]
        save("reminders", keep)
    return {"removed": len(items) - len(keep)}


def pop_due_reminders() -> list[dict]:
    now = _now()
    due = []
    with _LOCK:
        items = load("reminders", [])
        for r in items:
            if r.get("fired"):
                continue
            when = _parse_due(r.get("due"))
            if when is None:
                continue
            if when <= now:
                r["fired"] = True
                due.append(r)
        if due:
            # keep the file small: drop reminders fired more than a day ago
            cutoff = now - timedelta(days=1)
            items = [r for r in items if not r.get("fired") or (_parse_due(r.get("due")) or now) > cutoff]
            save("reminders", items)
    return due
