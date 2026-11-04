"""Calendar (ICS feeds + local calendar) and to-do list tools. Fully offline: feeds come from MockTransport."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.tools import TOOLS, call_tool, calendar_tools as ct, memory
from conftest import Router, make_ctx, mock_http, no_network, run

# The real launchers, before the autouse fixture below swaps them for recorders.
REAL_OPEN_URL, REAL_OPEN_FILE = ct._open_url, ct._open_file

icalendar = pytest.importorskip("icalendar")
pytest.importorskip("recurring_ical_events")

SECRET = "private-0123456789abcdef"
GOOGLE_URL = f"https://calendar.google.com/calendar/ical/me%40example.com/{SECRET}/basic.ics"
OTHER_URL = f"https://p01-caldav.icloud.com/published/2/{SECRET}"

# All dates are in 2030 so "today" never interferes. US DST ends Sun 2030-11-03, UK DST Sun 2030-10-27.
FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Fixture//EN
X-WR-CALNAME:Work
X-WR-TIMEZONE:America/New_York
BEGIN:VEVENT
UID:standup-1
DTSTART;TZID=America/New_York:20301028T090000
DTEND;TZID=America/New_York:20301028T093000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=5
EXDATE;TZID=America/New_York:20301111T090000
SUMMARY:Team standup
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
UID:standup-1
RECURRENCE-ID;TZID=America/New_York:20301118T090000
DTSTART;TZID=America/New_York:20301118T090000
DTEND;TZID=America/New_York:20301118T093000
STATUS:CANCELLED
SUMMARY:Team standup
END:VEVENT
BEGIN:VEVENT
UID:standup-1
RECURRENCE-ID;TZID=America/New_York:20301125T090000
DTSTART;TZID=America/New_York:20301125T100000
DTEND;TZID=America/New_York:20301125T103000
SUMMARY:Team standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:offsite
DTSTART;VALUE=DATE:20301105
DTEND;VALUE=DATE:20301107
SUMMARY:Company offsite
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:london-call
DTSTART;TZID=Europe/London:20301030T150000
DTEND;TZID=Europe/London:20301030T160000
SUMMARY:London call
END:VEVENT
BEGIN:VEVENT
UID:outlook-style
DTSTART;TZID=Eastern Standard Time:20301106T090000
DTEND;TZID=Eastern Standard Time:20301106T100000
SUMMARY:Outlook style
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1
DTSTART:20301105T200000Z
DTEND:20301105T210000Z
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
BEGIN:VEVENT
UID:declined-1
DTSTART:20301105T220000Z
DTEND:20301105T230000Z
SUMMARY:Declined meeting
ATTENDEE;CN=Me;PARTSTAT=DECLINED:mailto:me@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:boss@example.com
END:VEVENT
BEGIN:VEVENT
UID:lunch-1
DTSTART:20301105T200000Z
DTEND:20301105T210000Z
SUMMARY:Lunch with Sam
DESCRIPTION:<b>Bring</b> the deck<br>Table for 2
END:VEVENT
BEGIN:VEVENT
UID:float-1
DTSTART:20301105T160000
DURATION:PT30M
SUMMARY:Floating focus time
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").encode()


def ics_response(body: bytes = FEED, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"content-type": "text/calendar; charset=utf-8"})


@pytest.fixture(autouse=True)
def calendar_env(isolated, monkeypatch):
    """Redirect this module's data dir, clear its config/cache and capture anything it would open."""
    monkeypatch.setattr(ct, "DATA_DIR", isolated["data_dir"])
    for key in ("CALENDAR_ICS_URLS", "CALENDAR_PROVIDER", "CALENDAR_DAY_HOURS"):
        monkeypatch.delenv(key, raising=False)
    opened: dict[str, list] = {"urls": [], "files": []}
    monkeypatch.setattr(ct, "_open_url", lambda url: opened["urls"].append(url) or True)
    monkeypatch.setattr(ct, "_open_file", lambda path: opened["files"].append(path) or True)
    ct._FEEDS.clear()
    ct._FAILED.clear()
    yield opened
    ct._FEEDS.clear()
    ct._FAILED.clear()


