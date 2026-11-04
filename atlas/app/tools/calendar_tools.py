"""Calendar and to-do list.

Calendar data comes from two places, merged into one sorted view:
  * the user's own calendars, read from their *secret iCal (ICS) addresses* in CALENDAR_ICS_URLS
    (Google Calendar, iCloud, Outlook.com ...). Read-only; each feed is cached for a few minutes.
  * a local calendar in DATA_DIR/calendar.json holding the events ATLAS creates.

ATLAS can't write to Google/Outlook without OAuth, so add_calendar_event saves the event locally and
then hands it to the user's calendar app per CALENDAR_PROVIDER: a prefilled Google Calendar or
Outlook "new event" page in the browser, or an .ics file opened with the default calendar app. The
user clicks Save there. The to-do list lives in DATA_DIR/tasks.json.

Times are shown in the computer's local time zone. All-day events have plain dates for start/end,
with an inclusive end date (a one-day event has start == end).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
import weakref
import webbrowser
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

from ..config import DATA_DIR, load_env_file
from ..util import clip, html_to_text
from . import ToolError, tool
from .memory import find_contact

try:  # optional at import time so the rest of ATLAS still starts without them
    import icalendar
    import recurring_ical_events
except ImportError:  # pragma: no cover - exercised only when the packages are missing
    icalendar = None
    recurring_ical_events = None

SYSTEM = platform.system()
IS_WIN, IS_MAC = SYSTEM == "Windows", SYSTEM == "Darwin"

FEED_TTL = 300  # seconds a fetched ICS feed is reused
RETRY_AFTER = 60  # seconds before a feed that just failed is tried again (calls stay fast while it's down)
FEED_TIMEOUT = 20  # per network operation
FEED_DEADLINE = 30  # whole download of one feed, so a slow-dripping server can't stall a tool call
MAX_FEED_BYTES = 40 * 1024 * 1024
# Expanding one recurring series costs roughly one step per occurrence since its DTSTART. Anyone can
# send an invitation (Google adds them to the calendar automatically), so a series that would take
# more steps than this (e.g. RRULE:FREQ=MINUTELY since 1990: ~80 s per query) is skipped.
MAX_SERIES_STEPS = 600_000
MAX_FEED_STEPS = 3_000_000  # all series of one feed together
YEAR_RANGE = (1971, 2199)  # Windows can't convert local times before 1970; far-future dates overflow
# Windows hands URLs to the browser through ShellExecute, which fails beyond ~2080 characters.
MAX_LINK_CHARS = 2000 if IS_WIN else 8000
MAX_DAYS = 60
MAX_EVENTS = 50
DEFAULT_DURATION = 60
DEFAULT_DAY_HOURS = (8 * 60, 20 * 60)  # minutes after midnight
LOCAL_CALENDAR_NAME = "ATLAS"
PROVIDERS = {"google", "outlook", "office365", "ics", "local"}
_PROVIDER_ALIASES = {
    "": "google", "gcal": "google", "google calendar": "google",
    "outlook.com": "outlook", "hotmail": "outlook", "live": "outlook", "microsoft": "outlook",
    "office": "office365", "o365": "office365", "microsoft365": "office365", "m365": "office365",
    "apple": "ics", "icloud": "ics", "file": "ics", "none": "local", "atlas": "local",
}
PRIORITIES = {"high": 0, "normal": 1, "low": 2}
_PRIORITY_ALIASES = {
    "": "normal", "medium": "normal", "med": "normal", "mid": "normal", "default": "normal",
    "urgent": "high", "important": "high", "critical": "high", "asap": "high", "minor": "low",
}

_LOCK = threading.Lock()
# url -> (monotonic fetch time, recurring_ical_events query, calendar name from the feed)
_FEEDS: dict[str, tuple[float, Any, str]] = {}
# url -> (monotonic time of the failed fetch, scrubbed reason)
_FAILED: dict[str, tuple[float, str]] = {}
# event loop -> url -> lock, so parallel tool calls in one turn share a single fetch per feed
_FETCH_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()


def _setting(key: str, default: str = "") -> str:
    return os.environ.get(key) or load_env_file().get(key) or default


# ------------------------------------------------------------------ storage
def _path(name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{name}.json"


def _load(name: str) -> list:
    p = _path(name)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _save(name: str, value: list) -> None:
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# -------------------------------------------------------------------- time
def _now() -> datetime:
    return datetime.now().astimezone()


def _local(dt: datetime) -> datetime:
    """Aware datetime in the computer's zone. Naive (floating) times are taken as local wall time."""
    return dt.astimezone()


def _midnight(d: date) -> datetime:
    return datetime.combine(d, dtime()).astimezone()  # DST-aware for that date


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


_DAY_WORDS = {"today": 0, "tomorrow": 1, "yesterday": -1}
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_when(value, field: str) -> datetime | date:
    """ISO date ('2026-11-05') -> date; ISO datetime -> aware local datetime."""
    s = str(value or "").strip()
    if not s:
        raise ToolError(f"Give {field} as an ISO date or date-time, e.g. 2026-11-05 or 2026-11-05T15:00.")
    word = _DAY_WORDS.get(s.lower())
    if word is not None:
        return _now().date() + timedelta(days=word)
    if _DATE_ONLY.fullmatch(s):
        try:
            d = date.fromisoformat(s)
        except ValueError:
            raise ToolError(f"{field} '{s}' isn't a real date.")
        _check_year(d.year, field, s)
        return d
    iso = s[:-1] + "+00:00" if s[-1:] in "Zz" else s
    try:
        dt = datetime.fromisoformat(iso.replace(" ", "T", 1))
    except ValueError:
        raise ToolError(f"Use ISO format for {field}, e.g. 2026-11-05T15:00 (got '{s}').")
    _check_year(dt.year, field, s)
    try:
        return _local(dt)
    except (OverflowError, OSError, ValueError):
        raise ToolError(f"{field} '{s}' is out of range.")


def _check_year(year: int, field: str, raw: str) -> None:
    if not YEAR_RANGE[0] <= year <= YEAR_RANGE[1]:
        raise ToolError(f"{field} '{raw}' is out of range (years {YEAR_RANGE[0]}-{YEAR_RANGE[1]}).")


def _is_datetime(v) -> bool:
    return isinstance(v, datetime)


def _day_label(d: date, today: date | None = None) -> str:
    today = today or _now().date()
    label = f"{d:%a %b} {d.day}"
    return label if d.year == today.year else f"{label}, {d.year}"


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _when(start: datetime, end: datetime, all_day: bool) -> str:
    if all_day:
        last = (end - timedelta(days=1)).date() if end.date() > start.date() else start.date()
        if last == start.date():
            return f"{_day_label(start.date())} (all day)"
        return f"{_day_label(start.date())} – {_day_label(last)} (all day)"
    if end <= start:
        return f"{_day_label(start.date())}, {_clock(start)}"
    if end.date() == start.date():
        return f"{_day_label(start.date())}, {_clock(start)} – {_clock(end)}"
    return f"{_day_label(start.date())}, {_clock(start)} – {_day_label(end.date())}, {_clock(end)}"