@pytest.fixture(autouse=True)
def pacific_time():
    """Pin the local zone so conversions are deterministic (and cross a DST change)."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset (POSIX)")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def call(_name, _ctx=None, **args):
    return json.loads(run(call_tool(_name, args, _ctx or make_ctx(http=mock_http(no_network)))))


def feed_ctx(monkeypatch, routes=None, urls=GOOGLE_URL, **settings):
    monkeypatch.setenv("CALENDAR_ICS_URLS", urls)
    router = Router(routes if routes is not None else {"calendar.google.com": ics_response()})
    return make_ctx(http=mock_http(router), **settings), router


def titles(result):
    return [e["title"] for e in result["events"]]


# -------------------------------------------------------------- registration
def test_tools_registered_with_valid_schemas():
    names = {"get_calendar_events", "check_availability", "add_calendar_event", "delete_calendar_event",
             "add_task", "list_tasks", "complete_task", "delete_task"}
    assert names <= set(TOOLS)
    for n in names:
        p = TOOLS[n].parameters
        assert set(p["required"]) <= set(p["properties"]), n
        assert TOOLS[n].gate is None and TOOLS[n].description


# ---------------------------------------------------------------- ICS feeds
def test_feed_expands_recurrence_exdate_overrides_and_timezones(monkeypatch):
    ctx, _ = feed_ctx(monkeypatch)
    r = call("get_calendar_events", ctx, start_date="2030-10-28", days=31)
    assert "errors" not in r and "setup" not in r
    assert r["calendars"] == ["Work", "ATLAS"]
    assert r["range"] == {"from": "2030-10-28", "to": "2030-11-27", "days": 31}

    standups = [e for e in r["events"] if e["title"].startswith("Team standup")]
    # Weekly x5 from Oct 28: Nov 11 is EXDATE'd, Nov 18 cancelled, Nov 25 moved to 10:00 New York.
    assert [e["start"] for e in standups] == [
        "2030-10-28T06:00-07:00",  # EDT -> PDT
        "2030-11-04T06:00-08:00",  # EST -> PST, same wall clock after both zones left DST
        "2030-11-25T07:00-08:00",
    ]
    assert standups[-1]["title"] == "Team standup (moved)"
    assert standups[0]["end"] == "2030-10-28T06:30-07:00"
    assert standups[0]["location"] == "Zoom" and standups[0]["source"] == "ics" and standups[0]["calendar"] == "Work"
    assert standups[0]["when"] == "Mon Oct 28, 2030, 6:00 AM – 6:30 AM"

    by_title = {e["title"]: e for e in r["events"]}
    # London left DST on Oct 27 but Los Angeles only on Nov 3: a 7-hour gap that week.
    assert by_title["London call"]["start"] == "2030-10-30T08:00-07:00"
    assert by_title["Outlook style"]["start"] == "2030-11-06T06:00-08:00"  # Windows TZID name
    assert by_title["Lunch with Sam"]["start"] == "2030-11-05T12:00-08:00"  # UTC -> local
    assert by_title["Lunch with Sam"]["description"] == "Bring the deck\nTable for 2"  # HTML stripped
    # Floating time in a feed that declares X-WR-TIMEZONE belongs to that zone (16:00 New York).
    assert by_title["Floating focus time"]["start"] == "2030-11-05T13:00-08:00"
    assert by_title["Floating focus time"]["end"] == "2030-11-05T13:30-08:00"  # from DURATION

    offsite = by_title["Company offsite"]
    assert offsite["all_day"] is True
    assert (offsite["start"], offsite["end"]) == ("2030-11-05", "2030-11-06")  # end inclusive
    assert offsite["show_as"] == "free"
    assert "Cancelled thing" not in by_title
    assert "Declined meeting" not in by_title  # calendar id in the Google URL is me@example.com

    starts = [e["start"] for e in r["events"]]
    keyed = [datetime.fromisoformat(s) if "T" in s else datetime.fromisoformat(s).astimezone() for s in starts]
    assert keyed == sorted(keyed)
    assert r["status"] == f"{len(r['events'])} events"


def test_declined_uses_configured_email_for_non_google_feeds(monkeypatch):
    routes = {"p01-caldav.icloud.com": ics_response()}
    ctx, _ = feed_ctx(monkeypatch, routes, urls=OTHER_URL)
    assert "Declined meeting" in titles(call("get_calendar_events", ctx, start_date="2030-11-05", days=1))
    ct._FEEDS.clear()
    ctx, _ = feed_ctx(monkeypatch, routes, urls=OTHER_URL, email_address="Me@Example.com")
    assert "Declined meeting" not in titles(call("get_calendar_events", ctx, start_date="2030-11-05", days=1))


def test_range_and_query_filter(monkeypatch):
    ctx, _ = feed_ctx(monkeypatch)
    day = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert titles(day) == ["Company offsite", "Lunch with Sam", "Floating focus time"]
    # The offsite's second day is still listed the next day.
    assert "Company offsite" in titles(call("get_calendar_events", ctx, start_date="2030-11-06", days=1))
    q = call("get_calendar_events", ctx, start_date="2030-10-28", days=60, query="STANDUP moved")
    assert titles(q) == ["Team standup (moved)"]
    assert call("get_calendar_events", ctx, start_date="2030-10-28", days=999)["range"]["days"] == 60


def test_feed_is_cached_then_refreshed_with_stale_fallback(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return ics_response() if calls["n"] == 1 else httpx.Response(500, text="oops")

    ctx, router = feed_ctx(monkeypatch, {"calendar.google.com": handler})
    first = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    second = call("get_calendar_events", ctx, start_date="2030-11-04", days=3)
    assert calls["n"] == 1 and "errors" not in second  # served from the 5-minute cache
    assert router.requests[0].headers["user-agent"].startswith("ATLAS")

    monkeypatch.setattr(ct, "FEED_TTL", -1)  # expire it
    third = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert calls["n"] == 2
    assert titles(third) == titles(first)  # stale copy still shown
    assert "showing the copy from a few minutes ago" in third["errors"][0]
    assert "HTTP 500" in third["errors"][0]


def test_feed_errors_never_leak_the_secret_address(monkeypatch):
    def boom(request):
        raise httpx.ConnectError(f"could not reach {request.url}")

    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": boom})
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert r["events"] == []
    assert r["errors"] and "calendar #1 (calendar.google.com)" in r["errors"][0]
    assert SECRET not in json.dumps(r)

    ct._FEEDS.clear()
    ct._FAILED.clear()
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": httpx.Response(404, text="Not found")})
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert "HTTP 404" in r["errors"][0] and "secret address" in r["errors"][0]
    assert SECRET not in json.dumps(r)


def test_html_page_instead_of_feed_is_explained(monkeypatch):
    page = httpx.Response(200, text="<!DOCTYPE html><html><body>Sign in</body></html>")
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": page})
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert "web page" in r["errors"][0]


def test_labels_and_webcal_scheme(monkeypatch):
    ctx, router = feed_ctx(
        monkeypatch,
        {"p01-caldav.icloud.com": ics_response()},
        urls=f"Family = webcal://p01-caldav.icloud.com/published/2/{SECRET}",
    )
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert r["calendars"] == ["Family", "ATLAS"]
    assert {e["calendar"] for e in r["events"]} == {"Family"}
    assert router.requests[0].url.scheme == "https"


def test_multiple_feeds_are_merged(monkeypatch):
    second = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nX-WR-CALNAME:Gym\r\nBEGIN:VEVENT\r
UID:gym\r\nDTSTART:20301105T150000Z\r\nDTEND:20301105T160000Z\r\nSUMMARY:Spin class\r\nEND:VEVENT\r
BEGIN:VEVENT\r\nUID:stretch\r\nDTSTART:20301105T180000\r\nSUMMARY:Stretch\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    ctx, _ = feed_ctx(
        monkeypatch,
        {"calendar.google.com": ics_response(), "p01-caldav.icloud.com": ics_response(second)},
        urls=f"{GOOGLE_URL}, {OTHER_URL}",
    )
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert titles(r) == ["Company offsite", "Spin class", "Lunch with Sam", "Floating focus time", "Stretch"]
    assert r["calendars"] == ["Work", "Gym", "ATLAS"]
    stretch = r["events"][-1]
    # No X-WR-TIMEZONE: floating = local wall time; no DTEND/DURATION = zero length.
    assert stretch["start"] == stretch["end"] == "2030-11-05T18:00-08:00"


def test_no_feeds_shows_setup_hint():
    r = call("get_calendar_events", start_date="2030-11-05", days=1)
    assert r["events"] == [] and "CALENDAR_ICS_URLS" in r["setup"]


# ------------------------------------------------------------- availability
def test_check_availability_conflict_and_suggestions(monkeypatch):
    ctx, _ = feed_ctx(monkeypatch)
    r = call("check_availability", ctx, start="2030-11-05T12:30", end="2030-11-05T13:30")
    assert r["free"] is False and r["status"] == "busy (2 conflicts)"
    assert [c["title"] for c in r["conflicts"]] == ["Lunch with Sam", "Floating focus time"]
    assert [e["title"] for e in r["all_day_events"]] == ["Company offsite"]  # transparent: not a conflict
    # Day hours 08-20 minus lunch (12-13) and focus time (13:00-13:30); declined (14-15) and
    # cancelled events don't block.
    assert [(s["start"][11:16], s["end"][11:16]) for s in r["free_slots"]] == [("08:00", "12:00"), ("13:30", "20:00")]
    assert [s["start"][11:16] for s in r["suggestions"]] == ["13:30", "11:00"]
    assert all(s["minutes"] == 60 for s in r["suggestions"])


def test_check_availability_free_and_whole_day(monkeypatch):
    ctx, _ = feed_ctx(monkeypatch)
    free = call("check_availability", ctx, start="2030-11-05T09:00")
    assert free["free"] is True and free["status"] == "free" and "suggestions" not in free
    assert free["requested"]["end"] == "2030-11-05T10:00-08:00"  # default one hour

    monkeypatch.setenv("CALENDAR_DAY_HOURS", "09:00-17:00")
    day = call("check_availability", ctx, start="2030-11-05")
    assert day["day_hours"] == "09:00-17:00"
    assert day["status"] == "2 free slots"
    assert [(s["start"][11:16], s["end"][11:16]) for s in day["free_slots"]] == [("09:00", "12:00"), ("13:30", "17:00")]


def test_check_availability_validation_and_past_day():
    assert "error" in call("check_availability", start="2030-11-05T10:00", end="2030-11-05T09:00")
    assert "error" in call("check_availability", start="next tuesday-ish")
    past = call("check_availability", start="2020-01-01T10:00")
    assert past["free"] is True and past["free_slots"] == [] and "past" in past["note"]


def test_free_slots_today_start_from_now(monkeypatch):
    fake_now = datetime(2030, 11, 5, 10, 7).astimezone()
    monkeypatch.setattr(ct, "_now", lambda: fake_now)
    r = call("check_availability", start="2030-11-05")
    assert r["free_slots"][0]["start"] == "2030-11-05T10:15-08:00"


# --------------------------------------------------------- creating events
def _qs(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_google_link_utc_conversion_and_encoding():
    start = datetime(2030, 11, 5, 19, 30).astimezone()  # PST = UTC-8
    url = ct.google_calendar_link("Dinner & drinks @ Joe's #1", start, start + timedelta(hours=2),
                                  "Party of 4; conf ABC=123\nAsk for window", "12 Main St, Springfield",
                                  ["a@x.com", "b@y.org"])
    assert url.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE&")
    q = _qs(url)
    assert q["text"] == "Dinner & drinks @ Joe's #1"
    assert q["dates"] == "20301106T033000Z/20301106T053000Z"
    assert q["details"] == "Party of 4; conf ABC=123\nAsk for window"
    assert q["location"] == "12 Main St, Springfield"
    assert q["add"] == "a@x.com,b@y.org"
    raw = urlparse(url).query
    assert "text=Dinner%20%26%20drinks%20%40%20Joe%27s%20%231" in raw  # nothing unescaped can split params
    assert " " not in url and "#" not in url

    summer = datetime(2030, 7, 1, 10, 0).astimezone()  # PDT = UTC-7
    assert _qs(ct.google_calendar_link("x", summer, summer + timedelta(minutes=30)))["dates"] == \
        "20300701T170000Z/20300701T173000Z"
    assert _qs(ct.google_calendar_link("Trip", date(2030, 11, 5), date(2030, 11, 8)))["dates"] == "20301105/20301108"


def test_add_event_google_opens_prefilled_link(calendar_env):
    memory.save("contacts", [{"name": "Mom", "email": "mom@example.com", "aliases": ["mother"], "phone": ""}])
    r = call("add_calendar_event", title="Dinner at Luigi's", start="2030-11-05T19:30", duration_minutes=90,
             location="Luigi's, 5 Elm St", description="Reservation #A12 for 3",
             attendees=["sam@example.com", "mother", "Bob"])
    assert r["provider"] == "google" and r["opened"] is True
    assert "click Save" in r["status"] and "click Save" in r["next_step"]
    assert calendar_env["urls"] == [r["link"]]
    q = _qs(r["link"])
    assert q["dates"] == "20301106T033000Z/20301106T050000Z"
    assert q["add"] == "sam@example.com,mom@example.com"
    assert q["details"] == "Reservation #A12 for 3\n\nWith: Bob"
    assert r["unresolved_attendees"] == ["Bob"]

    ev = r["event"]
    assert ev["source"] == "local" and ev["calendar"] == "ATLAS"
    assert (ev["start"], ev["end"]) == ("2030-11-05T19:30-08:00", "2030-11-05T21:00-08:00")
    stored = json.loads((ct.DATA_DIR / "calendar.json").read_text())
    assert stored[0]["title"] == "Dinner at Luigi's" and stored[0]["link"] == r["link"]

    listed = call("get_calendar_events", start_date="2030-11-05", days=1)
    assert titles(listed) == ["Dinner at Luigi's"]
    assert listed["events"][0]["id"] == ev["id"]
    assert listed["events"][0]["attendees"] == ["sam@example.com", "mom@example.com", "Bob"]

    again = call("add_calendar_event", title="dinner at luigi's", start="2030-11-05T19:30")
    assert again["status"].startswith("already in ATLAS calendar")
    assert len(json.loads((ct.DATA_DIR / "calendar.json").read_text())) == 1
    # A retry without the details keeps them (and re-opens the same, complete event page).
    assert again["event"]["id"] == ev["id"] and again["event"]["end"] == "2030-11-05T21:00-08:00"
    assert again["event"]["location"] == "Luigi's, 5 Elm St"
    assert _qs(again["link"])["dates"] == q["dates"] and _qs(again["link"])["add"] == q["add"]


def test_add_all_day_event(calendar_env):
    r = call("add_calendar_event", title="Vacation", start="2030-12-20", end="2030-12-24")
    assert _qs(r["link"])["dates"] == "20301220/20301225"  # Google wants an exclusive end
    assert (r["event"]["start"], r["event"]["end"], r["event"]["all_day"]) == ("2030-12-20", "2030-12-24", True)
    one = call("add_calendar_event", title="Birthday", start="2030-12-01")
    assert _qs(one["link"])["dates"] == "20301201/20301202"
    assert one["event"]["when"] == "Sun Dec 1, 2030 (all day)"
    assert titles(call("get_calendar_events", start_date="2030-12-24", days=1)) == ["Vacation"]
    assert call("get_calendar_events", start_date="2030-12-25", days=1)["events"] == []


def test_add_event_outlook_link(monkeypatch, calendar_env):
    monkeypatch.setenv("CALENDAR_PROVIDER", "Outlook")
    r = call("add_calendar_event", title="Dentist", start="2030-11-05T08:15", end="2030-11-05T09:00",
             location="Smile & Co", attendees=["pat@example.com"])
    u = urlparse(r["link"])
    assert (u.scheme, u.netloc, u.path) == ("https", "outlook.live.com", "/calendar/0/deeplink/compose")
    q = _qs(r["link"])
    assert q["subject"] == "Dentist" and q["location"] == "Smile & Co"
    assert (q["startdt"], q["enddt"]) == ("2030-11-05T16:15:00Z", "2030-11-05T17:00:00Z")
    assert q["rru"] == "addevent" and q["path"] == "/calendar/action/compose"
    assert "pat@example.com" in q["body"]
    assert calendar_env["urls"] == [r["link"]]

    monkeypatch.setenv("CALENDAR_PROVIDER", "office365")
    work = call("add_calendar_event", title="Review", start="2030-11-07")
    assert urlparse(work["link"]).netloc == "outlook.office.com"
    assert _qs(work["link"])["allday"] == "true"
    assert (_qs(work["link"])["startdt"], _qs(work["link"])["enddt"]) == ("2030-11-07", "2030-11-08")


def test_add_event_ics_file_is_valid(monkeypatch, calendar_env):
    monkeypatch.setenv("CALENDAR_PROVIDER", "ics")
    notes = "Bring: passport, tickets; snacks\nGate opens at 6. " + "x" * 200
    r = call("add_calendar_event", title="Flight to NYC", start="2030-11-05T19:30", end="2030-11-05T23:00",
             location="SFO, Terminal 2", description=notes)
    path = r["file"]
    assert calendar_env["files"] and str(calendar_env["files"][0]) == path
    assert path.startswith(str(ct.DATA_DIR / "exports")) and path.endswith(".ics")
    raw = open(path, "rb").read()
    assert all(len(line) <= 75 for line in raw.split(b"\r\n"))  # RFC 5545 line folding
    cal = icalendar.Calendar.from_ical(raw)
    assert str(cal["VERSION"]) == "2.0" and "ATLAS" in str(cal["PRODID"])
    events = cal.walk("VEVENT")
    assert len(events) == 1
    ev = events[0]
    assert str(ev["SUMMARY"]) == "Flight to NYC"
    assert str(ev["LOCATION"]) == "SFO, Terminal 2"
    assert str(ev["DESCRIPTION"]) == notes
    assert ev.decoded("DTSTART") == datetime(2030, 11, 6, 3, 30, tzinfo=timezone.utc)
    assert ev.decoded("DTEND") == datetime(2030, 11, 6, 7, 0, tzinfo=timezone.utc)
    assert str(ev["UID"]).endswith("@atlas.local") and "DTSTAMP" in ev
    assert "click Save" in r["status"]

    allday = call("add_calendar_event", title="Holiday", start="2030-12-25")
    ev2 = icalendar.Calendar.from_ical(open(allday["file"], "rb").read()).walk("VEVENT")[0]
    assert ev2.decoded("DTSTART") == date(2030, 12, 25) and ev2.decoded("DTEND") == date(2030, 12, 26)


def test_add_event_local_provider_opens_nothing(monkeypatch, calendar_env):
    monkeypatch.setenv("CALENDAR_PROVIDER", "local")
    r = call("add_calendar_event", title="Focus", start="2030-11-05T14:00")
    assert r["status"] == "added to ATLAS calendar" and "link" not in r
    assert calendar_env == {"urls": [], "files": []}


def test_add_event_reports_conflicts_and_local_copy_dedupes_with_feed(monkeypatch):
    monkeypatch.setenv("CALENDAR_PROVIDER", "local")
    ctx, _ = feed_ctx(monkeypatch)
    r = call("add_calendar_event", ctx, title="Coffee with Lee", start="2030-11-05T12:45", duration_minutes=30)
    assert [c["title"] for c in r["conflicts"]] == ["Lunch with Sam", "Floating focus time"]
    r = call("add_calendar_event", ctx, title="Lunch with Sam", start="2030-11-05T12:00")
    # The feed copy is the same event, not a clash; the coffee added above is.
    assert [c["title"] for c in r["conflicts"]] == ["Coffee with Lee"]
    # Once saved into Google it appears in the feed too; only the feed copy is listed.
    listed = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    lunches = [e for e in listed["events"] if e["title"] == "Lunch with Sam"]
    assert len(lunches) == 1 and lunches[0]["source"] == "ics"


def test_add_event_validation():
    assert "error" in call("add_calendar_event", title="  ", start="2030-11-05T10:00")
    assert "error" in call("add_calendar_event", title="x", start="2030-11-05T10:00", end="2030-11-05T09:00")
    assert "error" in call("add_calendar_event", title="x", start="2030-11-05T10:00", duration_minutes=-5)
    assert "error" in call("add_calendar_event", title="x", start="5pm tomorrow")
    assert "error" in call("add_calendar_event", title="x", start="2030-12-05", end="2030-12-01")
    assert not (ct.DATA_DIR / "calendar.json").exists()


def test_offset_input_is_converted_to_local():
    r = call("add_calendar_event", title="Call", start="2030-11-05T15:00:00Z", duration_minutes=30)
    assert r["event"]["start"] == "2030-11-05T07:00-08:00"
    assert _qs(r["link"])["dates"] == "20301105T150000Z/20301105T153000Z"


def test_delete_calendar_event():
    ev = call("add_calendar_event", title="Temp", start="2030-11-05T10:00")["event"]
    assert "error" in call("delete_calendar_event", id="ics-0123456789")
    assert "error" in call("delete_calendar_event", id="nope")
    r = call("delete_calendar_event", id=ev["id"])
    assert r["removed"] == 1 and "Temp" in r["status"] and "delete it there" in r["note"]
    assert call("get_calendar_events", start_date="2030-11-05", days=1)["events"] == []


# ----------------------------------------------------------------- to-dos
def test_tasks_crud_and_ordering():
    ids = {}
    for title, due, prio in [
        ("Renew passport", "2030-11-10", "normal"),
        ("Water plants", "2030-11-05", "low"),
        ("Call the bank", None, "high"),
        ("Clean garage", None, "low"),
        ("Pay rent", "2030-11-05", "urgent"),
        ("Book dentist", "2030-11-05T09:00", None),
    ]:
        args = {"title": title, "priority": prio} if prio else {"title": title}
        if due:
            args["due"] = due
        r = call("add_task", **args)
        assert r["status"] == "added"
        ids[title] = r["task"]["id"]

    listed = call("list_tasks")
    assert [t["title"] for t in listed["tasks"]] == [
        "Book dentist", "Pay rent", "Water plants", "Renew passport", "Call the bank", "Clean garage"]
    assert listed["status"] == "6 open tasks" and listed["open"] == 6
    pay = listed["tasks"][1]
    assert pay["priority"] == "high" and pay["due"] == "2030-11-05" and pay["due_human"] == "Tue Nov 5, 2030"
    assert listed["tasks"][0]["due"] == "2030-11-05T09:00-08:00"
    assert listed["tasks"][0]["due_human"] == "Tue Nov 5, 2030, 9:00 AM"

    assert call("add_task", title="renew PASSPORT")["status"] == "already on the list"

    done = call("complete_task", id="passport")  # unique part of the title
    assert done["task"]["done"] is True and done["open_remaining"] == 5
    assert call("complete_task", id=ids["Renew passport"])["status"] == "already done"
    assert "Renew passport" not in [t["title"] for t in call("list_tasks")["tasks"]]
    with_done = call("list_tasks", include_done=True)
    assert with_done["tasks"][-1]["title"] == "Renew passport" and with_done["tasks"][-1]["completed"]

    err = call("complete_task", id="c")  # matches several open tasks
    assert "Several tasks match" in err["error"] and ids["Call the bank"] in err["error"]
    assert "error" in call("complete_task", id="no such thing")

    assert call("delete_task", id=ids["Clean garage"])["removed"] == 1
    assert [t["title"] for t in call("list_tasks", query="bank")["tasks"]] == ["Call the bank"]
    assert len(call("list_tasks", include_done=True)["tasks"]) == 5


def test_task_overdue_aliases_and_validation():
    late = call("add_task", title="File taxes", due="2020-04-15", notes="Use the 1099s")["task"]
    assert late["overdue"] is True and late["notes"] == "Use the 1099s"
    today = call("add_task", title="Stretch", due="today", priority="bogus")["task"]
    assert today["due"] == datetime.now().astimezone().date().isoformat()
    assert today["priority"] == "normal" and "overdue" not in today
    r = call("list_tasks")
    assert r["overdue"] == 1 and r["status"] == "2 open tasks, 1 overdue"
    assert "error" in call("add_task", title="x", due="whenever")
    assert "error" in call("add_task", title="   ")
    assert len(call("list_tasks")["tasks"]) == 2


def test_tasks_survive_a_corrupt_file():
    (ct.DATA_DIR).mkdir(parents=True, exist_ok=True)
    (ct.DATA_DIR / "tasks.json").write_text("{not json")
    assert call("list_tasks")["tasks"] == []
    assert call("add_task", title="Fresh start")["status"] == "added"


# ------------------------------------------------------------------ panel
def test_agenda_for_panel(monkeypatch):
    monkeypatch.setenv("CALENDAR_PROVIDER", "local")
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    call("add_calendar_event", title="Soon", start=(now + timedelta(hours=1)).isoformat())
    call("add_calendar_event", title="Already over", start=(now - timedelta(hours=3)).isoformat())
    call("add_calendar_event", title="Far away", start=(now + timedelta(days=10)).isoformat())
    call("add_task", title="Buy milk", priority="high")
    t = call("add_task", title="Old chore")["task"]
    call("complete_task", id=t["id"])

    panel = run(ct.agenda_for_panel(make_ctx(http=mock_http(no_network)), days=2))
    assert [e["title"] for e in panel["events"]] == ["Soon"]
    assert [t["title"] for t in panel["tasks"]] == ["Buy milk"] and panel["open_tasks"] == 1
    assert panel["feeds_connected"] is False and panel["errors"] == []
    json.dumps(panel)  # JSON-able for the UI


def test_agenda_for_panel_never_raises(monkeypatch):
    def broken(request):
        raise httpx.ConnectError("down")

    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": broken})
    panel = run(ct.agenda_for_panel(ctx))
    assert panel["events"] == [] and panel["feeds_connected"] is True and panel["errors"]


# ------------------------------------------------------- adversarial feeds
def _cal(*events: str, name: str = "") -> bytes:
    head = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n" + (f"X-WR-CALNAME:{name}\r\n" if name else "")
    return (head + "".join(events) + "END:VCALENDAR\r\n").encode()


def _vevent(uid: str, summary: str, *lines: str, start: str = "20301105T150000Z", end: str = "20301105T160000Z"):
    body = [f"UID:{uid}", f"SUMMARY:{summary}"]
    if start:
        body.append(f"DTSTART:{start}")
    if end:
        body.append(f"DTEND:{end}")
    return "BEGIN:VEVENT\r\n" + "".join(x + "\r\n" for x in [*body, *lines]) + "END:VEVENT\r\n"


def test_malformed_events_do_not_hide_the_rest_of_the_feed(monkeypatch):
    body = _cal(
        _vevent("ok", "Good one"),
        # icalendar decodes this to a tuple, which the expansion library chokes on
        _vevent("period", "Period start", "DTSTART;VALUE=PERIOD:20301105T170000Z/PT1H", start="", end=""),
        _vevent("badrule", "Bad rule", "RRULE:FREQ=SOMETIMES", start="20301101T150000Z", end="20301101T160000Z"),
        _vevent("nodate", "No date", start="", end=""),
    )
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": ics_response(body)})
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert titles(r) == ["Good one"] and "errors" not in r

    # An unknown zone in the calendar-wide X-WR-TIMEZONE header is ignored rather than fatal.
    bad_zone = _cal(_vevent("ok", "Good one")).replace(b"PRODID:x\r\n", b"PRODID:x\r\nX-WR-TIMEZONE:Mars/Olympus\r\n")
    ct._FEEDS.clear()
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": ics_response(bad_zone)})
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert titles(r) == ["Good one"] and "errors" not in r


def test_dense_recurring_invitations_are_skipped_not_expanded(monkeypatch):
    """Anyone can send an invite; one that repeats every minute since 1990 took ~80 s per query."""
    body = _cal(
        _vevent("spam1", "Every minute", "RRULE:FREQ=MINUTELY", start="19900101T150000Z", end="19900101T150100Z"),
        _vevent("spam2", "Daily but every minute",
                "RRULE:FREQ=DAILY;BYHOUR=" + ",".join(map(str, range(24))) + ";BYMINUTE=" + ",".join(map(str, range(60))),
                start="19900101T000000Z", end="19900101T000100Z"),
        _vevent("spam3", "Secondly", "RRULE:FREQ=SECONDLY;INTERVAL=1", start="20000101T000000Z", end=""),
        _vevent("weekly", "Weekly 1:1", "RRULE:FREQ=WEEKLY;BYDAY=TU", start="19900102T150000Z", end="19900102T153000Z"),
        _vevent("hourly", "Hourly ping", "RRULE:FREQ=HOURLY;COUNT=3", start="20301105T170000Z", end=""),
        _vevent("weekdays", "Gym", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", start="20100104T130000Z",
                end="20100104T140000Z"),
    )
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": ics_response(body)})
    t0 = time.monotonic()
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert time.monotonic() - t0 < 10
    assert titles(r) == ["Gym", "Weekly 1:1", "Hourly ping", "Hourly ping", "Hourly ping"]


def test_series_cost_estimate():
    start, horizon = date(2020, 1, 1), date(2021, 1, 1)
    days = (horizon - start).days + 1
    rule = icalendar.vRecur.from_ical
    assert ct._series_steps(rule("FREQ=DAILY"), start, horizon) == days
    assert ct._series_steps(rule("FREQ=HOURLY;INTERVAL=2"), start, horizon) == 12 * days
    assert ct._series_steps(rule("FREQ=MINUTELY;COUNT=10"), start, horizon) == 10
    assert ct._series_steps(rule("FREQ=WEEKLY;BYDAY=MO,TU;BYHOUR=9,17"), start, horizon) == 2 * days
    assert ct._series_steps(rule("FREQ=DAILY;UNTIL=20200110"), start, horizon) == 10


def test_secret_parts_of_any_address_are_scrubbed(monkeypatch):
    token = "PassphraseKey-77f0e1d2c3"
    url = f"https://user:pw-secret-1@calendar.example.org/api/v1/url/abc/calendar.ics?CacheKey=k1&{token}=1"

    def boom(request):
        raise httpx.ConnectError(f"failed for {url} / {request.url} (query {request.url.query.decode()})")

    ctx, _ = feed_ctx(monkeypatch, {"calendar.example.org": boom}, urls=url)
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    dumped = json.dumps(r)
    assert "calendar #1 (calendar.example.org)" in r["errors"][0]
    for secret in (token, "pw-secret-1", "CacheKey=k1", "/api/v1/url/abc"):
        assert secret not in dumped
    assert "<secret>" in dumped


def test_oversized_feed_is_refused(monkeypatch):
    monkeypatch.setattr(ct, "MAX_FEED_BYTES", 1024)
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": ics_response(FEED)})
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert r["events"] == [] and "larger than" in r["errors"][0]


def test_slow_feed_times_out_then_backs_off(monkeypatch):
    calls = {"n": 0}

    async def slow(request):
        calls["n"] += 1
        await asyncio.sleep(10)
        return ics_response()

    monkeypatch.setattr(ct, "FEED_DEADLINE", 0.2)
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": slow})
    t0 = time.monotonic()
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert time.monotonic() - t0 < 5
    assert "no complete answer" in r["errors"][0] and r["events"] == []
    # Within RETRY_AFTER the failing feed isn't waited on again.
    again = call("check_availability", ctx, start="2030-11-05T09:00")
    assert calls["n"] == 1 and "no complete answer" in again["errors"][0]
    monkeypatch.setattr(ct, "RETRY_AFTER", -1)
    call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert calls["n"] == 2


def test_parallel_tool_calls_share_one_fetch(monkeypatch):
    ctx, router = feed_ctx(monkeypatch)

    async def both():
        return await asyncio.gather(
            call_tool("get_calendar_events", {"start_date": "2030-11-05", "days": 1}, ctx),
            call_tool("check_availability", {"start": "2030-11-05T12:30"}, ctx),
        )

    listed, avail = (json.loads(x) for x in run(both()))
    assert len(router.requests) == 1
    assert "Lunch with Sam" in titles(listed) and avail["free"] is False


def test_duplicate_addresses_and_shared_invites_are_listed_once(monkeypatch):
    invite = _vevent("shared-1", "Board meeting")
    other = _cal(invite, _vevent("own", "Own thing", start="20301105T180000Z", end="20301105T190000Z"), name="Family")
    ctx, router = feed_ctx(
        monkeypatch,
        {"calendar.google.com": ics_response(_cal(invite, name="Work")), "p01-caldav.icloud.com": ics_response(other)},
        urls=f"{GOOGLE_URL}, {GOOGLE_URL}\n{OTHER_URL}",
    )
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert len(router.hits("calendar.google.com")) == 1
    assert titles(r) == ["Board meeting", "Own thing"]
    assert r["events"][0]["calendar"] == "Work" and r["calendars"] == ["Work", "Family", "ATLAS"]


def test_removed_feed_is_dropped_from_the_cache(monkeypatch):
    ctx, _ = feed_ctx(monkeypatch)
    call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert GOOGLE_URL in ct._FEEDS
    monkeypatch.delenv("CALENDAR_ICS_URLS")
    call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert ct._FEEDS == {}


def test_missing_packages_give_install_hints(monkeypatch):
    monkeypatch.setattr(ct, "icalendar", None)
    monkeypatch.setattr(ct, "recurring_ical_events", None)
    ctx, _ = feed_ctx(monkeypatch)
    r = call("get_calendar_events", ctx, start_date="2030-11-05", days=1)
    assert r["events"] == [] and "pip install icalendar recurring-ical-events" in r["errors"][0]
    monkeypatch.setenv("CALENDAR_PROVIDER", "ics")
    assert "pip install icalendar" in call("add_calendar_event", title="x", start="2030-11-05T10:00")["error"]
    monkeypatch.setenv("CALENDAR_PROVIDER", "local")
    assert call("add_calendar_event", title="x", start="2030-11-05T10:00")["status"] == "added to ATLAS calendar"
    assert call("add_task", title="still works")["status"] == "added"


# ------------------------------------------------------------ input limits
def test_out_of_range_dates_and_sizes_are_friendly_errors():
    for name, args in [
        ("get_calendar_events", {"start_date": "9999-12-31"}),
        ("get_calendar_events", {"start_date": "1900-01-01"}),  # Windows can't localise pre-1970 times
        ("check_availability", {"start": "9999-12-31T23:30"}),
        ("add_calendar_event", {"title": "x", "start": "9999-12-31"}),
        ("add_calendar_event", {"title": "x", "start": "2030-11-05T10:00", "duration_minutes": 10 ** 12}),
        ("add_calendar_event", {"title": "x", "start": "2030-11-05T10:00", "duration_minutes": "lots"}),
        ("check_availability", {"start": "2030-11-05T10:00", "end": "2031-11-05T10:00"}),
        ("add_task", {"title": "x", "due": "0001-01-01"}),
    ]:
        err = call(name, **args).get("error", "")
        assert err and " failed: " not in err, (name, args, err)  # a ToolError, not a crash


def test_day_hours_setting_is_validated(monkeypatch):
    monkeypatch.setenv("CALENDAR_DAY_HOURS", "08:75-20:00")
    assert ct._day_hours() == ct.DEFAULT_DAY_HOURS
    monkeypatch.setenv("CALENDAR_DAY_HOURS", "7-23:30")
    assert ct._day_hours() == (7 * 60, 23 * 60 + 30)


def test_evening_request_outside_day_hours_gets_evening_suggestions(monkeypatch):
    late = _vevent("late", "Late call", start="20301106T053000Z", end="20301106T063000Z")  # 21:30-22:30 PST
    ctx, _ = feed_ctx(monkeypatch, {"calendar.google.com": ics_response(_cal(late))})
    r = call("check_availability", ctx, start="2030-11-05T21:00", end="2030-11-05T22:30")
    assert r["free"] is False and [c["title"] for c in r["conflicts"]] == ["Late call"]
    assert r["day_hours"] == "08:00-20:00"
    # The search reaches the requested evening hours instead of stopping at 20:00.
    assert r["suggestions"][0]["start"][11:16] == "20:00"
    assert r["free_slots"][-1]["end"][11:16] == "21:30"


def test_long_details_are_fitted_to_the_link_limit(monkeypatch):
    monkeypatch.setattr(ct, "MAX_LINK_CHARS", 2000)  # what Windows' ShellExecute accepts
    start = datetime(2030, 11, 5, 19, 30).astimezone()
    notes = "Bestätigung 東京 " * 120
    for url, key in [
        (ct.google_calendar_link("Dinner", start, start + timedelta(hours=2), notes, "Main St"), "details"),
        (ct.outlook_calendar_link("Dinner", start, start + timedelta(hours=2), notes, "Main St"), "body"),
    ]:
        assert len(url) <= 2000
        got = _qs(url)[key]
        assert got.endswith("…") and notes.startswith(got[:-1].rstrip()) and len(got) > 50
    short = ct.google_calendar_link("Dinner", start, start + timedelta(hours=2), "Table for 2")
    assert _qs(short)["details"] == "Table for 2"


def test_injected_text_cannot_add_link_parameters_or_ics_lines(monkeypatch, calendar_env):
    evil = "Lunch\r\nATTENDEE:mailto:x@evil.test\r\nEND:VEVENT&add=x@evil.test#frag"
    r = call("add_calendar_event", title=evil, start="2030-11-05T12:00", description=evil, location=evil)
    q = parse_qs(urlparse(r["link"]).query)
    assert "add" not in q and set(q) == {"action", "text", "dates", "details", "location"}
    monkeypatch.setenv("CALENDAR_PROVIDER", "ics")
    path = call("add_calendar_event", title=evil, start="2030-11-06T12:00", description=evil)["file"]
    assert os.path.dirname(path) == str(ct.DATA_DIR / "exports") and "/" not in os.path.basename(path)
    raw = open(path, "rb").read()
    cal = icalendar.Calendar.from_ical(raw)
    assert len(cal.walk("VEVENT")) == 1 and b"\r\nATTENDEE" not in raw
    assert "ATTENDEE" not in cal.walk("VEVENT")[0]


def test_readd_merges_and_ics_keeps_the_same_uid(monkeypatch, calendar_env):
    monkeypatch.setenv("CALENDAR_PROVIDER", "ics")
    first = call("add_calendar_event", title="Dinner", start="2030-11-05T19:00", duration_minutes=120,
                 location="Luigi's")
    second = call("add_calendar_event", title="Dinner", start="2030-11-05T19:00", description="Conf #XY12")
    assert second["status"].startswith("already in ATLAS calendar")
    stored = json.loads((ct.DATA_DIR / "calendar.json").read_text())
    assert len(stored) == 1
    assert (stored[0]["location"], stored[0]["description"]) == ("Luigi's", "Conf #XY12")
    assert stored[0]["end"] == "2030-11-05T21:00-08:00"
    uid = [str(icalendar.Calendar.from_ical(open(x["file"], "rb").read()).walk("VEVENT")[0]["UID"])
           for x in (first, second)]
    assert uid[0] == uid[1]  # calendar apps update the event instead of adding a second copy
    ev = icalendar.Calendar.from_ical(open(second["file"], "rb").read()).walk("VEVENT")[0]
    assert str(ev["LOCATION"]) == "Luigi's" and "Conf #XY12" in str(ev["DESCRIPTION"])


# ------------------------------------------------------- opening per platform
def test_open_url_needs_a_desktop_and_https(monkeypatch):
    opened = []
    monkeypatch.setattr(ct.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(ct, "IS_WIN", False)
    monkeypatch.setattr(ct, "IS_MAC", False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    # Headless Linux: webbrowser would start a console browser (lynx, w3m) that blocks.
    assert REAL_OPEN_URL("https://calendar.google.com/x") is False and opened == []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert REAL_OPEN_URL("https://calendar.google.com/x") is True
    assert REAL_OPEN_URL("file:///etc/passwd") is False
    assert opened == ["https://calendar.google.com/x"]
    monkeypatch.setattr(ct.webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no")))
    assert REAL_OPEN_URL("https://calendar.google.com/x") is False


def test_open_file_on_windows_macos_and_linux(monkeypatch, tmp_path):
    path = tmp_path / "e.ics"
    path.write_text("x")
    seen = []
    monkeypatch.setattr(ct, "IS_WIN", True)
    monkeypatch.setattr(ct.os, "startfile", lambda p: seen.append(("startfile", p)), raising=False)
    assert REAL_OPEN_FILE(path) is True and seen[-1] == ("startfile", str(path))

    def no_handler(p):
        raise OSError("No application is associated")

    monkeypatch.setattr(ct.os, "startfile", no_handler, raising=False)
    assert REAL_OPEN_FILE(path) is False

    monkeypatch.setattr(ct, "IS_WIN", False)
    monkeypatch.setattr(ct, "IS_MAC", True)
    monkeypatch.setattr(ct.subprocess, "run",
                        lambda args, **kw: seen.append(("run", args, kw.get("timeout"))) or
                        type("R", (), {"returncode": 0})())
    assert REAL_OPEN_FILE(path) is True and seen[-1] == ("run", ["open", str(path)], 15)

    monkeypatch.setattr(ct, "IS_MAC", False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(ct.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(ct.subprocess, "Popen", lambda args, **kw: seen.append(("popen", args, kw)))
    assert REAL_OPEN_FILE(path) is True
    assert seen[-1][1] == ["xdg-open", str(path)] and seen[-1][2]["start_new_session"] is True
    monkeypatch.setattr(ct.shutil, "which", lambda name: None)
    assert REAL_OPEN_FILE(path) is False
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(ct.shutil, "which", lambda name: "/usr/bin/xdg-open")
    assert REAL_OPEN_FILE(path) is False


def test_ics_hand_off_without_a_calendar_app_points_at_the_file(monkeypatch, calendar_env):
    monkeypatch.setenv("CALENDAR_PROVIDER", "ics")
    monkeypatch.setattr(ct, "_open_file", lambda p: False)
    r = call("add_calendar_event", title="Trip", start="2030-11-05")
    assert r["opened"] is False and r["file"] in r["next_step"] and "click Save" not in r["status"]


# ------------------------------------------------------- damaged to-do data
def test_damaged_task_entries_and_string_booleans():
    ct.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (ct.DATA_DIR / "tasks.json").write_text(json.dumps([
        {"title": "No id", "done": "false"},
        {"id": "x1"},  # no title: dropped
        {"id": "x2", "title": "Done one", "done": True, "completed": "2030-01-01T00:00:00"},
        {"id": "x3", "title": "Odd priority", "priority": "URGENT", "due": None},
        "not a dict",
    ]))
    r = call("list_tasks")
    assert [t["title"] for t in r["tasks"]] == ["Odd priority", "No id"]
    assert r["tasks"][0]["priority"] == "high"
    assert len(call("list_tasks", include_done="false")["tasks"]) == 2
    assert len(call("list_tasks", include_done="true")["tasks"]) == 3
    assert call("complete_task", id="no id")["task"]["done"] is True
    panel = run(ct.agenda_for_panel(make_ctx(http=mock_http(no_network))))
    assert [t["title"] for t in panel["tasks"]] == ["Odd priority"] and panel["errors"] == []