def _day_hours() -> tuple[int, int]:
    raw = _setting("CALENDAR_DAY_HOURS", "08:00-20:00")
    m = re.fullmatch(r"\s*(\d{1,2})(?::([0-5]\d))?\s*-\s*(\d{1,2})(?::([0-5]\d))?\s*", raw)
    if not m:
        return DEFAULT_DAY_HOURS
    a = int(m.group(1)) * 60 + int(m.group(2) or 0)
    b = int(m.group(3)) * 60 + int(m.group(4) or 0)
    if not (0 <= a < b <= 24 * 60):
        return DEFAULT_DAY_HOURS
    return a, b


def _at_minutes(d: date, minutes: int) -> datetime:
    if minutes >= 24 * 60:
        return _midnight(d + timedelta(days=1))
    return datetime.combine(d, dtime(minutes // 60, minutes % 60)).astimezone()


# ------------------------------------------------------------- event model
def _event(*, id: str, title: str, start: datetime, end: datetime, all_day: bool, calendar: str,
           source: str, location: str = "", description: str = "", busy: bool = True,
           tentative: bool = False, attendees: list | None = None) -> dict:
    """Internal event: aware local datetimes in _start/_end (all-day: midnight, end exclusive)."""
    return {
        "_start": start, "_end": end, "_busy": busy,
        "id": id, "title": clip(title, 300) or "(no title)", "all_day": all_day,
        "location": clip(location, 300), "description": description or "",
        "calendar": calendar, "source": source, "tentative": tentative,
        "attendees": attendees or [],
    }


def _public(ev: dict, with_description: bool = True) -> dict:
    s, e = ev["_start"], ev["_end"]
    if ev["all_day"]:
        last = (e - timedelta(days=1)).date() if e.date() > s.date() else s.date()
        start, end = s.date().isoformat(), last.isoformat()
    else:
        start, end = s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes")
    out = {
        "id": ev["id"], "title": ev["title"], "start": start, "end": end,
        "when": _when(s, e, ev["all_day"]), "all_day": ev["all_day"],
    }
    if ev["location"]:
        out["location"] = ev["location"]
    if with_description and ev["description"]:
        out["description"] = clip(ev["description"], 200)
    if ev["attendees"]:
        out["attendees"] = ev["attendees"]
    if ev["tentative"]:
        out["tentative"] = True
    if not ev["_busy"]:
        out["show_as"] = "free"
    out["calendar"] = ev["calendar"]
    out["source"] = ev["source"]
    return out


def _overlaps(ev: dict, start: datetime, end: datetime) -> bool:
    if ev["_end"] <= ev["_start"]:  # zero-length event: a point in time
        return start <= ev["_start"] < end
    return ev["_start"] < end and ev["_end"] > start


# ------------------------------------------------------------ ICS feeds
def _feed_specs() -> list[dict]:
    """CALENDAR_ICS_URLS: comma-separated URLs, each optionally labelled 'Work=https://...'."""
    specs = []
    seen: set[str] = set()
    for item in (x.strip() for x in re.split(r"[,\n]", _setting("CALENDAR_ICS_URLS"))):
        if not item:
            continue
        label = ""
        m = re.match(r"^([^:/=|]+?)\s*[=|]\s*((?:https?|webcals?)://\S+)$", item, re.I)
        if m:
            label, item = m.group(1).strip(), m.group(2)
        url = re.sub(r"^webcals?://", "https://", item, flags=re.I)
        if not re.match(r"^https?://", url, re.I) or url in seen:  # listed twice: read it once
            continue
        seen.add(url)
        try:
            host = urlparse(url).hostname or "calendar"
        except ValueError:  # e.g. a malformed IPv6 host
            continue
        me = set()
        g = re.search(r"/calendar/ical/([^/]+)/", url)  # Google: .../calendar/ical/<calendar id>/private-.../basic.ics
        if g and "@" in unquote(g.group(1)):
            me.add(unquote(g.group(1)).lower())
        specs.append({"url": url, "label": label, "fallback": f"calendar #{len(specs) + 1} ({host})", "me": me})
    return specs


if recurring_ical_events is not None:
    class _TolerantQuery(recurring_ical_events.CalendarQuery):
        """Skip any series the library can't expand, not only the error types it lists by default,
        so one malformed invitation (e.g. DTSTART;VALUE=PERIOD) doesn't hide the whole calendar."""

        suppressed_errors = [Exception]
else:  # pragma: no cover
    _TolerantQuery = None

_PER_DAY = {"SECONDLY": 86400.0, "MINUTELY": 1440.0, "HOURLY": 24.0, "DAILY": 1.0,
            "WEEKLY": 1 / 7, "MONTHLY": 1 / 28, "YEARLY": 1 / 365}
_DAY_PARTS = ("BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS")


def _first(value):
    return value[0] if isinstance(value, (list, tuple)) and value else value


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _series_steps(rule, start: date, horizon: date) -> float:
    """Rough upper bound of the iterations needed to expand one RRULE up to horizon."""
    freq = str(_first(rule.get("FREQ")) or "").upper()
    if freq not in _PER_DAY:
        return 0.0  # the library rejects it (and _TolerantQuery skips it)
    try:
        interval = max(1, int(_first(rule.get("INTERVAL")) or 1))
    except (TypeError, ValueError):
        interval = 1
    rate = _PER_DAY[freq] / interval
    if freq in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        if freq != "DAILY" and any(rule.get(p) for p in _DAY_PARTS):
            rate = 1.0  # BYDAY etc. can expand to at most one match per day
        rate = min(rate, 1.0)
        subdaily = ("BYHOUR", "BYMINUTE", "BYSECOND")  # each expands every matching day
    else:
        subdaily = {"HOURLY": ("BYMINUTE", "BYSECOND"), "MINUTELY": ("BYSECOND",)}.get(freq, ())
    for part in subdaily:
        rate *= max(1, len(rule.get(part) or []))
    until = _as_date(_first(rule.get("UNTIL")))
    last = min(until, horizon) if until else horizon
    steps = rate * max(1, (last - start).days + 1)
    try:
        count = int(_first(rule.get("COUNT")) or 0)
    except (TypeError, ValueError):
        count = 0
    return min(steps, count) if count > 0 else steps


def _drop_dense_series(cal) -> None:
    """Remove recurring series that would be too expensive to expand (see MAX_SERIES_STEPS)."""
    horizon = date.today() + timedelta(days=400)
    heavy: set[str] = set()
    total = 0.0
    for ev in cal.walk("VEVENT"):
        rules = ev.get("RRULE")
        if rules is None:
            continue
        start = _as_date(_decoded(ev, "DTSTART"))
        if start is None:
            continue
        try:
            cost = sum(_series_steps(r, start, horizon) for r in (rules if isinstance(rules, list) else [rules]))
        except Exception:
            continue
        if cost > MAX_SERIES_STEPS or (cost > 20_000 and total + cost > MAX_FEED_STEPS):
            heavy.add(str(ev.get("UID") or id(ev)))
        else:
            total += cost
    if heavy:
        cal.subcomponents = [
            c for c in cal.subcomponents
            if not (c.name == "VEVENT" and str(c.get("UID") or id(c)) in heavy)
        ]


def _parse_feed(content: bytes):
    if icalendar is None or recurring_ical_events is None:
        raise ToolError("Calendar feeds need two packages: pip install icalendar recurring-ical-events")
    head = content[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").upper()
    if not head.startswith(b"BEGIN:VCALENDAR"):
        if b"<HTML" in head or b"<!DOCTYPE" in head:
            raise ValueError("the address returned a web page, not an iCal feed (use the secret iCal/ICS address)")
        raise ValueError("the address didn't return an iCal (ICS) calendar")
    cal = icalendar.Calendar.from_ical(content)
    name = str(cal.get("X-WR-CALNAME") or "").strip()
    _drop_dense_series(cal)
    try:
        return _query(cal), name
    except Exception:
        if "X-WR-TIMEZONE" not in cal:
            raise
    del cal["X-WR-TIMEZONE"]  # an unknown zone name there: read the times as the events give them
    return _query(cal), name


def _query(cal):
    try:
        has_param = "calendar_query" in inspect.signature(recurring_ical_events.of).parameters
    except (TypeError, ValueError):
        has_param = False
    if has_param:
        return recurring_ical_events.of(cal, skip_bad_series=True, calendar_query=_TolerantQuery)
    try:  # early 3.x releases: do what of() does there, with the tolerant query class
        import x_wr_timezone  # a dependency of recurring-ical-events

        return _TolerantQuery(x_wr_timezone.to_standard(cal), False, ("VEVENT",), True)
    except (ImportError, TypeError):
        return recurring_ical_events.of(cal, skip_bad_series=True)


def _scrub(message: str, url: str) -> str:
    """Secret calendar addresses are credentials: never echo them (or their secret parts) to the model."""
    try:
        parsed = urlparse(url)
        parts = [parsed.path, parsed.query, parsed.params, parsed.fragment, parsed.username, parsed.password]
    except ValueError:
        parts = []
    secrets = {url, unquote(url)}
    for part in parts:
        if part and len(part) >= 4:
            secrets.update({part, unquote(part)})
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "<secret>")
    return clip(message, 200)


_FEED_HEADERS = {"User-Agent": "ATLAS-assistant/1.0", "Accept": "text/calendar, text/plain, */*"}


async def _download(ctx, url: str) -> bytes:
    async with ctx.http.stream("GET", url, timeout=FEED_TIMEOUT, follow_redirects=True,
                               headers=_FEED_HEADERS) as r:
        if r.status_code in (401, 403, 404, 410):
            raise ValueError(
                f"HTTP {r.status_code} — the secret address may have been reset or mistyped; "
                "copy it again from your calendar's settings"
            )
        if r.status_code >= 400:
            raise ValueError(f"HTTP {r.status_code}")
        too_big = ValueError(f"the calendar is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB")
        declared = r.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
            raise too_big
        chunks, size = [], 0
        async for chunk in r.aiter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise too_big
            chunks.append(chunk)
        return b"".join(chunks)


class _RecentFailure(Exception):
    """The feed failed moments ago; report that again instead of waiting on it."""


async def _get_feed(ctx, spec: dict) -> tuple[Any, str, str | None]:
    """Return (query, calendar name, error) for one feed, using the cache when fresh."""
    url = spec["url"]
    locks = _FETCH_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault(url, asyncio.Lock())
    async with lock:
        hit = _FEEDS.get(url)
        if hit and time.monotonic() - hit[0] < FEED_TTL:
            return hit[1], hit[2], None
        label = spec["label"] or (hit[2] if hit else "") or spec["fallback"]
        failed = _FAILED.get(url)
        try:
            if failed and time.monotonic() - failed[0] < RETRY_AFTER:
                raise _RecentFailure(failed[1])
            content = await asyncio.wait_for(_download(ctx, url), timeout=FEED_DEADLINE)
            query, feed_name = await asyncio.to_thread(_parse_feed, content)
        except ToolError as exc:  # e.g. the icalendar packages aren't installed
            return None, label, str(exc)
        except Exception as exc:
            if isinstance(exc, _RecentFailure):
                reason = str(exc)
            elif isinstance(exc, asyncio.TimeoutError):
                reason = f"no complete answer within {FEED_DEADLINE} s"
            else:
                reason = _scrub(str(exc) or exc.__class__.__name__, url)
            if not isinstance(exc, _RecentFailure):
                _FAILED[url] = (time.monotonic(), reason)
            if hit:
                mins = int((time.monotonic() - hit[0]) // 60)
                age = "a few minutes ago" if mins < 10 else (f"{mins} minutes ago" if mins < 120 else f"{mins // 60} hours ago")
                return hit[1], hit[2], f"Couldn't refresh '{label}' ({reason}); showing the copy from {age}."
            return None, label, f"Couldn't read '{label}': {reason}."
        name = spec["label"] or feed_name or spec["fallback"]
        _FEEDS[url] = (time.monotonic(), query, name)
        _FAILED.pop(url, None)
        return query, name, None


def _attendee_declined(ev, me: set[str]) -> bool:
    if not me:
        return False
    att = ev.get("ATTENDEE")
    if att is None:
        return False
    for a in att if isinstance(att, list) else [att]:
        addr = str(a).strip().lower()
        if addr.startswith("mailto:"):
            addr = addr[7:]
        params = getattr(a, "params", {}) or {}
        if addr in me and str(params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


def _decoded(ev, key: str):
    try:
        return ev.decoded(key) if key in ev else None
    except Exception:
        return None


def _text(ev, key: str) -> str:
    v = ev.get(key)
    if v is None:
        return ""
    if isinstance(v, list):
        v = v[0] if v else ""
    s = str(v).strip()
    if "<" in s and ">" in s:  # Google puts HTML in descriptions
        s = html_to_text(s)
    return s


def _feed_event(ev, cal_name: str) -> dict | None:
    start_raw = _decoded(ev, "DTSTART")
    if start_raw is None:
        return None
    end_raw = _decoded(ev, "DTEND")
    dur = _decoded(ev, "DURATION")
    if not _is_datetime(start_raw):  # all-day
        s_day: date = start_raw
        if end_raw is not None:
            e_day = end_raw.date() if _is_datetime(end_raw) else end_raw
        elif isinstance(dur, timedelta):
            e_day = s_day + timedelta(days=max(1, dur.days))
        else:
            e_day = s_day + timedelta(days=1)
        if e_day <= s_day:
            e_day = s_day + timedelta(days=1)
        start, end, all_day = _midnight(s_day), _midnight(e_day), True
    else:
        start = _local(start_raw)
        if end_raw is not None:
            end = _local(end_raw) if _is_datetime(end_raw) else _midnight(end_raw)
        elif isinstance(dur, timedelta):
            end = _local(start_raw + dur)
        else:
            end = start
        end = max(end, start)
        all_day = False
    uid = str(ev.get("UID") or "") or _text(ev, "SUMMARY")
    digest = hashlib.sha1(f"{cal_name}|{uid}|{start.isoformat()}".encode()).hexdigest()[:10]
    return _event(
        id=f"ics-{digest}",
        title=_text(ev, "SUMMARY"),
        start=start,
        end=end,
        all_day=all_day,
        calendar=cal_name,
        source="ics",
        location=_text(ev, "LOCATION"),
        description=_text(ev, "DESCRIPTION"),
        busy=str(ev.get("TRANSP", "OPAQUE")).upper() != "TRANSPARENT",
        tentative=str(ev.get("STATUS", "")).upper() == "TENTATIVE",
    )


def _expand_feed(query, cal_name: str, start: datetime, end: datetime, me: set[str]) -> list[dict]:
    out = []
    for ev in query.between(start, end):
        try:  # one odd event (bad dates, unexpected value types) mustn't hide the rest of the feed
            if str(ev.get("STATUS", "")).upper() == "CANCELLED" or _attendee_declined(ev, me):
                continue
            item = _feed_event(ev, cal_name)
        except Exception:
            continue
        if item and _overlaps(item, start, end):
            out.append(item)
    return out


async def _feed_events(ctx, spec: dict, start: datetime, end: datetime) -> tuple[list[dict], str, str | None]:
    query, name, err = await _get_feed(ctx, spec)
    if query is None:
        return [], name, err
    me = set(spec["me"])
    if getattr(ctx.settings, "email_address", ""):
        me.add(ctx.settings.email_address.strip().lower())
    try:
        events = await asyncio.to_thread(_expand_feed, query, name, start, end, me)
    except Exception as exc:
        return [], name, f"Couldn't read the events in '{name}': {exc.__class__.__name__}."
    return events, name, err


# ---------------------------------------------------------- local calendar
def _local_event(item: dict) -> dict | None:
    try:
        if item.get("all_day"):
            s_day = date.fromisoformat(str(item["start"])[:10])
            e_day = date.fromisoformat(str(item.get("end") or item["start"])[:10])  # inclusive
            start, end = _midnight(s_day), _midnight(max(e_day, s_day) + timedelta(days=1))
        else:
            start = _local(datetime.fromisoformat(str(item["start"])))
            end = _local(datetime.fromisoformat(str(item.get("end") or item["start"])))
    except (KeyError, ValueError, TypeError):
        return None
    attendees = [a.get("email") or a.get("name") for a in item.get("attendees") or [] if isinstance(a, dict)]
    return _event(
        id=str(item.get("id", "")),
        title=str(item.get("title", "")),
        start=start,
        end=max(end, start),
        all_day=bool(item.get("all_day")),
        calendar=LOCAL_CALENDAR_NAME,
        source="local",
        location=str(item.get("location") or ""),
        description=str(item.get("description") or ""),
        attendees=[a for a in attendees if a],
    )


def _local_events() -> list[dict]:
    return [e for e in (_local_event(i) for i in _load("calendar")) if e]


def _dedupe_key(ev: dict) -> tuple[str, str]:
    return (re.sub(r"\s+", " ", ev["title"]).strip().lower(), ev["_start"].strftime("%Y%m%d%H%M"))


async def collect_events(ctx, start: datetime, end: datetime) -> tuple[list[dict], list[str], list[str]]:
    """All events overlapping [start, end): (internal events sorted, calendar names, errors)."""
    specs = _feed_specs()
    wanted = {s["url"] for s in specs}
    for url in [u for u in (*_FEEDS, *_FAILED) if u not in wanted]:  # addresses removed from Settings
        _FEEDS.pop(url, None)
        _FAILED.pop(url, None)
    results = await asyncio.gather(*(_feed_events(ctx, s, start, end) for s in specs))
    events: list[dict] = []
    names: list[str] = []
    errors: list[str] = []
    shown: dict[tuple, int] = {}  # the same invitation can sit in two connected calendars
    for feed_no, (evs, name, err) in enumerate(results):
        for ev in evs:
            key = (*_dedupe_key(ev), ev["_end"].strftime("%Y%m%d%H%M"))
            if shown.setdefault(key, feed_no) == feed_no:
                events.append(ev)
        if name and name not in names:
            names.append(name)
        if err:
            errors.append(err)
    # A local event the user has since saved into a synced calendar shows up in its feed too.
    seen = {_dedupe_key(e) for e in events}
    for ev in _local_events():
        if _overlaps(ev, start, end) and _dedupe_key(ev) not in seen:
            events.append(ev)
    names.append(LOCAL_CALENDAR_NAME)
    events.sort(key=lambda e: (e["_start"], not e["all_day"], e["_end"], e["title"].lower()))
    return events, names, errors


def _matches(ev: dict, query: str) -> bool:
    hay = " ".join([ev["title"], ev["location"], ev["description"], ev["calendar"]]).lower()
    return all(word in hay for word in query.lower().split())


_SETUP_HINT = (
    "No calendar feeds are connected, so only events ATLAS created are shown. To read the user's "
    "Google/Outlook/iCloud calendar, add its secret iCal (ICS) address in Settings (CALENDAR_ICS_URLS)."
)


# ------------------------------------------------------------------ tools
@tool(
    "get_calendar_events",
    "Read the user's calendar: events from their connected Google/Outlook/iCloud calendars plus events "
    "ATLAS added, merged and sorted, with recurring events expanded and times in local time. Use for "
    "'what's on my schedule', 'when is my dentist appointment', daily briefings. All-day events have "
    "plain dates (end date inclusive). To find a specific event further ahead, pass query and a larger "
    "days (max 60).",
    {
        "start_date": {"type": "string", "description": "First day, ISO date (YYYY-MM-DD). Default today."},
        "days": {"type": "integer", "description": "How many days to cover, 1-60. Default 7."},
        "query": {"type": "string", "description": "Only events whose title/location/notes contain these words."},
    },
    label="Checking your calendar",
)
async def get_calendar_events(ctx, start_date: str | None = None, days: int | None = 7, query: str | None = None):
    first = _parse_when(start_date, "start_date") if start_date else _now().date()
    if isinstance(first, datetime):
        first = first.date()
    try:
        n_days = max(1, min(int(days or 7), MAX_DAYS))
    except (TypeError, ValueError):
        n_days = 7
    start, end = _midnight(first), _midnight(first + timedelta(days=n_days))
    events, names, errors = await collect_events(ctx, start, end)
    if query and query.strip():
        events = [e for e in events if _matches(e, query)]
    total = len(events)
    shown = events[:MAX_EVENTS]
    result: dict[str, Any] = {
        "status": f"{total} event{'s' if total != 1 else ''}",
        "range": {"from": first.isoformat(), "to": (first + timedelta(days=n_days - 1)).isoformat(), "days": n_days},
        "events": [_public(e, with_description=total <= 20 or bool(query)) for e in shown],
        "calendars": names,
    }
    if total > len(shown):
        result["truncated"] = f"Showing the first {len(shown)} of {total}; use fewer days or a query."
    if errors:
        result["errors"] = errors
    if len(names) == 1:  # only the local calendar
        result["setup"] = _SETUP_HINT
    return result


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _gaps(window: tuple[datetime, datetime], busy: list[tuple[datetime, datetime]], min_minutes: int):
    ws, we = window
    gaps, cursor = [], ws
    for s, e in _merge([(max(s, ws), min(e, we)) for s, e in busy if e > ws and s < we]):
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if we > cursor:
        gaps.append((cursor, we))
    return [(s, e) for s, e in gaps if (e - s) >= timedelta(minutes=min_minutes)]


def _slot(s: datetime, e: datetime) -> dict:
    return {
        "start": s.isoformat(timespec="minutes"),
        "end": e.isoformat(timespec="minutes"),
        "when": _when(s, e, False),
        "minutes": int((e - s).total_seconds() // 60),
    }


def _round_up(dt: datetime, minutes: int = 15) -> datetime:
    dt = dt.replace(second=0, microsecond=0) + (timedelta(minutes=1) if dt.second or dt.microsecond else timedelta())
    extra = (-dt.minute) % minutes
    return dt + timedelta(minutes=extra)


@tool(
    "check_availability",
    "Check whether the user is free at a time BEFORE proposing, booking or scheduling anything (meetings, "
    "restaurant reservations, appointments, calls). Returns conflicting events, the free slots that day "
    "(within the user's usual day hours, widened to include the requested time, e.g. a late dinner) and the "
    "nearest free alternatives of the same length. Pass only a "
    "date (YYYY-MM-DD) as start to list the whole day's free slots. Times are local unless an offset is given.",
    {
        "start": {"type": "string", "description": "ISO local start, e.g. 2026-11-05T19:30, or a date for the whole day."},
        "end": {"type": "string", "description": "ISO local end. Default: one hour after start."},
    },
    ["start"],
    label="Checking availability",
)
async def check_availability(ctx, start: str, end: str | None = None):
    first = _parse_when(start, "start")
    h0, h1 = _day_hours()
    whole_day = not _is_datetime(first)
    if whole_day:
        day = first
        req_start, req_end = _at_minutes(day, h0), _at_minutes(day, h1)
        need = 30
    else:
        day = first.date()
        req_start = first
        if end:
            last = _parse_when(end, "end")
            req_end = last if _is_datetime(last) else _midnight(last + timedelta(days=1))
        else:
            req_end = req_start + timedelta(minutes=DEFAULT_DURATION)
        if req_end <= req_start:
            raise ToolError("end must be after start.")
        if req_end - req_start > timedelta(days=14):
            raise ToolError("Check at most two weeks at a time.")
        need = max(5, int((req_end - req_start).total_seconds() // 60))

    win_start, win_end = _at_minutes(day, h0), _at_minutes(day, h1)
    if not whole_day:  # a late dinner or early flight: also search the hours around the request
        win_start = min(win_start, max(req_start, _midnight(day)))
        win_end = max(win_end, min(req_end, _midnight(day + timedelta(days=1))))
    span_start = min(_midnight(day), req_start)
    span_end = max(_midnight(day + timedelta(days=1)), req_end)
    events, _names, errors = await collect_events(ctx, span_start, span_end)

    timed_busy = [e for e in events if e["_busy"] and not e["all_day"] and e["_end"] > e["_start"]]
    conflicts = [e for e in timed_busy if _overlaps(e, req_start, req_end)]
    all_day = [e for e in events if e["all_day"] and _overlaps(e, _midnight(day), _midnight(day + timedelta(days=1)))]

    now = _now()
    today = now.date()
    if day < today:
        free_slots, note = [], "That day is in the past."
    else:
        if day == today:
            win_start = max(win_start, _round_up(now))
        busy = [(e["_start"], e["_end"]) for e in timed_busy]
        free_slots = _gaps((win_start, win_end), busy, need) if win_end > win_start else []
        note = None

    result: dict[str, Any] = {
        "requested": _slot(req_start, req_end) if not whole_day else {"date": day.isoformat(), "when": _day_label(day)},
        "free": not conflicts,
        "conflicts": [_public(e, with_description=False) for e in conflicts],
        "free_slots": [_slot(s, e) for s, e in free_slots[:10]],
        "day_hours": f"{h0 // 60:02d}:{h0 % 60:02d}-{h1 // 60:02d}:{h1 % 60:02d}",
    }
    if whole_day:
        result["status"] = f"{len(free_slots)} free slot{'s' if len(free_slots) != 1 else ''}"
    else:
        result["status"] = "free" if not conflicts else f"busy ({len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''})"
        if conflicts and free_slots:
            length = req_end - req_start
            options = []
            for s, e in free_slots:
                cand = min(max(req_start, s), e - length)
                options.append((abs((cand - req_start).total_seconds()), cand))
            result["suggestions"] = [_slot(c, c + length) for _d, c in sorted(options)[:3]]
    if all_day:
        result["all_day_events"] = [_public(e, with_description=False) for e in all_day]
    if note:
        result["note"] = note
    if errors:
        result["errors"] = errors
    if len(_names) == 1:
        result["setup"] = _SETUP_HINT
    return result


# ------------------------------------------------------- creating events
def _provider() -> str:
    raw = _setting("CALENDAR_PROVIDER", "google").strip().lower()
    raw = _PROVIDER_ALIASES.get(raw, raw)
    return raw if raw in PROVIDERS else "google"


def _fit_details(make, details: str) -> str:
    """make(details) -> URL. Keep as much of details as fits in MAX_LINK_CHARS (the full notes stay in
    ATLAS's own record); over-long links fail to open on Windows and get rejected by web servers."""
    url = make(details)
    if len(url) <= MAX_LINK_CHARS or not details:
        return url
    lo, hi = 0, len(details) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(make(clip(details, mid))) <= MAX_LINK_CHARS:
            lo = mid
        else:
            hi = mid - 1
    return make(clip(details, lo) if lo > 1 else "")


def _q(params: dict) -> str:
    return urlencode(params, quote_via=quote, safe="/")


def google_calendar_link(title: str, start: datetime | date, end: datetime | date, details: str = "",
                         location: str = "", guests: list[str] | None = None) -> str:
    """Prefilled Google Calendar 'new event' page. For all-day events pass dates with an EXCLUSIVE end."""
    if _is_datetime(start):
        dates = f"{_utc(start):%Y%m%dT%H%M%SZ}/{_utc(end):%Y%m%dT%H%M%SZ}"
    else:
        dates = f"{start:%Y%m%d}/{end:%Y%m%d}"

    def make(text: str) -> str:
        params = {"action": "TEMPLATE", "text": clip(title, 300), "dates": dates}
        if text:
            params["details"] = text
        if location:
            params["location"] = clip(location, 300)
        if guests:
            params["add"] = ",".join(guests[:50])
        return "https://calendar.google.com/calendar/render?" + _q(params)

    return _fit_details(make, clip(details, 1500))


def outlook_calendar_link(title: str, start: datetime | date, end: datetime | date, details: str = "",
                          location: str = "", office365: bool = False) -> str:
    """Prefilled Outlook on the web compose page (outlook.live.com, or outlook.office.com for work accounts)."""
    base = "https://outlook.office.com" if office365 else "https://outlook.live.com"
    if _is_datetime(start):
        when = {"startdt": f"{_utc(start):%Y-%m-%dT%H:%M:%SZ}", "enddt": f"{_utc(end):%Y-%m-%dT%H:%M:%SZ}"}
    else:
        when = {"startdt": start.isoformat(), "enddt": end.isoformat(), "allday": "true"}

    def make(text: str) -> str:
        params = {"path": "/calendar/action/compose", "rru": "addevent", "subject": clip(title, 300), **when}
        if text:
            params["body"] = text
        if location:
            params["location"] = clip(location, 300)
        return f"{base}/calendar/0/deeplink/compose?" + _q(params)

    return _fit_details(make, clip(details, 1500))


def build_ics(uid: str, title: str, start: datetime | date, end: datetime | date, description: str = "",
              location: str = "") -> bytes:
    """A single-event iCalendar file. All-day events take dates with an EXCLUSIVE end."""
    if icalendar is None:
        raise ToolError("Writing .ics files needs: pip install icalendar")
    cal = icalendar.Calendar()
    cal.add("prodid", "-//ATLAS//Personal Assistant//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    ev = icalendar.Event()
    ev.add("uid", uid)
    ev.add("dtstamp", datetime.now(timezone.utc).replace(microsecond=0))
    if _is_datetime(start):
        ev.add("dtstart", _utc(start).replace(microsecond=0))
        ev.add("dtend", _utc(end).replace(microsecond=0))
    else:
        ev.add("dtstart", start)
        ev.add("dtend", end)
    ev.add("summary", title)
    if location:
        ev.add("location", location)
    if description:
        ev.add("description", description)
    cal.add_component(ev)
    return cal.to_ical()


def _has_desktop() -> bool:
    """Without a display, webbrowser falls back to console browsers (lynx, w3m) that take over the
    terminal and block until they exit, and xdg-open has nothing to open files with."""
    return IS_WIN or IS_MAC or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_url(url: str) -> bool:
    if not url.startswith("https://") or not _has_desktop():
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _open_file(path: Path) -> bool:
    if not _has_desktop():
        return False
    try:
        if IS_WIN:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MAC:
            return subprocess.run(["open", str(path)], capture_output=True, timeout=15).returncode == 0
        else:
            if not shutil.which("xdg-open"):
                return False
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        return True
    except Exception:
        return False


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40].lower() or "event"


def _write_export(name: str, data: bytes) -> Path:
    folder = DATA_DIR / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 14 * 86400
    for old in folder.glob("*.ics"):  # these are hand-offs, not storage
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    path = folder / name
    path.write_bytes(data)
    return path


_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _resolve_attendees(items) -> tuple[list[dict], list[str]]:
    if isinstance(items, str):
        items = re.split(r"[,;]", items)
    people, unresolved = [], []
    for raw in items or []:
        text = str(raw).strip()
        if not text:
            continue
        m = _EMAIL.search(text)
        if m:
            name = text.replace(m.group(0), "").strip(" <>\"'()")
            people.append({"name": name, "email": m.group(0)})
            continue
        c = find_contact(text)
        if c and c.get("email"):
            people.append({"name": c["name"], "email": c["email"]})
        else:
            people.append({"name": text, "email": ""})
            unresolved.append(text)
    return people, unresolved


@tool(
    "add_calendar_event",
    "Put an event on the user's calendar: a meeting, appointment, call, trip, or a confirmed restaurant "
    "or other reservation. Saves it to ATLAS's calendar and opens a prefilled new-event page (Google "
    "Calendar or Outlook) or an .ics file in their calendar app — the user must click Save there for it to "
    "reach that calendar, so tell them. Times are local unless an offset is given. Give end or "
    "duration_minutes (default 60); a date-only start (YYYY-MM-DD) makes an all-day event. Attendees may "
    "be email addresses or saved contact names. Check availability first when the time is flexible.",
    {
        "title": {"type": "string"},
        "start": {"type": "string", "description": "ISO local start, e.g. 2026-11-05T19:30, or YYYY-MM-DD for all day."},
        "end": {"type": "string", "description": "ISO local end (for all-day events: last day, inclusive)."},
        "duration_minutes": {"type": "integer", "description": "Used when end is omitted. Default 60."},
        "location": {"type": "string"},
        "description": {"type": "string", "description": "Notes, e.g. confirmation number, party size, dial-in."},
        "attendees": {"type": "array", "items": {"type": "string"}, "description": "Emails or contact names."},
    },
    ["title", "start"],
    label="Adding to calendar",
)
async def add_calendar_event(ctx, title: str, start: str, end: str | None = None, duration_minutes: int | None = None,
                             location: str | None = None, description: str | None = None, attendees=None):
    title = clip(re.sub(r"\s+", " ", title or "").strip(), 300)
    if not title:
        raise ToolError("Give the event a title.")
    first = _parse_when(start, "start")
    all_day = not _is_datetime(first)
    explicit_end = bool(end) or bool(duration_minutes)
    if all_day:
        last = _parse_when(end, "end") if end else first
        last = last.date() if _is_datetime(last) else last
        if last < first:
            raise ToolError("The end date is before the start date.")
        if (last - first).days > 31:
            raise ToolError("All-day events can span at most a month.")
    else:
        if end:
            last = _parse_when(end, "end")
            if not _is_datetime(last):
                raise ToolError("Give end as a date-time (e.g. 2026-11-05T21:00), or use duration_minutes.")
        else:
            try:
                minutes = int(duration_minutes) if duration_minutes else DEFAULT_DURATION
            except (TypeError, ValueError, OverflowError):
                raise ToolError("duration_minutes must be a number.")
            if minutes <= 0:
                raise ToolError("duration_minutes must be positive.")
            if minutes > 14 * 24 * 60:
                raise ToolError("That event is longer than two weeks — check the duration.")
            last = first + timedelta(minutes=minutes)
        if last <= first:
            raise ToolError("The event must end after it starts.")
        if last - first > timedelta(days=14):
            raise ToolError("That event is longer than two weeks — check the end time.")

    people, unresolved = _resolve_attendees(attendees)
    location = clip((location or "").strip(), 500)
    description = clip((description or "").strip(), 8000)
    provider = _provider()

    # The same event asked for twice (a retry, or a correction that adds details) updates ATLAS's copy,
    # and the hand-off below carries the merged details under the same id (.ics UID).
    key_start = _midnight(first) if all_day else first
    new_key = (re.sub(r"\s+", " ", title).strip().lower(), key_start.strftime("%Y%m%d%H%M"))
    with _LOCK:
        same = next((dict(i) for i in _load("calendar")
                     if (e := _local_event(i)) and _dedupe_key(e) == new_key), None)
    if same is not None:
        old = _local_event(same)
        if not explicit_end and old and old["all_day"] == all_day:
            last = (old["_end"] - timedelta(days=1)).date() if all_day else old["_end"]
        location = location or str(same.get("location") or "")
        description = description or str(same.get("description") or "")
        if not people and isinstance(same.get("attendees"), list):
            people = [p for p in same["attendees"] if isinstance(p, dict)]
            unresolved = [str(p.get("name")) for p in people if not p.get("email") and p.get("name")]
    if all_day:
        last = max(last, first)
        start_v, end_v = first, last + timedelta(days=1)  # exclusive for links and .ics
        stored_start, stored_end = first.isoformat(), last.isoformat()
        win = (_midnight(first), _midnight(end_v))
    else:
        start_v, end_v = first, last
        stored_start, stored_end = first.isoformat(timespec="minutes"), last.isoformat(timespec="minutes")
        win = (first, last)

    # Mention clashes so the assistant can flag them (best effort: never blocks saving). The same
    # event already on a calendar (asked twice, or already saved in Google) isn't a clash.
    conflicts: list[dict] = []
    try:
        evs, _n, _e = await asyncio.wait_for(collect_events(ctx, *win), timeout=12)
        conflicts = [_public(e, with_description=False) for e in evs
                     if e["_busy"] and (all_day or not e["all_day"]) and _overlaps(e, *win)
                     and _dedupe_key(e) != new_key][:10]
    except Exception:
        pass

    event_id = str(same.get("id")) if same is not None and same.get("id") else uuid.uuid4().hex[:8]
    record = {
        "id": event_id,
        "title": title,
        "start": stored_start,
        "end": stored_end,
        "all_day": all_day,
        "location": location,
        "description": description,
        "attendees": people,
        "provider": provider,
        "created": str((same or {}).get("created") or _now().isoformat(timespec="minutes")),
    }

    with_line = ", ".join(p.get("email") or p.get("name") or "" for p in people)
    details = description
    guests = [p["email"] for p in people if p.get("email")]
    result: dict[str, Any] = {"provider": provider}
    if provider == "google":
        if unresolved:
            details = (details + "\n\n" if details else "") + "With: " + ", ".join(unresolved)
        link = google_calendar_link(title, start_v, end_v, details, location, guests)
        opened = await asyncio.to_thread(_open_url, link)
        record["link"] = link
        result.update(link=link, opened=opened)
        result["next_step"] = (
            "Google Calendar is open in the browser with the event filled in; the user needs to click Save."
            if opened else "Couldn't open a browser; give the user the link to open and click Save."
        )
    elif provider in ("outlook", "office365"):
        if with_line:
            details = (details + "\n\n" if details else "") + "With: " + with_line
        link = outlook_calendar_link(title, start_v, end_v, details, location, office365=provider == "office365")
        opened = await asyncio.to_thread(_open_url, link)
        record["link"] = link
        result.update(link=link, opened=opened)
        result["next_step"] = (
            "Outlook is open in the browser with the event filled in; the user needs to click Save."
            + (" Invitees must be added there." if people else "")
            if opened else "Couldn't open a browser; give the user the link to open and click Save."
        )
    elif provider == "ics":
        if with_line:
            details = (details + "\n\n" if details else "") + "With: " + with_line
        data = build_ics(f"{event_id}@atlas.local", title, start_v, end_v, details, location)
        try:
            path = await asyncio.to_thread(_write_export, f"{_slug(title)}-{event_id}.ics", data)
        except OSError as exc:
            raise ToolError(f"Couldn't write the .ics file in ATLAS's data folder: {exc.strerror or exc}")
        opened = await asyncio.to_thread(_open_file, path)
        record["file"] = str(path)
        result.update(file=str(path), opened=opened)
        result["next_step"] = (
            "The event opened in the user's default calendar app; they need to click Save/Add."
            + (" Invitees must be added there." if people else "")
            if opened else f"Couldn't open the calendar app; the user can double-click {path} to import it."
        )
    else:
        result["next_step"] = "Saved in ATLAS's calendar only (CALENDAR_PROVIDER is 'local')."

    with _LOCK:
        items = _load("calendar")
        index = next((n for n, i in enumerate(items) if same is not None and str(i.get("id")) == event_id), None)
        if index is None:
            items.append(record)
        else:
            items[index] = record
        _save("calendar", items[-1000:])
    ev = _local_event(record)
    saved = "already in ATLAS calendar (updated)" if same is not None else "added to ATLAS calendar"
    app = {"google": "Google Calendar", "outlook": "Outlook", "office365": "Outlook", "ics": "calendar app"}.get(provider)
    result["status"] = saved + (f"; {app} opened — click Save" if app and result.get("opened") else "")
    result["event"] = _public(ev) if ev else record
    if conflicts:
        result["conflicts"] = conflicts
    if unresolved:
        result["unresolved_attendees"] = unresolved
    if not all_day and first < _now() - timedelta(minutes=1):
        result["warning"] = "That start time is in the past."
    return result


@tool(
    "delete_calendar_event",
    "Delete an event ATLAS added (source 'local') by its id from get_calendar_events. Events that come from "
    "the user's Google/Outlook/iCloud calendars are read-only here; they must delete those in that app.",
    {"id": {"type": "string"}},
    ["id"],
    label="Removing calendar event",
)
async def delete_calendar_event(ctx, id: str):
    key = (id or "").strip()
    if key.startswith("ics-"):
        raise ToolError("That event is from the user's synced calendar, which ATLAS can only read; "
                        "delete it in Google Calendar/Outlook/iCloud instead.")
    with _LOCK:
        items = _load("calendar")
        gone = [e for e in items if str(e.get("id")) == key]
        if not gone:
            raise ToolError(f"No ATLAS calendar event with id '{key}'. Use get_calendar_events to find the id.")
        _save("calendar", [e for e in items if str(e.get("id")) != key])
    ev = gone[0]
    result: dict[str, Any] = {"status": f"deleted '{ev.get('title', '')}'", "removed": len(gone)}
    if ev.get("provider") in ("google", "outlook", "office365", "ics"):
        result["note"] = "If the user saved it in their calendar app too, they need to delete it there as well."
    return result


# ------------------------------------------------------------------ to-dos
def _due_parts(value) -> tuple[str, datetime | None]:
    """Normalise a due value -> (stored string, comparable deadline)."""
    if value in (None, ""):
        return "", None
    when = _parse_when(value, "due")
    if _is_datetime(when):
        return when.isoformat(timespec="minutes"), when
    return when.isoformat(), _midnight(when + timedelta(days=1)) - timedelta(seconds=1)  # end of that day


def _deadline(task: dict) -> datetime | None:
    try:
        return _due_parts(task.get("due"))[1]
    except ToolError:
        return None


def _task_key(t: dict):
    due = _deadline(t)
    return (
        bool(t.get("done")),
        due is None,
        due.timestamp() if due else 0.0,
        PRIORITIES.get(t.get("priority"), 1),
        str(t.get("created", "")),
    )


def _task_public(t: dict, now: datetime | None = None) -> dict:
    now = now or _now()
    out = {"id": t["id"], "title": t["title"], "priority": t.get("priority", "normal"), "done": bool(t.get("done"))}
    due = _deadline(t)
    if t.get("due"):
        out["due"] = t["due"]
        if due:
            out["due_human"] = _day_label(due.date()) + ("" if _DATE_ONLY.fullmatch(t["due"]) else f", {_clock(due)}")
        if due and not t.get("done") and due < now:
            out["overdue"] = True
    if t.get("notes"):
        out["notes"] = t["notes"]
    if t.get("done") and t.get("completed"):
        out["completed"] = t["completed"]
    return out


def _load_tasks() -> list[dict]:
    """tasks.json, with hand-edited or damaged entries repaired (no title: dropped; no id: given one)."""
    out = []
    for t in _load("tasks"):
        title = re.sub(r"\s+", " ", str(t.get("title") or "")).strip()
        if not title:
            continue
        t["title"] = title
        t["id"] = str(t.get("id") or uuid.uuid4().hex[:8])
        t["due"] = str(t.get("due") or "")
        t["notes"] = str(t.get("notes") or "")
        if t.get("priority") not in PRIORITIES:
            t["priority"] = _priority(t.get("priority"))
        t["done"] = _truthy(t.get("done"))
        out.append(t)
    return out


def _truthy(value) -> bool:
    if isinstance(value, str):  # the model sometimes sends "false"
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def tasks_sorted() -> list[dict]:
    return sorted(_load_tasks(), key=_task_key)


def _find_task(items: list[dict], key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise ToolError("Say which task (its id or part of its title).")
    for t in items:
        if t.get("id") == key:
            return t
    low = key.lower()
    for pool in ([t for t in items if not t.get("done")], items):
        exact = [t for t in pool if t["title"].strip().lower() == low]
        if len(exact) == 1:
            return exact[0]
        partial = [t for t in pool if low in t["title"].lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = "; ".join(f"{t['title']} (id {t['id']})" for t in partial[:6])
            raise ToolError(f"Several tasks match '{key}': {names}. Use the id.")
    raise ToolError(f"No task matches '{key}'. Use list_tasks to see them.")


def _priority(value) -> str:
    p = str(value or "").strip().lower()
    p = _PRIORITY_ALIASES.get(p, p)
    return p if p in PRIORITIES else "normal"


@tool(
    "add_task",
    "Add an item to the user's to-do list ('add renew passport to my to-dos', 'I need to call the plumber "
    "this week'). For an alert at a specific time use set_reminder instead (or as well).",
    {
        "title": {"type": "string"},
        "due": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD) or local date-time."},
        "priority": {"type": "string", "enum": ["low", "normal", "high"], "description": "Default normal."},
        "notes": {"type": "string"},
    },
    ["title"],
    label="Adding to-do",
)
async def add_task(ctx, title: str, due: str | None = None, priority: str | None = None, notes: str | None = None):
    title = clip(re.sub(r"\s+", " ", title or "").strip(), 300)
    if not title:
        raise ToolError("Give the task a title.")
    due_str, _deadline_dt = _due_parts(due)
    notes = clip((notes or "").strip(), 4000)
    with _LOCK:
        items = _load_tasks()
        for t in items:
            if not t.get("done") and t["title"].lower() == title.lower():
                return {"status": "already on the list", "task": _task_public(t)}
        task = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "due": due_str,
            "priority": _priority(priority),
            "notes": notes,
            "done": False,
            "created": _now().isoformat(timespec="seconds"),
        }
        items.append(task)
        open_items = [t for t in items if not t.get("done")]
        done_items = sorted((t for t in items if t.get("done")), key=lambda t: str(t.get("completed", "")))[-200:]
        _save("tasks", open_items + done_items)
    return {"status": "added", "task": _task_public(task)}


@tool(
    "list_tasks",
    "List the user's to-do items: open tasks first, ordered by due date then priority, with overdue ones "
    "flagged. Set include_done to also show completed tasks; query filters by text.",
    {
        "include_done": {"type": "boolean", "description": "Also list completed tasks. Default false."},
        "query": {"type": "string"},
    },
    label="Reading to-do list",
)
async def list_tasks(ctx, include_done: bool = False, query: str | None = None):
    now = _now()
    items = tasks_sorted()
    open_count = sum(1 for t in items if not t.get("done"))
    overdue = sum(1 for t in items if not t.get("done") and (_deadline(t) or now) < now)
    if not _truthy(include_done):
        items = [t for t in items if not t.get("done")]
    if query and query.strip():
        words = query.lower().split()
        items = [t for t in items if all(w in f"{t['title']} {t.get('notes', '')}".lower() for w in words)]
    return {
        "status": f"{open_count} open task{'s' if open_count != 1 else ''}" + (f", {overdue} overdue" if overdue else ""),
        "tasks": [_task_public(t, now) for t in items[:100]],
        "open": open_count,
        "overdue": overdue,
    }


@tool(
    "complete_task",
    "Mark a to-do item as done. id may be the task id or a unique part of its title.",
    {"id": {"type": "string"}},
    ["id"],
    label="Completing to-do",
)
async def complete_task(ctx, id: str):
    with _LOCK:
        items = _load_tasks()
        task = _find_task(items, id)
        if task.get("done"):
            return {"status": "already done", "task": _task_public(task)}
        task["done"] = True
        task["completed"] = _now().isoformat(timespec="seconds")
        _save("tasks", items)
    remaining = sum(1 for t in items if not t.get("done"))
    return {"status": f"done: {task['title']}", "task": _task_public(task), "open_remaining": remaining}


@tool(
    "delete_task",
    "Permanently remove a to-do item (use complete_task if it was done). id may be the task id or a unique "
    "part of its title.",
    {"id": {"type": "string"}},
    ["id"],
    label="Removing to-do",
)
async def delete_task(ctx, id: str):
    with _LOCK:
        items = _load_tasks()
        task = _find_task(items, id)
        _save("tasks", [t for t in items if t is not task])
    return {"status": f"deleted: {task['title']}", "removed": 1}


# ------------------------------------------------------------------- panel
async def agenda_for_panel(ctx, days: int = 2) -> dict:
    """Upcoming events (now → end of the given number of days) and open to-dos, for a UI panel. Never raises."""
    now = _now()
    try:
        n_days = max(1, min(int(days or 2), 14))
    except (TypeError, ValueError, OverflowError):
        n_days = 2
    errors: list[str] = []
    events: list[dict] = []
    feeds = False
    try:
        feeds = bool(_feed_specs())
        found, _names, errors = await collect_events(ctx, now, _midnight(now.date() + timedelta(days=n_days)))
        events = [_public(e, with_description=False) for e in found[:MAX_EVENTS]]
    except Exception as exc:  # the class name only: a message could quote a feed's secret address
        errors = [f"Calendar unavailable ({exc.__class__.__name__})."]
    tasks: list[dict] = []
    open_count = 0
    try:
        open_tasks = [t for t in tasks_sorted() if not t.get("done")]
        open_count = len(open_tasks)
        tasks = [_task_public(t, now) for t in open_tasks[:15]]
    except Exception as exc:
        errors.append(f"To-do list unavailable ({exc.__class__.__name__}).")
    return {
        "now": now.isoformat(timespec="minutes"),
        "days": n_days,
        "events": events,
        "tasks": tasks,
        "open_tasks": open_count,
        "feeds_connected": feeds,
        "errors": errors,
    }
