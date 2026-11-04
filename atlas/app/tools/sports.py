"""Live sports from ESPN's public JSON feed: scores, standings, team schedules and news.

Source: ESPN's "site" API (site.api.espn.com), the JSON that espn.com and the ESPN apps load. It is public and
keyless but UNOFFICIAL (no published contract or SLA): the endpoint formats below are long-lived and widely used,
yet every call here is best-effort. Every failure ends in a friendly ToolError that names ESPN, and sports news
falls back to Google News (through the news tool's feed reader) when ESPN's news feed is down.

  scoreboard  https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD
  standings   https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings   (note: /apis/v2, not /site/v2;
              the /site/v2 standings path only returns a link to the web page)
  teams       https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams
  schedule    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{id}/schedule
  news        https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news?limit=N[&team={id}]

Requests to each host are spaced out and cached: scoreboards for 30 s while a game is live and for minutes
otherwise, standings for 10 minutes, team lists for a day. Times are shown in the user's local time zone (the
computer's own, or the optional SPORTS_TIMEZONE override). Nothing here needs approval: it only reads public data.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from ..config import load_env_file
from ..util import BROWSER_UA, clip, relative_time
from . import ToolError, tool

SITE = "https://site.api.espn.com/apis/site/v2/sports"
STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports"
SOURCE = "ESPN"
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
REQUEST_TIMEOUT = 10.0  # per request; a tool may chain up to four (agent limit: 60 s per tool)

# Cache lifetimes (seconds)
LIVE_TTL = 30  # a game in progress
SOON_TTL = 60  # a game starts within the hour
UPCOMING_TTL = 300
FINAL_TTL = 900  # every game on the board is over
STANDINGS_TTL = 600
TEAMS_TTL = 24 * 3600
NEWS_TTL = 300
CACHE_MAX = 256

# Per-host spacing and a pause after HTTP 429.
HOST_INTERVAL = 0.2
MAX_QUEUE_WAIT = 8.0
COOLDOWN_429 = 30.0

MAX_GAMES = 30
RESULT_BUDGET = 9000  # characters of JSON; the agent clips tool output at 14,000
STANDINGS_ROW_CAP = 64  # beyond this many rows (college football...), show the top of each group only
DEFAULT_PANEL_LEAGUES = "nfl,nba,nhl,mlb,epl"


def _setting(key: str, default: str = "") -> str:
    try:
        env = load_env_file()
    except Exception:  # unreadable .env: behave as if it were empty
        env = {}
    return str(os.environ.get(key) or env.get(key) or default).strip()


def _d(value) -> dict:
    """ESPN's JSON, defensively: a field that should be an object (anything else reads as empty)."""
    return value if isinstance(value, dict) else {}


def _l(value) -> list:
    """A field that should be an array (anything else reads as empty)."""
    return value if isinstance(value, list) else []


def _text(value) -> str:
    """Tool arguments as a single-spaced string (models sometimes send numbers or null)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())


# --------------------------------------------------------------------------- leagues
@dataclass(frozen=True)
class League:
    key: str
    sport: str
    slug: str
    name: str
    kind: str = "team"  # team | racing | golf | mma

    @property
    def path(self) -> str:
        return f"{self.sport}/{self.slug}"

    @property
    def soccer(self) -> bool:
        return self.sport == "soccer"

    @property
    def college(self) -> bool:
        return "college" in self.slug


LEAGUES: dict[str, League] = {
    lg.key: lg
    for lg in (
        League("nfl", "football", "nfl", "NFL"),
        League("college-football", "football", "college-football", "College Football"),
        League("nba", "basketball", "nba", "NBA"),
        League("wnba", "basketball", "wnba", "WNBA"),
        League("mens-college-basketball", "basketball", "mens-college-basketball", "Men's College Basketball"),
        League("womens-college-basketball", "basketball", "womens-college-basketball", "Women's College Basketball"),
        League("mlb", "baseball", "mlb", "MLB"),
        League("nhl", "hockey", "nhl", "NHL"),
        League("mls", "soccer", "usa.1", "MLS"),
        League("nwsl", "soccer", "usa.nwsl", "NWSL"),
        League("epl", "soccer", "eng.1", "Premier League"),
        League("laliga", "soccer", "esp.1", "LaLiga"),
        League("bundesliga", "soccer", "ger.1", "Bundesliga"),
        League("serie-a", "soccer", "ita.1", "Serie A"),
        League("ligue-1", "soccer", "fra.1", "Ligue 1"),
        League("liga-mx", "soccer", "mex.1", "Liga MX"),
        League("ucl", "soccer", "uefa.champions", "UEFA Champions League"),
        League("europa", "soccer", "uefa.europa", "UEFA Europa League"),
        League("f1", "racing", "f1", "Formula 1", "racing"),
        League("ufc", "mma", "ufc", "UFC", "mma"),
        League("pga", "golf", "pga", "PGA Tour", "golf"),
    )
}
LEAGUE_LIST = ", ".join(LEAGUES)

_LEAGUE_ALIASES = {
    "football": "nfl", "pro football": "nfl", "american football": "nfl", "national football league": "nfl",
    "college football": "college-football", "ncaaf": "college-football", "cfb": "college-football",
    "ncaa football": "college-football", "fbs": "college-football", "ncaafb": "college-football",
    "basketball": "nba", "pro basketball": "nba", "national basketball association": "nba",
    "college basketball": "mens-college-basketball", "mens college basketball": "mens-college-basketball",
    "ncaab": "mens-college-basketball", "ncaam": "mens-college-basketball", "cbb": "mens-college-basketball",
    "ncaa basketball": "mens-college-basketball", "march madness": "mens-college-basketball",
    "ncaa mens basketball": "mens-college-basketball",
    "womens college basketball": "womens-college-basketball", "ncaaw": "womens-college-basketball",
    "wcbb": "womens-college-basketball", "ncaa womens basketball": "womens-college-basketball",
    "baseball": "mlb", "major league baseball": "mlb",
    "hockey": "nhl", "ice hockey": "nhl", "national hockey league": "nhl",
    "major league soccer": "mls",
    "premier league": "epl", "english premier league": "epl", "prem": "epl", "the prem": "epl",
    "barclays premier league": "epl", "england": "epl",
    "la liga": "laliga", "spanish league": "laliga", "primera division": "laliga", "liga": "laliga",
    "german league": "bundesliga",
    "serie a": "serie-a", "seriea": "serie-a", "italian league": "serie-a", "calcio": "serie-a",
    "ligue 1": "ligue-1", "ligue1": "ligue-1", "french league": "ligue-1",
    "liga mx": "liga-mx", "ligamx": "liga-mx", "mexican league": "liga-mx",
    "champions league": "ucl", "uefa champions league": "ucl", "cl": "ucl",
    "europa league": "europa", "uel": "europa", "uefa europa league": "europa",
    "formula 1": "f1", "formula one": "f1", "formula1": "f1", "grand prix": "f1",
    "mma": "ufc", "ultimate fighting championship": "ufc",
    "pga tour": "pga", "golf": "pga",
}
_SLUG_KEYS = {lg.slug: lg.key for lg in LEAGUES.values()}
_PATH_KEYS = {lg.path: lg.key for lg in LEAGUES.values()}
_RAW_PATH = re.compile(r"([a-z]+(?:-[a-z]+)*)/([a-z0-9]+(?:[.\-][a-z0-9]+)*)")
_KIND_BY_SPORT = {"racing": "racing", "golf": "golf", "mma": "mma"}


def resolve_league(value) -> League:
    """Map 'NBA', 'premier league', 'college football', 'eng.1' or an ESPN 'sport/league' slug to a League."""
    raw = _text(value).lower()
    if not raw:
        raise ToolError(f"Say which league, e.g. {LEAGUE_LIST}.")
    if raw in _PATH_KEYS:
        return LEAGUES[_PATH_KEYS[raw]]
    m = _RAW_PATH.fullmatch(raw)
    if m:  # any other ESPN league, e.g. soccer/ned.1 (Eredivisie); best-effort
        sport, slug = m.groups()
        return League(raw, sport, slug, slug.upper(), _KIND_BY_SPORT.get(sport, "team"))
    s = raw.replace("’", "'").replace("'", "")
    s = re.sub(r"[\s_\-]+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\s+(scores?|games?|results?|fixtures?|standings|table|schedule|news)$", "", s)

    def lookup(text: str) -> str | None:
        hyphen = text.replace(" ", "-")
        if hyphen in LEAGUES:
            return hyphen
        return _LEAGUE_ALIASES.get(text) or _SLUG_KEYS.get(text) or _SLUG_KEYS.get(text.replace(" ", "."))

    key = lookup(s)
    if not key:  # "nfl football", "nba basketball", "premier league soccer"
        trimmed = re.sub(r"\s+(football|soccer|futbol|basketball|hoops|baseball|hockey|racing|golf|league)$", "", s)
        key = lookup(trimmed) if trimmed != s else None
    if key:
        return LEAGUES[key]
    if s in ("soccer", "futbol", "football soccer"):
        soccer = ", ".join(k for k, lg in LEAGUES.items() if lg.soccer)
        raise ToolError(f"Which soccer league? I can do {soccer}.")
    raise ToolError(
        f"I don't have '{_text(value)}' as a league. Supported: {LEAGUE_LIST}, or an ESPN 'sport/league' slug "
        "such as soccer/ned.1."
    )


# --------------------------------------------------------------------------- rate limiting & caching
class HostLimiter:
    """Spaces requests to each host at least `interval` apart (across concurrent callers) and pauses a host for a
    while after it answers 429. Slots are reserved under a thread lock, so it isn't tied to one event loop."""

    def __init__(self, interval: float = HOST_INTERVAL, max_wait: float = MAX_QUEUE_WAIT, clock=time.monotonic, sleep=None):
        self.interval = interval
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self._next: dict[str, float] = {}
        self._cool: dict[str, float] = {}
        self._lock = threading.Lock()

    def reserve(self, host: str) -> float:
        with self._lock:
            now = self.clock()
            until = self._cool.get(host, 0.0)
            if until > now:
                raise ToolError(
                    f"ESPN is limiting requests right now. Try again in about {max(1, math.ceil(until - now))} seconds."
                )
            slot = max(now, self._next.get(host, 0.0))
            delay = slot - now
            if delay > self.max_wait:
                raise ToolError("Too many sports lookups at once. Try again in a few seconds.")
            self._next[host] = slot + self.interval
            return delay

    def cooldown(self, host: str, seconds: float = COOLDOWN_429) -> None:
        with self._lock:
            self._cool[host] = self.clock() + seconds

    async def wait(self, host: str) -> None:
        delay = self.reserve(host)
        if delay > 0:
            await (self.sleep or asyncio.sleep)(delay)

    def clear(self) -> None:
        with self._lock:
            self._next.clear()
            self._cool.clear()


_LIMITER = HostLimiter()
_CACHE: dict[tuple, tuple[float, float, dict]] = {}  # key -> (stored_at, ttl, payload)
_CACHE_LOCK = threading.Lock()  # the cache is shared by every event loop / thread that runs these tools


def _cache_get(key: tuple):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < hit[1]:
        return hit[2]
    return None


def _cache_put(key: tuple, ttl: float, data: dict) -> None:
    now = time.time()
    with _CACHE_LOCK:
        # Expired payloads go first (a college scoreboard can be megabytes), then the least recently stored.
        for k in [k for k, (at, life, _) in _CACHE.items() if now - at >= life]:
            del _CACHE[k]
        _CACHE.pop(key, None)  # re-insert at the end
        _CACHE[key] = (now, ttl, data)
        while len(_CACHE) > CACHE_MAX:
            del _CACHE[next(iter(_CACHE))]


def _retry_after(r: httpx.Response) -> float:
    """Seconds from a 429's Retry-After header (5 s to 5 min), or the default pause."""
    raw = str(r.headers.get("retry-after") or "").strip()
    return float(min(max(int(raw), 5), 300)) if raw.isdigit() else COOLDOWN_429


async def _get_json(http, url: str, params: dict | None = None, *, what: str, ttl: float = UPCOMING_TTL,
                    ttl_for=None, not_found: str | None = None) -> dict:
    """GET an ESPN JSON document through the cache and the per-host limiter. Raises ToolError (never raw errors)."""
    params = {k: str(v) for k, v in (params or {}).items()}
    key = (url, tuple(sorted(params.items())))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    host = httpx.URL(url).host
    await _LIMITER.wait(host)
    try:
        r = await http.get(url, params=params or None, headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
    except httpx.TimeoutException:
        raise ToolError(f"ESPN's sports feed didn't answer in time ({what}). Try again in a moment.") from None
    except httpx.HTTPError as exc:
        raise ToolError(f"Couldn't reach ESPN's sports feed for {what} ({exc.__class__.__name__}).") from None
    if r.status_code == 429:
        pause = _retry_after(r)
        _LIMITER.cooldown(host, pause)
        raise ToolError(f"ESPN is limiting requests right now. Try again in about {math.ceil(pause)} seconds.")
    if r.status_code in (400, 404):
        raise ToolError(not_found or f"ESPN has no {what} for that request.")
    if r.status_code >= 400:
        raise ToolError(
            f"ESPN's sports feed is unavailable right now (HTTP {r.status_code}, {what}). Try again later, or use web_search."
        )
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ToolError(f"ESPN sent an unexpected reply for {what}. Try again later, or use web_search.")
    code = data.get("code")
    if isinstance(code, int) and code >= 400 and not any(k in data for k in ("events", "children", "articles", "sports", "team")):
        raise ToolError(not_found or f"ESPN has no {what} for that request.")
    _cache_put(key, ttl_for(data) if ttl_for else ttl, data)
    return data


def _board_ttl(data: dict) -> float:
    """Short while anything is live, a minute when a game is about to start, longer when nothing will change."""
    now = time.time()
    states: list = []
    soon = False
    for ev in _l(data.get("events")):
        if not isinstance(ev, dict):
            continue
        for comp in _l(ev.get("competitions")) or [ev]:
            if not isinstance(comp, dict):
                continue
            st = _d((_d(comp.get("status")) or _d(ev.get("status"))).get("type"))
            state = st.get("state")
            if state == "in":
                return LIVE_TTL
            if state == "pre":
                dt = _parse_dt(comp.get("date") or ev.get("date"))
                if dt and dt.timestamp() - now < 3600:
                    soon = True
            states.append(state)
    if soon:
        return SOON_TTL
    if states and all(s == "post" for s in states):
        return FINAL_TTL
    return UPCOMING_TTL


# --------------------------------------------------------------------------- dates & times
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3,
    "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)} | {
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8, "sept": 9,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_CURRENT = {"", "current", "latest", "live", "now", "right now", "this week", "upcoming", "next", "recent", "default"}


def _zone():
    """The SPORTS_TIMEZONE override (an IANA name such as America/Chicago), or None for the computer's zone."""
    name = _setting("SPORTS_TIMEZONE")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # unknown zone or no tz database: use the computer's zone
        return None


def _now(tz) -> datetime:
    return datetime.now(tz) if tz else datetime.now().astimezone()


def _loc(dt: datetime, tz) -> datetime:
    return dt.astimezone(tz) if tz else dt.astimezone()


def _parse_dt(value) -> datetime | None:
    """ESPN times look like '2026-11-08T18:00Z' (no seconds) or '2026-11-08T18:00:00Z'."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(d) -> str:
    return f"{_DAYS[d.weekday()]} {_MONTHS[d.month - 1]} {d.day}"


def _is_day_marker(dt: datetime) -> bool:
    """ESPN stamps date-only values (golf start/end days, calendar entries, TBD kick-offs) at midnight or early
    morning US Eastern: 04:00-08:00Z on the hour. Read in a zone west of Eastern they fall on the previous day."""
    u = dt.astimezone(timezone.utc)
    return u.minute == 0 and u.second == 0 and 4 <= u.hour <= 8


def _event_day(dt: datetime, tz, markers: bool = True) -> date:
    """The calendar day meant: ESPN's own day (the UTC date) for a day marker, else the user's local date."""
    if markers and _is_day_marker(dt):
        return dt.astimezone(timezone.utc).date()
    return _loc(dt, tz).date()


def _fmt_dt(dt: datetime | None, tz, *, time_valid: bool = True, with_time: bool = True) -> str | None:
    """'Sun Nov 8, 1:00 PM EST' in the user's zone (locale-independent, works on Windows too)."""
    if dt is None:
        return None
    d = _loc(dt, tz)
    if not with_time:
        return _day(d)
    if not time_valid:  # the time is a placeholder, so is the local date: use the day ESPN means
        return f"{_day(_event_day(dt, tz))}, time TBD"
    hour = d.hour % 12 or 12
    clock = f"{hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    abbr = d.tzname() or ""
    # Short zone names ("EST", "CET") read well; Windows' long ones ("Eastern Standard Time") are left to the
    # result's top-level timezone field.
    if " " in abbr or len(abbr) > 5:
        abbr = ""
    return f"{_day(d)}, {clock} {abbr}".strip()


def _zone_label(now: datetime) -> str:
    off = now.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"{now.tzname() or 'local'} (UTC{sign}{mins // 60:02d}:{mins % 60:02d})"


def parse_date(value, today: date) -> date | None:
    """'today' / 'tonight' / 'yesterday' / 'last night' / 'tomorrow', ISO 'YYYY-MM-DD', 'YYYYMMDD', weekday names
    ('saturday', 'last sunday', 'next friday'), 'Nov 8' / '8 November 2026', 'N days ago' / 'in N days'.
    None (no date) means ESPN's current slate."""
    s = _text(value).lower().strip(" .!?")
    s = re.sub(r"^(on|for)\s+", "", s)
    if s in _CURRENT:
        return None
    if s in ("today", "tonight", "this evening", "this afternoon", "this morning", "today's", "todays"):
        return today
    if s in ("yesterday", "last night", "yesterday's", "yesterdays"):
        return today - timedelta(days=1)
    if s in ("tomorrow", "tomorrow night", "tomorrow's", "tomorrows"):
        return today + timedelta(days=1)
    out = None
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ].*)?", s):
            out = date(int(m[1]), int(m[2]), int(m[3]))
        elif m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s):
            out = date(int(m[1]), int(m[2]), int(m[3]))
        elif m := re.fullmatch(r"(\d{1,3}) days? ago", s):
            out = today - timedelta(days=int(m[1]))
        elif m := re.fullmatch(r"in (\d{1,3}) days?", s):
            out = today + timedelta(days=int(m[1]))
        elif (m := re.fullmatch(r"(this |next |last |coming )?([a-z]+)( night| afternoon| evening)?", s)) and m[2] in _WEEKDAYS:
            wd, prefix = _WEEKDAYS[m[2]], (m[1] or "").strip()
            if prefix == "last":
                out = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
            else:
                delta = (wd - today.weekday()) % 7
                out = today + timedelta(days=delta or (7 if prefix == "next" else 0))
        else:
            mm = re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?,? ?(\d{4})?", s)
            if mm and mm[1] in _MONTH_NUM:
                month, dom, year = _MONTH_NUM[mm[1]], int(mm[2]), mm[3]
            else:
                mm = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)\.?,? ?(\d{4})?", s)
                if not (mm and mm[2] in _MONTH_NUM):
                    raise ValueError(s)
                month, dom, year = _MONTH_NUM[mm[2]], int(mm[1]), mm[3]
            if year:
                out = date(int(year), month, dom)
            else:  # no year: the nearest such date to today
                options = []
                for y in (today.year - 1, today.year, today.year + 1):
                    try:
                        options.append(date(y, month, dom))
                    except ValueError:
                        pass
                if not options:
                    raise ValueError(s)
                out = min(options, key=lambda d: abs((d - today).days))
    except ValueError:
        out = None
    if out is None or not (1950 <= out.year <= today.year + 2):
        raise ToolError(
            f"I couldn't read the date '{_text(value)}'. Use YYYY-MM-DD, 'today', 'yesterday', 'tomorrow' or a weekday."
        )
    return out


# --------------------------------------------------------------------------- name matching
_PUNCT = re.compile(r"[^\w\s]")


def _norm(value) -> str:
    s = unicodedata.normalize("NFKD", _text(value)).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s.replace("'", ""))
    return " ".join(s.replace("_", " ").split())


_SOCCER_ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man u": "manchester united",
    "mufc": "manchester united", "man city": "manchester city", "mcfc": "manchester city",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur", "wolves": "wolverhampton wanderers",
    "villa": "aston villa", "forest": "nottingham forest", "palace": "crystal palace", "gunners": "arsenal",
    "toffees": "everton", "hammers": "west ham united", "west ham": "west ham united", "magpies": "newcastle united",
    "toon": "newcastle united", "newcastle": "newcastle united", "brighton": "brighton and hove albion",
    "barca": "barcelona", "atleti": "atletico madrid", "real": "real madrid", "madrid": "real madrid",
    "betis": "real betis", "sociedad": "real sociedad", "bayern": "bayern munich", "bvb": "borussia dortmund",
    "dortmund": "borussia dortmund", "inter": "internazionale", "inter milan": "internazionale",
    "juve": "juventus", "psg": "paris saint germain", "galaxy": "la galaxy",
}
_TEAM_ALIASES: dict[str, dict[str, str]] = {
    "nba": {
        "sixers": "76ers", "cavs": "cavaliers", "mavs": "mavericks", "wolves": "timberwolves",
        "twolves": "timberwolves", "blazers": "trail blazers", "dubs": "warriors", "nugs": "nuggets",
        "grizz": "grizzlies", "pels": "pelicans", "okc": "thunder", "celts": "celtics",
    },
    "nfl": {
        "niners": "49ers", "pats": "patriots", "bucs": "buccaneers", "jags": "jaguars", "fins": "dolphins",
        "phins": "dolphins", "cards": "cardinals", "g men": "giants", "boys": "cowboys", "hawks": "seahawks",
        "bolts": "chargers", "pack": "packers", "skins": "commanders",
    },
    "nhl": {
        "habs": "canadiens", "leafs": "maple leafs", "bolts": "lightning", "pens": "penguins", "caps": "capitals",
        "canes": "hurricanes", "avs": "avalanche", "sens": "senators", "isles": "islanders", "preds": "predators",
        "hawks": "blackhawks", "wings": "red wings", "jackets": "blue jackets", "knights": "golden knights",
        "nucks": "canucks", "yotes": "coyotes",
    },
    "mlb": {
        "yanks": "yankees", "cards": "cardinals", "jays": "blue jays", "dbacks": "diamondbacks",
        "d backs": "diamondbacks", "halos": "angels", "nats": "nationals", "phils": "phillies", "bucs": "pirates",
        "barves": "braves", "os": "orioles", "redsox": "red sox", "whitesox": "white sox", "chisox": "white sox",
        "stros": "astros", "ms": "mariners",
    },
    "college": {
        "bama": "alabama", "zags": "gonzaga", "nova": "villanova", "cuse": "syracuse", "tosu": "ohio state",
        "the u": "miami", "huskers": "nebraska", "noles": "florida state", "dawgs": "georgia",
    },
}


# City shorthand people put in front of a team name ("LA Rams", "NY Giants", "KC Chiefs"); ESPN spells the city out.
_CITY_SHORT = {
    "la": "los angeles", "ny": "new york", "nyc": "new york", "kc": "kansas city", "sf": "san francisco",
    "tb": "tampa bay", "gb": "green bay", "ne": "new england", "no": "new orleans", "nola": "new orleans",
    "lv": "las vegas", "vegas": "las vegas", "stl": "st louis", "philly": "philadelphia", "sd": "san diego",
    "sj": "san jose", "dc": "washington", "jax": "jacksonville", "okc": "oklahoma city", "slc": "salt lake",
}


def _queries(query, lg: League) -> list[str]:
    """Normalized forms to try for a team/athlete name: the nickname alias first ('man utd' -> 'manchester
    united'), then the words as given, then 'St' read as 'State' ('Ohio St'), then city shorthand spelled out
    ('LA Rams' -> 'los angeles rams') and 'Los Angeles Clippers' -> 'la clippers' (ESPN's own spelling)."""
    q = _norm(query)
    table = _SOCCER_ALIASES if lg.soccer else _TEAM_ALIASES.get("college" if lg.college else lg.key, {})
    out = [table.get(q) or q, q]
    if q.endswith(" st"):
        out.append(q[:-3] + " state")
    words = q.split()
    if words and words[0] in _CITY_SHORT:
        out.append(" ".join([_CITY_SHORT[words[0]]] + words[1:]))
    if q.startswith("los angeles "):
        out.append("la " + q[len("los angeles "):])
    return [x for i, x in enumerate(out) if x and x not in out[:i]]


def _best(queries: list[str], fields: dict) -> int:
    return max((match_score(q, fields) for q in queries), default=0)


def _team_fields(t: dict) -> dict:
    return {
        "abbr": t.get("abbreviation"),
        "display": t.get("displayName") or t.get("name"),
        "short": t.get("shortDisplayName"),
        "name": t.get("name"),
        "nickname": t.get("nickname"),
        "location": t.get("location"),
        "slug": (t.get("slug") or "").replace("-", " ") or None,
    }


def _athlete_fields(a: dict) -> dict:
    display = a.get("displayName") or a.get("fullName") or ""
    return {
        "display": display,
        "full": a.get("fullName"),
        "short": a.get("shortName"),
        "last": a.get("lastName") or (display.split()[-1] if display.split() else None),
    }


def _contains_run(tokens: list[str], sub: list[str]) -> bool:
    n = len(sub)
    return any(tokens[i : i + n] == sub for i in range(len(tokens) - n + 1))


def match_score(query: str, fields: dict) -> int:
    """How well a normalized query names a team/athlete: 100 = abbreviation, 98 = full name, 92 = nickname,
    75 = city only, 70 = whole words of the name, 50 = word prefixes, 0 = no match."""
    q = query
    if not q:
        return 0
    n = {k: _norm(v) for k, v in fields.items() if v}
    if q == n.get("abbr"):
        return 100
    if q in (n.get("display"), n.get("full")):
        return 98
    # ESPN's team lists put the city in "nickname" for pro teams ("New York" for both the Giants and the Jets),
    # so a nickname that just repeats the location counts as the location below, not as a unique name.
    nick = n.get("nickname") if n.get("nickname") != n.get("location") else None
    if q in (n.get("short"), n.get("name")) or (nick and q == nick):
        return 92
    if q == n.get("slug"):
        return 90
    if q == n.get("last"):
        return 85
    if q == n.get("location"):
        return 75
    qt = q.split()
    best = 0
    for key in ("display", "full", "slug", "short"):
        tokens = (n.get(key) or "").split()
        if not tokens:
            continue
        if _contains_run(tokens, qt):
            best = max(best, 70)
        elif len(q) >= 3 and all(any(t.startswith(p) for t in tokens) for p in qt):
            best = max(best, 50)
    return best


def resolve_team(query, teams: list[dict], lg: League) -> dict:
    """Pick the team a user means from ESPN's team list, or raise a friendly ToolError (unknown / ambiguous)."""
    raw = _text(query)
    if not raw:
        raise ToolError("Say which team.")
    for q in _queries(raw, lg):
        scored = sorted(
            ((match_score(q, _team_fields(t)), t) for t in teams if isinstance(t, dict)),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not scored or scored[0][0] < 50:
            continue
        top = scored[0][0]
        tied: dict[str, dict] = {}
        for s, t in scored:
            if s != top:
                break
            tied.setdefault(str(t.get("id")), t)
        # Two different teams that fit equally well ('New York' in the NFL, 'Tigers' in college football) must
        # not be settled by list order: ask.
        if len(tied) > 1:
            picks = list(tied.values())
            names = ", ".join(f"{t.get('displayName')} ({t.get('abbreviation')})" for t in picks[:6])
            more = f" and {len(picks) - 6} more" if len(picks) > 6 else ""
            raise ToolError(f"'{raw}' matches several {lg.name} teams: {names}{more}. Which one?")
        return scored[0][1]
    raise ToolError(f"I couldn't find a {lg.name} team called '{raw}'. Try the team's city, nickname or abbreviation.")


async def _teams(http, lg: League) -> list[dict]:
    # College leagues list hundreds of schools; ESPN pages them unless asked for more (limit is best-effort).
    params = {"limit": 1000} if lg.college else {}
    data = await _get_json(http, f"{SITE}/{lg.path}/teams", params, what=f"the {lg.name} team list", ttl=TEAMS_TTL)
    teams = []
    for sport in _l(data.get("sports")):
        for league in _l(_d(sport).get("leagues")):
            for item in _l(_d(league).get("teams")):
                t = item.get("team") if isinstance(item, dict) else None
                if isinstance(t, dict) and t.get("id"):
                    teams.append(t)
    if not teams:
        raise ToolError(f"ESPN didn't return a team list for {lg.name}.")
    return teams


async def find_team(http, lg: League, query) -> dict:
    return resolve_team(query, await _teams(http, lg), lg)


# --------------------------------------------------------------------------- shared parsing
_SPECIAL_STATUS = {
    "STATUS_POSTPONED": "postponed", "STATUS_CANCELED": "canceled", "STATUS_CANCELLED": "canceled",
    "STATUS_SUSPENDED": "suspended", "STATUS_ABANDONED": "abandoned", "STATUS_DELAYED": "delayed",
    "STATUS_RAIN_DELAY": "delayed", "STATUS_FORFEIT": "final",
}
_ORDER = {"live": 0, "delayed": 1, "suspended": 1, "scheduled": 2, "final": 3}


def game_state(status: dict) -> tuple[str, str, str]:
    """(label, detail, state): label is scheduled / live / final / postponed / canceled / delayed / suspended;
    detail is ESPN's own text ('4:32 - 3rd Quarter', 'Top 7th', "67'", 'Final/OT', 'Postponed');
    state is ESPN's pre / in / post."""
    t = _d(_d(status).get("type"))
    name = str(t.get("name") or "").upper()
    state = t.get("state") or ""
    detail = str(t.get("detail") or t.get("shortDetail") or t.get("description") or "")
    if name in _SPECIAL_STATUS:
        label = _SPECIAL_STATUS[name]
    elif state == "in":
        label = "live"
    elif state == "post":
        label = "final" if t.get("completed", True) else (str(t.get("description") or "ended").lower())
    else:
        label = "scheduled"
    return label, detail, state


def _num(v):
    """ESPN scores arrive as '102', 102.0, {'value': 102.0, 'displayValue': '102'} or '-18' / 'E' for golf."""
    if isinstance(v, dict):
        v = v.get("displayValue") if v.get("displayValue") not in (None, "") else v.get("value")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else v
    s = str(v).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        f = float(s)
        return int(f) if f.is_integer() else f
    return s or None


def _record(c: dict) -> str | None:
    recs = c.get("records") or c.get("record") or []
    if isinstance(recs, str):
        return recs
    if not isinstance(recs, list):
        return None
    for r in recs:
        if isinstance(r, dict) and (r.get("type") == "total" or str(r.get("name") or "").lower() in ("overall", "all splits", "total")):
            return r.get("summary") or r.get("displayValue")
    first = recs[0] if recs and isinstance(recs[0], dict) else {}
    return first.get("summary") or first.get("displayValue")


def _venue(v) -> str | None:
    if not isinstance(v, dict):
        return None
    name = v.get("fullName") or v.get("shortName") or v.get("name")
    addr = _d(v.get("address"))
    place = ", ".join(x for x in (addr.get("city"), addr.get("state") or addr.get("country")) if x)
    return ", ".join(x for x in (name, place) if x) or None


def _tv(comp: dict) -> str | None:
    names: list[str] = []
    casts = [b for b in _l(comp.get("broadcasts")) if isinstance(b, dict)]

    def market(b: dict) -> str:  # scoreboards: "national"; schedules: {"type": "National"}
        m = b.get("market")
        return str(m.get("type") or "") if isinstance(m, dict) else str(m or "")

    casts.sort(key=lambda b: 0 if "national" in market(b).lower() else 1)
    for b in casts:
        names.extend(n for n in _l(b.get("names")) if isinstance(n, str))
        media = _d(b.get("media")).get("shortName")
        if media:
            names.append(media)
    if not names:
        for g in _l(comp.get("geoBroadcasts")):
            media = _d(_d(g).get("media"))
            if isinstance(media.get("shortName"), str) and media["shortName"]:
                names.append(media["shortName"])
    if not names and isinstance(comp.get("broadcast"), str) and comp["broadcast"]:
        names.append(comp["broadcast"])
    seen: list[str] = []
    for n in names:
        if isinstance(n, str) and n not in seen:
            seen.append(n)
    return ", ".join(seen[:4]) or None


def _odds(comp: dict) -> str | None:
    odds = _l(comp.get("odds"))
    o = odds[0] if odds and isinstance(odds[0], dict) else None
    if not o:
        return None
    parts = []
    if o.get("details"):
        parts.append(str(o["details"]))
    if o.get("overUnder") not in (None, ""):
        parts.append(f"O/U {o['overUnder']}")
    if not parts:
        return None
    provider = _d(o.get("provider")).get("name")
    return ", ".join(parts) + (f" ({provider})" if provider else "")


def _headline(comp: dict) -> str | None:
    hl = _l(comp.get("headlines"))
    h = hl[0] if hl and isinstance(hl[0], dict) else None
    if not h:
        return None
    return clip(_text(h.get("shortLinkText") or h.get("description") or h.get("title")), 160) or None


def _note(comp: dict) -> str | None:
    parts = []
    notes = _l(comp.get("notes"))
    if notes and isinstance(notes[0], dict) and notes[0].get("headline"):
        parts.append(_text(notes[0]["headline"]))
    series = comp.get("series")
    if isinstance(series, dict) and series.get("summary"):
        parts.append(_text(series["summary"]))
    return " · ".join(parts) or None


def _situation(comp: dict, sport: str, abbr_by_id: dict) -> dict:
    s = comp.get("situation")
    if not isinstance(s, dict) or not s:
        return {}
    out: dict = {}
    if sport == "football":
        bits = []
        pos = abbr_by_id.get(str(s.get("possession"))) if s.get("possession") else None
        if pos:
            bits.append(f"{pos} ball")
        if s.get("downDistanceText") or s.get("shortDownDistanceText"):
            bits.append(_text(s.get("downDistanceText") or s.get("shortDownDistanceText")))
        if s.get("isRedZone"):
            bits.append("red zone")
        if bits:
            out["situation"] = ", ".join(bits)
    elif sport == "baseball":
        bits = []
        if isinstance(s.get("outs"), int):
            bits.append(f"{s['outs']} out{'s' if s['outs'] != 1 else ''}")
        if isinstance(s.get("balls"), int) and isinstance(s.get("strikes"), int):
            bits.append(f"{s['balls']}-{s['strikes']} count")
        runners = [b for b, k in (("1st", "onFirst"), ("2nd", "onSecond"), ("3rd", "onThird")) if s.get(k)]
        bits.append(("runners on " if len(runners) > 1 else "runner on ") + ", ".join(runners) if runners else "bases empty")
        out["situation"] = ", ".join(bits)
    last = s.get("lastPlay")
    if isinstance(last, dict) and last.get("text") and out:
        out["last_play"] = clip(_text(last["text"]), 140)
    return out


def _goals(comp: dict, abbr_by_id: dict) -> tuple[list[str], list[str]]:
    goals, reds = [], []
    for d in _l(comp.get("details")):
        if not isinstance(d, dict) or d.get("shootout"):
            continue
        who = _d((_l(d.get("athletesInvolved")) or [{}])[0])
        name = who.get("shortName") or who.get("displayName") or "?"
        clock = str(_d(d.get("clock")).get("displayValue") or "").strip()
        team = abbr_by_id.get(str(_d(d.get("team")).get("id")))
        tail = f" ({team})" if team else ""
        if d.get("scoringPlay"):
            tag = " (pen)" if d.get("penaltyKick") else " (og)" if d.get("ownGoal") else ""
            goals.append(f"{clock} {name}{tag}{tail}".strip())
        elif d.get("redCard"):
            reds.append(f"{clock} {name}{tail}".strip())
    return goals[:12], reds[:6]


def _competitor_name(c: dict, short: bool = False) -> str:
    t = _d(c.get("team"))
    a = _d(c.get("athlete"))
    if short:
        return t.get("shortDisplayName") or t.get("name") or a.get("shortName") or t.get("displayName") or a.get("displayName") or "?"
    return t.get("displayName") or a.get("displayName") or a.get("fullName") or t.get("name") or "?"


def _competitor_fields(c: dict) -> dict:
    return _team_fields(c["team"]) if isinstance(c.get("team"), dict) else _athlete_fields(_d(c.get("athlete")))


def _summary(first: dict, second: dict, label: str, detail: str, soccer: bool) -> str:
    """'Celtics 102, Knicks 98 · Final/OT' (winner or leader first); 'Arsenal 1, Chelsea 1 · FT (draw)'."""
    fs, ss = first["_score"], second["_score"]
    a, b = first, second
    if label == "final" and (second.get("winner") and not first.get("winner")):
        a, b = second, first
    elif label != "final" and isinstance(fs, (int, float)) and isinstance(ss, (int, float)) and ss > fs:
        a, b = second, first
    text = f"{a['_short']} {a['_score']}, {b['_short']} {b['_score']}"
    if a.get("pens") is not None and b.get("pens") is not None:
        text += f" ({a['pens']}-{b['pens']} on penalties)"
    tag = detail or label.title()
    level = isinstance(fs, (int, float)) and fs == ss and a.get("pens") is None
    if label == "final" and level and not first.get("winner") and not second.get("winner"):
        tag += " (draw)" if soccer else " (tie)"
    return f"{text} · {tag}"


def _side(c: dict, played: bool) -> dict:
    t = _d(c.get("team"))
    side = {"team": _competitor_name(c), "abbr": t.get("abbreviation")}
    rank = _d(c.get("curatedRank")).get("current")
    if isinstance(rank, int) and 0 < rank <= 25:
        side["rank"] = rank
    if played:
        side["score"] = _num(c.get("score"))
        if c.get("shootoutScore") not in (None, ""):
            side["pens"] = _num(c.get("shootoutScore"))
    rec = _record(c)
    if rec:
        side["record"] = rec
    if c.get("winner") is True:
        side["winner"] = True
    return {k: v for k, v in side.items() if v is not None}


def team_game(ev: dict, comp: dict, lg: League, tz) -> dict:
    comps = [c for c in _l(comp.get("competitors")) if isinstance(c, dict)]
    home = next((c for c in comps if c.get("homeAway") == "home"), comps[0] if comps else {})
    away = next((c for c in comps if c.get("homeAway") == "away" and c is not home), None)
    if away is None:
        away = next((c for c in comps if c is not home), {})
    status = _d(comp.get("status")) or _d(ev.get("status"))
    label, detail, state = game_state(status)
    played = state in ("in", "post") and label not in ("postponed", "canceled")
    hs, as_ = _side(home, played), _side(away, played)
    abbr_by_id = {str(c.get("id") or _d(c.get("team")).get("id")): _d(c.get("team")).get("abbreviation") for c in comps}
    habbr, aabbr = hs.get("abbr") or _competitor_name(home, True), as_.get("abbr") or _competitor_name(away, True)
    g: dict = {"matchup": f"{habbr} vs {aabbr}" if lg.soccer else f"{aabbr} @ {habbr}", "status": label}
    if played and (hs.get("score") is not None or as_.get("score") is not None):
        h = dict(hs, _short=_competitor_name(home, True), _score=hs.get("score"))
        a = dict(as_, _short=_competitor_name(away, True), _score=as_.get("score"))
        first, second = (h, a) if lg.soccer else (a, h)
        g["summary"] = _summary(first, second, label, detail, lg.soccer)
    elif label != "scheduled" and detail:
        g["detail"] = detail
    dt = _parse_dt(comp.get("date") or ev.get("date"))
    if label != "live":
        g["start"] = _fmt_dt(dt, tz, time_valid=comp.get("timeValid", ev.get("timeValid", True)) is not False)
    g["away"], g["home"] = as_, hs
    if comp.get("neutralSite"):
        g["neutral_site"] = True
    if label in ("live", "delayed"):
        g.update(_situation(comp, lg.sport, abbr_by_id))
    if lg.soccer and played:
        goals, reds = _goals(comp, abbr_by_id)
        if goals:
            g["goals"] = goals
        if reds:
            g["red_cards"] = reds
    extras = {
        "note": _note(comp),
        "venue": _venue(comp.get("venue")),
        "tv": _tv(comp) if label in ("scheduled", "live", "delayed") else None,
        "odds": _odds(comp) if label in ("scheduled", "delayed") else None,
        "headline": _headline(comp),
    }
    g.update({k: v for k, v in extras.items() if v})
    g["_sort"] = (_ORDER.get(label, 4), dt.timestamp() if dt else 0)
    g["_fields"] = [_competitor_fields(c) for c in comps]
    return g


def _is_race(name: str) -> bool:
    return _norm(name) in ("race", "grand prix", "gp", "main race")


def racing_event(ev: dict, tz, focus: str | None = None) -> dict:
    sessions, fields, winner = [], [], None
    for comp in _l(ev.get("competitions")):
        if not isinstance(comp, dict):
            continue
        label, detail, _ = game_state(comp.get("status"))
        typ = _d(comp.get("type"))
        name = str(typ.get("text") or typ.get("abbreviation") or "Session")
        s: dict = {"session": name, "status": label}
        if label != "final":
            s["start"] = _fmt_dt(_parse_dt(comp.get("date") or comp.get("startDate")), tz)
        drivers = sorted(
            (c for c in _l(comp.get("competitors")) if isinstance(c, dict)),
            key=lambda c: c.get("order") if isinstance(c.get("order"), int) else 999,
        )
        fields.extend(_competitor_fields(c) for c in drivers)
        if label in ("final", "live") and drivers:
            n = 10 if _is_race(name) else 3
            s["top"] = [f"{c.get('order', i + 1)}. {_competitor_name(c)}" for i, c in enumerate(drivers[:n])]
            if label == "live" and detail:
                s["detail"] = detail
            if _is_race(name) and label == "final":
                winner = _competitor_name(drivers[0])
        if focus:
            mine = [c for c in drivers if match_score(focus, _competitor_fields(c)) >= 50]
            if mine and label in ("final", "live"):
                s["focus"] = ", ".join(f"P{c.get('order', '?')} {_competitor_name(c)}" for c in mine[:2])
        sessions.append(s)
    labels = [s["status"] for s in sessions]
    if not labels:
        label = game_state(ev.get("status"))[0]
    elif "live" in labels:
        label = "live"
    elif winner or all(x == "final" for x in labels):
        label = "final"
    elif "final" in labels:
        label = "weekend underway"
    else:
        label = labels[0] if all(x == labels[0] for x in labels) else "scheduled"
    start, end = _parse_dt(ev.get("date")), _parse_dt(ev.get("endDate"))
    out = {
        "event": ev.get("name") or ev.get("shortName") or "Race weekend",
        "status": label,
        "dates": _date_span(start, end, tz),
        "circuit": _venue(ev.get("circuit") or _d((_l(ev.get("competitions")) or [{}])[0]).get("venue")),
        "winner": winner,
        "sessions": sessions,
    }
    out = {k: v for k, v in out.items() if v}
    out["_fields"] = fields
    return out


def _date_span(start: datetime | None, end: datetime | None, tz, markers: bool = False) -> str | None:
    """'Fri Nov 6 - Sun Nov 8'. With markers, midnight-Eastern day stamps (golf, calendars) keep ESPN's day."""
    if not start:
        return None
    s = _event_day(start, tz, markers)
    text = _day(s)
    e = _event_day(end, tz, markers) if end else None
    if e and e > s:
        text += f" - {_day(e)}"
    return text


def _to_par(v):
    s = _num(v)
    if isinstance(s, (int, float)):
        return s
    if isinstance(s, str) and s.upper() in ("E", "EVEN"):
        return 0
    if isinstance(s, str) and re.fullmatch(r"\+\d+", s):
        return int(s[1:])
    return None


def golf_event(ev: dict, tz, focus: str | None = None, top: int = 10) -> dict:
    comp = _d((_l(ev.get("competitions")) or [{}])[0])
    label, detail, _ = game_state(_d(comp.get("status")) or _d(ev.get("status")))

    def order(c):
        v = c.get("sortOrder") or c.get("order")
        return v if isinstance(v, (int, float)) else 9999

    players = sorted((c for c in _l(comp.get("competitors")) if isinstance(c, dict)), key=order)
    pars = [_to_par(c.get("score")) for c in players]
    rows = []
    for i, c in enumerate(players):
        pos = _d(_d(c.get("status")).get("position")).get("displayName")
        if not pos and pars[i] is not None:  # ESPN's scoreboard has no position field: rank by score to par
            better = sum(1 for p in pars if p is not None and p < pars[i])
            pos = ("T" if pars.count(pars[i]) > 1 else "") + str(better + 1)
        score = _num(c.get("score"))
        if isinstance(score, int):
            score = "E" if score == 0 else f"{score:+d}"
        row = {"pos": pos or str(c.get("order") or i + 1), "player": _competitor_name(c), "score": score}
        rounds = [_num(ls.get("value")) for ls in _l(c.get("linescores")) if isinstance(ls, dict) and ls.get("value")]
        if rounds:
            row["rounds"] = rounds
        thru = _d(c.get("status")).get("thru")
        if thru not in (None, "", 0) and label == "live":
            row["thru"] = thru
        rows.append({k: v for k, v in row.items() if v is not None})
    start, end = _parse_dt(ev.get("date")), _parse_dt(ev.get("endDate"))
    dates = _date_span(start, end, tz, markers=True)  # golf days are stamped at midnight Eastern
    out = {
        "event": ev.get("name") or ev.get("shortName") or "Tournament",
        "status": label,
        "detail": detail if label != "scheduled" else None,
        "dates": dates,
        "venue": _venue(comp.get("venue") or ev.get("venue")),
        "leader": f"{rows[0]['player']} ({rows[0].get('score')})" if rows and label in ("live", "final") else None,
        "winner": rows[0]["player"] if rows and label == "final" else None,
        "leaderboard": rows[:top] if label in ("live", "final") else None,
        "field_size": len(rows) or None,
    }
    if focus:
        mine = [r for c, r in zip(players, rows) if match_score(focus, _competitor_fields(c)) >= 50]
        if mine:
            out["players"] = mine[:4]
    out = {k: v for k, v in out.items() if v is not None}
    out["_fields"] = [_competitor_fields(c) for c in players]
    return out


def mma_event(ev: dict, tz, focus: str | None = None) -> dict:
    fights, fields = [], []
    for comp in _l(ev.get("competitions")):
        if not isinstance(comp, dict):
            continue
        status = _d(comp.get("status"))
        label, detail, _ = game_state(status)
        fighters = [c for c in _l(comp.get("competitors")) if isinstance(c, dict)]
        fighters.sort(key=lambda c: c.get("order") if isinstance(c.get("order"), int) else 9)
        f_fields = [_competitor_fields(c) for c in fighters]
        if focus and not any(match_score(focus, f) >= 50 for f in f_fields):
            continue
        fields.extend(f_fields)
        bout = " vs ".join(
            _competitor_name(c) + (f" ({_record(c)})" if _record(c) else "") for c in fighters
        ) or (comp.get("name") or "Bout")
        f: dict = {"bout": bout, "status": label}
        typ = _d(comp.get("type"))
        weight = typ.get("text") or typ.get("abbreviation")
        if isinstance(weight, str) and not weight.isdigit():
            f["weight_class"] = weight
        segment = _d(comp.get("cardSegment")).get("description")
        if segment:
            f["card"] = segment
        if label == "final":
            w = next((c for c in fighters if c.get("winner")), None)
            f["winner"] = _competitor_name(w) if w else "no winner (draw / no contest)"
            result = _d(status.get("result"))
            method = result.get("displayName") or result.get("shortDisplayName") or result.get("name")
            if method:
                f["method"] = method
            if status.get("period"):
                f["round"] = status["period"]
            if status.get("displayClock") and status.get("displayClock") != "0:00":
                f["time"] = status["displayClock"]
        elif label == "live":
            f["detail"] = detail
        else:
            f["start"] = _fmt_dt(_parse_dt(comp.get("date") or comp.get("startDate")), tz)
        fights.append(f)
    label, detail, _ = game_state(ev.get("status"))
    first = _d((_l(ev.get("competitions")) or [{}])[0])
    out = {
        "event": ev.get("name") or ev.get("shortName") or "Fight card",
        "status": label,
        "start": _fmt_dt(_parse_dt(ev.get("date")), tz),
        "venue": _venue(first.get("venue") or ev.get("venue")),
        "fights": fights[:16],
    }
    out = {k: v for k, v in out.items() if v}
    out["_fields"] = fields
    return out


def _strip_private(items: list[dict]) -> list[dict]:
    for it in items:
        for k in [k for k in it if k.startswith("_")]:
            it.pop(k)
    return items


_OPTIONAL_FIELDS = ("last_play", "headline", "odds", "venue", "neutral_site", "red_cards", "goals", "tv", "note",
                    "situation", "circuit", "field_size")
_NEWS_OPTIONAL = ("type", "premium", "age", "byline", "summary")  # never the headline: it is the story
_SCHEDULE_OPTIONAL = ("note", "tv")


def _fit(out: dict, list_key: str, budget: int = RESULT_BUDGET, *, optional: tuple = _OPTIONAL_FIELDS,
         hint: str = "pass team for a specific one", inner: str | None = None) -> dict:
    """Keep the JSON under `budget` characters: drop optional per-item fields first, then trailing items, then
    (with `inner`, e.g. a standings table's 'teams') trailing rows of the last item left. Says so in a note."""
    def size() -> int:
        return len(json.dumps(out, ensure_ascii=False, default=str))

    if size() <= budget:
        return out
    items = out.get(list_key)
    if not isinstance(items, list):
        return out
    for field in optional:
        for it in items:
            if isinstance(it, dict):
                it.pop(field, None)
        if size() <= budget:
            return out
    total = len(items)
    while len(items) > 1 and size() > budget:
        items.pop()
    cut = ""
    if len(items) < total:
        cut = f"Trimmed to {len(items)} of {total} to keep it short; {hint}."
    if inner and items and isinstance(items[-1], dict) and isinstance(items[-1].get(inner), list):
        rows = items[-1][inner]
        n = len(rows)
        while len(rows) > 1 and size() > budget:
            rows.pop()
        if len(rows) < n:
            cut = (cut + f" Showing the first {len(rows)} of {n} rows; {hint}.").strip()
    if cut:
        out["note"] = (str(out.get("note") or "") + " " + cut).strip()
    return out


# --------------------------------------------------------------------------- scoreboard
_COLLEGE_GROUPS = {  # ESPN group ids: all FBS games / all Division I games (best-effort, used with a team filter)
    "college-football": "80",
    "mens-college-basketball": "50",
    "womens-college-basketball": "50",
}


def _calendar(data: dict) -> list:
    leagues = _l(data.get("leagues"))
    return _l(_d(leagues[0]).get("calendar")) if leagues else []


def _next_dates(data: dict, after: date) -> list[str]:
    """For daily leagues ESPN's scoreboard lists the season's game dates in leagues[0].calendar, as strings such as
    '2026-11-07T08:00Z' (the ESPN day, so read in UTC)."""
    out = set()
    for item in _calendar(data):
        if isinstance(item, str):
            dt = _parse_dt(item)
            if dt and dt.astimezone(timezone.utc).date() > after:
                out.add(dt.astimezone(timezone.utc).date())
    return [_day(d) for d in sorted(out)[:3]]


def _coming_up(data: dict, after: date, tz) -> tuple[list[str], bool]:
    """Weekly and event leagues list calendar entries instead of dates: NFL / college football nest
    {label: 'Week 10', detail: 'Nov 5-11', startDate, endDate} under each season type's 'entries'; F1, golf and
    UFC list {label: event name, startDate, endDate}. Returns the next few still ahead of `after` and whether the
    calendar is week-based."""
    found: list[tuple[datetime, str]] = []
    weekly = False

    def walk(items, depth: int = 0) -> None:
        nonlocal weekly
        for item in items:
            if not isinstance(item, dict) or depth > 2:
                continue
            if _l(item.get("entries")):
                weekly = True
                walk(item["entries"], depth + 1)
                continue
            start, end = _parse_dt(item.get("startDate")), _parse_dt(item.get("endDate"))
            label = _text(item.get("label") or item.get("alternateLabel"))
            if not start or not label:
                continue
            if _event_day(start, tz) <= after and not (end and _event_day(end, tz) > after):
                continue
            detail = _text(item.get("detail"))
            when = detail or _date_span(start, end, tz, markers=True) or ""
            found.append((start, f"{label} ({when})" if when else label))

    walk(_calendar(data))
    found.sort(key=lambda pair: pair[0])
    return [text for _, text in found[:3]], weekly


async def _scoreboard(ctx, lg: League, d: date | None, focus: str | None) -> dict:
    params: dict = {}
    if d:
        params["dates"] = d.strftime("%Y%m%d")
    if focus and lg.key in _COLLEGE_GROUPS:
        params.update(groups=_COLLEGE_GROUPS[lg.key], limit=500)
    when = _day(d) if d else "the current slate"
    return await _get_json(
        ctx.http,
        f"{SITE}/{lg.path}/scoreboard",
        params,
        what=f"{lg.name} scores",
        ttl_for=_board_ttl,
        not_found=f"ESPN has no {lg.name} scoreboard for {when}.",
    )


def _parse_board(data: dict, lg: League, tz, focus: str | None) -> list[dict]:
    focus = _queries(focus, lg)[0] if focus and _norm(focus) else None  # normalized for match_score
    items = []
    for ev in _l(data.get("events")):
        if not isinstance(ev, dict):
            continue
        if lg.kind == "racing":
            items.append(racing_event(ev, tz, focus))
        elif lg.kind == "golf":
            items.append(golf_event(ev, tz, focus))
        elif lg.kind == "mma":
            items.append(mma_event(ev, tz, focus))
        else:
            for comp in _l(ev.get("competitions"))[:1]:
                if isinstance(comp, dict):
                    items.append(team_game(ev, comp, lg, tz))
    return items


def _filter_focus(items: list[dict], focus: str, lg: League) -> list[dict]:
    qs = _queries(focus, lg)
    scored = [(max((_best(qs, f) for f in it.get("_fields") or []), default=0), it) for it in items]
    best = max((s for s, _ in scored), default=0)
    if best < 50:
        return []
    if lg.kind == "mma":  # fights were already filtered inside each card
        return [it for s, it in scored if s >= 50 and it.get("fights")]
    if lg.kind in ("racing", "golf"):
        return [it for s, it in scored if s >= 50]
    return [it for s, it in scored if s == best]


async def _team_next_last(ctx, lg: League, team: str, tz) -> dict:
    """For 'do the Lakers play tonight?' when they don't: their last result and next game (best-effort)."""
    try:
        t = await find_team(ctx.http, lg, team)
    except ToolError as exc:  # unknown or ambiguous name: say so, so the model can ask or retry
        return {"note": str(exc)}
    except Exception:  # only a hint: never let it sink the scoreboard answer
        return {}
    try:
        data = await _schedule_data(ctx, lg, t)
    except Exception:
        return {}
    rows = _schedule_rows(data, str(t.get("id")), lg, tz)
    out = {}
    if rows["recent"]:
        out["last_game"] = rows["recent"][0]
    if rows["upcoming"]:
        out["next_game"] = rows["upcoming"][0]
    return out


@tool(
    "get_sports_scores",
    "Live scores, results and game times for ONE league on ONE day, from ESPN's public scoreboard feed (unofficial; "
    "live scores can trail the broadcast by up to a minute). Use it for: 'what's the Lakers score', 'did the Chiefs "
    "win', 'who plays tonight', 'Premier League results today', 'F1 race result / when is qualifying', 'PGA "
    "leaderboard', 'UFC card results'. Each game gives its status - scheduled (start time in the user's local time "
    "zone), live (clock and period, e.g. '4:32 - 3rd Quarter', 'Top 7th', \"67'\"), final, or postponed/canceled - "
    "with teams, scores, records, venue, TV, betting line and recap headline; F1 gives each session with the top "
    "finishers, golf a leaderboard, UFC the fight card with winners and methods. Leagues: nfl, college-football, "
    "nba, wnba, mens-college-basketball, womens-college-basketball, mlb, nhl, mls, nwsl, epl (Premier League), "
    "laliga, bundesliga, serie-a, ligue-1, liga-mx, ucl (Champions League), europa, f1, ufc, pga - or any ESPN "
    "'sport/league' slug such as 'soccer/ned.1'. Omit date for ESPN's current slate (this week's games for NFL and "
    "college football; the current or next event for F1, golf and UFC). For a team's next or last game on other "
    "days use get_team_schedule; for league tables use get_sports_standings.",
    {
        "league": {"type": "string", "description": "League, e.g. 'nba', 'nfl', 'epl', 'college-football', 'f1', 'pga'."},
        "date": {
            "type": "string",
            "description": "Optional day: 'today', 'yesterday', 'tomorrow', a weekday ('sunday', 'last saturday') or "
            "YYYY-MM-DD. Omit for ESPN's current slate.",
        },
        "team": {
            "type": "string",
            "description": "Optional filter: team name, city, nickname or abbreviation ('Lakers', 'KC', 'Man City'), "
            "or a driver/golfer/fighter ('Verstappen', 'Scheffler').",
        },
    },
    ["league"],
    label="Checking scores",
)
async def get_sports_scores(ctx, league: str, date: str | None = None, team: str | None = None):
    lg = resolve_league(league)
    tz = _zone()
    now = _now(tz)
    d = parse_date(date, now.date())
    focus = _text(team) or None
    data = await _scoreboard(ctx, lg, d, focus)
    items = _parse_board(data, lg, tz, focus)
    total = len(items)
    if focus:
        items = _filter_focus(items, focus, lg)
    list_key = "games" if lg.kind == "team" else "events"
    if lg.kind == "team":
        items.sort(key=lambda g: g["_sort"])
    when = _day(d) if d else "current"
    out: dict = {"status": "", "league": lg.name, "date": d.isoformat() if d else "current"}
    week = (data.get("week") or {}).get("number") if isinstance(data.get("week"), dict) else None
    if week and not d:
        out["week"] = week
    counts: dict[str, int] = {}
    for it in items:
        counts[it.get("status", "?")] = counts.get(it.get("status", "?"), 0) + 1
    noun = "game" if lg.kind == "team" else "fight card" if lg.kind == "mma" else "event"
    day_txt = f" on {when}" if d else ""
    if items:
        parts = [f"{len(items)} {noun}{'s' if len(items) != 1 else ''}"]
        parts += [f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: _ORDER.get(kv[0], 4))]
        out["status"] = f"{lg.name}{day_txt}: " + " · ".join(parts)
    elif focus and total:
        out["status"] = f"No {lg.name} {noun} for '{focus}'{day_txt}"
    else:
        out["status"] = f"No {lg.name} {noun}s{day_txt}"
    if lg.kind == "team" and len(items) > MAX_GAMES:
        out["note"] = f"Showing {MAX_GAMES} of {len(items)} games; pass team to find a specific one."
        items = items[:MAX_GAMES]
    elif lg.college and not focus and items:
        # Without a team ESPN's college scoreboard is its default view (mostly Top 25 and featured games).
        out["note"] = "ESPN's default college view (mostly Top 25 and featured games); pass team for any other school."
    out[list_key] = _strip_private(items)
    if not items:
        if focus and lg.kind == "team":
            out.update(await _team_next_last(ctx, lg, focus, tz))
        weekly = False
        if not focus or not total:
            after = d or now.date()
            upcoming = _next_dates(data, after)
            if upcoming:
                out["next_dates_with_games"] = upcoming
            else:
                ahead, weekly = _coming_up(data, after, tz)
                if ahead:
                    out["coming_up"] = ahead
        if d and not total and (weekly or lg.kind != "team"):
            out.setdefault("note", f"{lg.name} is not on every day: omit date for the current {'week' if weekly else 'or next event'}.")
        elif lg.kind == "team":
            out.setdefault("note", "Try get_team_schedule for a team's next game, or another date.")
    out["timezone"] = _zone_label(now)
    out["as_of"] = _fmt_dt(now, tz)
    out["source"] = SOURCE
    return _fit(out, list_key)


# --------------------------------------------------------------------------- standings
def _flatten_groups(node: dict) -> list[dict]:
    """ESPN nests standings as league -> conference -> division ('children'); a node with standings.entries is a
    table. Returns [{name, abbr, entries, season}] for every table, in ESPN's order."""
    out = []
    if not isinstance(node, dict):
        return out
    st = _d(node.get("standings"))
    entries = [e for e in _l(st.get("entries")) if isinstance(e, dict)]
    if entries:
        out.append({
            "name": node.get("name") or st.get("name") or st.get("displayName") or "",
            "abbr": node.get("abbreviation") or "",
            "entries": entries,
            "season": st.get("seasonDisplayName") or node.get("seasonDisplayName"),
        })
    for child in _l(node.get("children")):
        out.extend(_flatten_groups(child))
    return out


def _stats(entry: dict) -> dict:
    out: dict = {}
    for s in _l(entry.get("stats")):
        if not isinstance(s, dict):
            continue
        for key in (s.get("name"), s.get("type"), (s.get("abbreviation") or "").upper() or None):
            if key and key not in out:
                out[key] = s
    return out


def _stat_value(stats: dict, *names):
    for n in names:
        s = stats.get(n)
        if s is None:
            continue
        v = s.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v) if float(v).is_integer() else round(v, 3)
        dv = _num(s.get("displayValue"))
        if dv is not None:
            return dv
    return None


def _stat_display(stats: dict, *names):
    for n in names:
        s = stats.get(n)
        if s is not None and s.get("displayValue") not in (None, ""):
            return str(s["displayValue"])
    return None


def standings_row(entry: dict, lg: League) -> dict:
    stats = _stats(entry)
    who = _d(entry.get("team")) or _d(entry.get("athlete"))
    row: dict = {"team": who.get("displayName") or who.get("name") or who.get("fullName") or "?"}
    if who.get("abbreviation"):
        row["abbr"] = who["abbreviation"]
    w, l = _stat_value(stats, "wins", "W"), _stat_value(stats, "losses", "L")
    points = _stat_value(stats, "points", "championshipPts", "totalPoints", "PTS")
    if lg.kind in ("racing", "golf"):
        row["pts"] = points
        if w:
            row["wins"] = w
    elif lg.soccer:
        d = _stat_value(stats, "ties", "draws", "D", "T")
        if w is not None and l is not None:
            row["record"] = f"{w}-{d or 0}-{l}"
        row["pts"] = points
        row["gp"] = _stat_value(stats, "gamesPlayed", "GP")
        row["gd"] = _stat_display(stats, "pointDifferential", "GD")
    elif lg.sport == "hockey":
        otl = _stat_value(stats, "otLosses", "overtimeLosses", "OTL")
        if w is not None and l is not None:
            row["record"] = f"{w}-{l}-{otl or 0}"
        row["pts"] = points
        row["gp"] = _stat_value(stats, "gamesPlayed", "GP")
        row["streak"] = _stat_display(stats, "streak", "STRK")
    else:
        t = _stat_value(stats, "ties", "T")
        if w is not None and l is not None:
            row["record"] = f"{w}-{l}" + (f"-{t}" if t else "")
        row["pct"] = _stat_display(stats, "winPercent", "PCT")
        gb = _stat_display(stats, "gamesBehind", "GB")
        if gb is not None:
            row["gb"] = gb
        row["streak"] = _stat_display(stats, "streak", "STRK")
    clinch = _stat_display(stats, "clincher", "CLINCH")
    if clinch and clinch.strip().lower() == "e":  # ESPN's code for "eliminated from playoff contention"
        row["eliminated"] = True
    elif clinch and clinch not in ("-", "0"):
        row["clinched"] = clinch
    note = entry.get("note")
    if isinstance(note, dict) and note.get("description"):
        row["note"] = _text(note["description"])
    row["_rank"] = _stat_value(stats, "rank", "R")
    row["_seed"] = _stat_value(stats, "playoffSeed", "SEED")
    row["_pct"] = _stat_value(stats, "winPercent", "PCT")
    row["_pts"] = points if (lg.soccer or lg.sport == "hockey" or lg.kind in ("racing", "golf")) else None
    row["_gd"] = _stat_value(stats, "pointDifferential", "GD")
    row["_w"] = w
    return row


def _sorted_rows(rows: list[dict]) -> list[dict]:
    def all_have(key):
        return rows and all(isinstance(r.get(key), (int, float)) and r.get(key) > 0 for r in rows)

    if all_have("_rank"):
        rows.sort(key=lambda r: r["_rank"])
    elif all_have("_seed"):
        rows.sort(key=lambda r: r["_seed"])
    elif any(isinstance(r.get("_pts"), (int, float)) for r in rows):
        rows.sort(key=lambda r: (-(r.get("_pts") or 0), -(r.get("_gd") if isinstance(r.get("_gd"), (int, float)) else 0)))
    elif any(isinstance(r.get("_pct"), (int, float)) for r in rows):
        rows.sort(key=lambda r: (-(r.get("_pct") or 0), -(r.get("_w") or 0)))
    out = []
    for i, r in enumerate(rows, 1):
        clean = {"rank": i}
        clean.update({k: v for k, v in r.items() if not k.startswith("_") and v is not None})
        out.append(clean)
    return out


_GROUP_EXPAND = {"al": "american league", "nl": "national league"}


def _group_matches(query: str, grp: dict) -> bool:
    """'AFC' / 'AFC East' / 'East' / 'AL West' / 'SEC' / 'Constructors' against a table's name or abbreviation."""
    q = " ".join(re.sub(r"\b(conference|division|standings|table)\b", " ", _norm(query)).split())
    if not q:
        return False
    name, abbr = _norm(grp.get("name")), _norm(grp.get("abbr"))
    if q in (name, abbr):
        return True
    tokens = name.split() + abbr.split()

    def has(t: str) -> bool:
        return any(tok.startswith(t) or (len(t) > 3 and t.endswith("s") and tok.startswith(t[:-1])) for tok in tokens)

    plain = q.split()
    expanded = [w for t in plain for w in _GROUP_EXPAND.get(t, t).split()]
    return all(has(t) for t in plain) or all(has(t) for t in expanded)


@tool(
    "get_sports_standings",
    "Current standings / league table for one league from ESPN's public feed (unofficial): rank, team, record "
    "(W-L, W-L-T for NFL ties, W-D-L for soccer, W-L-OTL for NHL), win % or points, games back, streak, goal "
    "difference and clinch / qualification notes; F1 gives the drivers' and constructors' championships. Use it for "
    "'NBA East standings', 'where are the Bills in the AFC East', 'Premier League table', 'F1 championship "
    "standings'. Supported: nfl, college-football, nba, wnba, mens-college-basketball, womens-college-basketball, "
    "mlb, nhl, mls, nwsl, epl, laliga, bundesliga, serie-a, ligue-1, liga-mx, ucl, europa, f1 (UFC has no "
    "standings; golf standings are best-effort). The optional group narrows to one conference, division or table, "
    "e.g. 'AFC', 'AFC East', 'Eastern', 'AL West', 'Metropolitan', 'SEC', 'Constructor'.",
    {
        "league": {"type": "string", "description": "League, e.g. 'nfl', 'nba', 'epl', 'f1'."},
        "group": {"type": "string", "description": "Optional conference/division/table name, e.g. 'AFC East', 'West'."},
    },
    ["league"],
    label="Pulling standings",
)
async def get_sports_standings(ctx, league: str, group: str | None = None):
    lg = resolve_league(league)
    if lg.kind == "mma":
        raise ToolError("UFC has no league standings on ESPN. Try get_sports_scores for the latest card, or get_sports_news.")
    url = f"{STANDINGS_BASE}/{lg.path}/standings"
    what = f"{lg.name} standings"
    data = await _get_json(ctx.http, url, {}, what=what, ttl=STANDINGS_TTL,
                           not_found=f"ESPN doesn't publish {lg.name} standings in this feed.")
    groups = _flatten_groups(data)
    if not groups:
        raise ToolError(f"ESPN has no {lg.name} standings right now (offseason, or not published for this league).")
    group = _text(group)
    if group:
        chosen = [g for g in groups if _group_matches(group, g)]
        if not chosen and lg.kind == "team":
            # Divisions: ESPN's level=3 view (best-effort; the default view stops at conferences).
            try:
                deeper = _flatten_groups(await _get_json(ctx.http, url, {"level": 3}, what=what, ttl=STANDINGS_TTL))
            except ToolError:
                deeper = []
            chosen = [g for g in deeper if _group_matches(group, g)]
            if not chosen and deeper:
                groups = groups + [g for g in deeper if g["name"] not in {x["name"] for x in groups}]
        if not chosen:
            names = ", ".join(dict.fromkeys(g["name"] for g in groups))
            raise ToolError(f"No {lg.name} group called '{group}'. Available: {clip(names, 400)}.")
        groups = chosen
    tables = [{"group": g["name"], "teams": _sorted_rows([standings_row(e, lg) for e in g["entries"]])} for g in groups]
    total = sum(len(t["teams"]) for t in tables)
    out: dict = {
        "status": f"{lg.name} standings · " + (f"{tables[0]['group']}" if len(tables) == 1 else f"{len(tables)} groups"),
        "league": lg.name,
        "season": next((g["season"] for g in groups if g.get("season")), None),
    }
    if total > STANDINGS_ROW_CAP:
        if len(tables) == 1:
            tables[0]["teams"] = tables[0]["teams"][:STANDINGS_ROW_CAP]
            out["note"] = f"Showing the top {STANDINGS_ROW_CAP} of {total}."
        else:
            for t in tables:
                t["teams"] = t["teams"][:5]
            out["note"] = "Showing the top 5 of each group; pass group (e.g. a conference name) for the full table."
    out["groups"] = tables
    out["source"] = SOURCE
    out = {k: v for k, v in out.items() if v is not None}
    return _fit(out, "groups", optional=(), hint="pass group for one table", inner="teams")


# --------------------------------------------------------------------------- team schedule
_SAFE_ID = re.compile(r"[A-Za-z0-9-]{1,24}")


def _merge_events(data: dict, extra_payload) -> dict:
    extra = [e for e in _l(_d(extra_payload).get("events")) if isinstance(e, dict)]
    if not extra:
        return data
    seen = {e.get("id") for e in _l(data.get("events")) if isinstance(e, dict)}
    return dict(data, events=_l(data.get("events")) + [e for e in extra if e.get("id") not in seen])


def _days_since_last_game(data: dict) -> float | None:
    now = _now(None).timestamp()
    past = [dt.timestamp() for ev in _l(data.get("events")) if isinstance(ev, dict)
            for dt in [_parse_dt(ev.get("date"))] if dt and dt.timestamp() <= now]
    return (now - max(past)) / 86400 if past else None


async def _schedule_data(ctx, lg: League, team: dict) -> dict:
    tid = str(team.get("id") or "")
    name = team.get("displayName") or "that team"
    if not _SAFE_ID.fullmatch(tid):  # ESPN ids are numeric; never put anything else into the URL path
        raise ToolError(f"ESPN's team list gave no usable id for {name}.")
    url = f"{SITE}/{lg.path}/teams/{tid}/schedule"
    what = f"the {name} schedule"
    data = await _get_json(ctx.http, url, {}, what=what, ttl_for=_board_ttl, not_found=f"ESPN has no schedule for the {name}.")
    rows = _schedule_rows(data, tid, lg, None)
    if rows["upcoming"] or rows["live"]:
        return data
    if lg.soccer:
        # Soccer schedules default to results; fixture=true lists upcoming matches (best-effort, merged in).
        params = {"fixture": "true"}
    else:
        # The schedule defaults to the regular season (seasontype=2): once it ends, a team still in the playoffs would
        # show no next game. Ask for the postseason (seasontype=3) in the weeks after its last game (best-effort).
        season_type = _d(data.get("requestedSeason")).get("type") or _d(data.get("season")).get("type")
        since = _days_since_last_game(data)
        if str(season_type) != "2" or since is None or since > 45:
            return data
        params = {"seasontype": "3"}
    try:
        extra = await _get_json(ctx.http, url, params, what=what, ttl_for=_board_ttl)
    except ToolError:
        return data
    return _merge_events(data, extra)


def _schedule_rows(data: dict, team_id: str, lg: League, tz, count: int = 5) -> dict:
    now = _now(tz).astimezone(timezone.utc)
    recent, upcoming, live = [], [], []
    for ev in _l(data.get("events")):
        if not isinstance(ev, dict):
            continue
        comp = _d((_l(ev.get("competitions")) or [{}])[0])
        comps = [c for c in _l(comp.get("competitors")) if isinstance(c, dict)]
        me = next((c for c in comps if str(c.get("id") or _d(c.get("team")).get("id")) == team_id), None)
        opp = next((c for c in comps if c is not me), None)
        if me is None or opp is None:
            continue
        label, detail, state = game_state(_d(comp.get("status")) or _d(ev.get("status")))
        dt = _parse_dt(comp.get("date") or ev.get("date"))
        time_valid = comp.get("timeValid", ev.get("timeValid", True)) is not False
        where = "vs" if me.get("homeAway") == "home" else "at"
        played = state in ("in", "post") and label not in ("postponed", "canceled")
        row: dict = {
            "date": _fmt_dt(dt, tz, with_time=not played, time_valid=time_valid),
            "opponent": f"{where} {_competitor_name(opp)}",
            "status": label,
        }
        if comp.get("neutralSite"):
            row["opponent"] = f"vs {_competitor_name(opp)} (neutral site)"
        if played:
            mine, theirs = _num(me.get("score")), _num(opp.get("score"))
            if label == "final":
                if me.get("winner"):
                    res = "W"
                elif opp.get("winner"):
                    res = "L"
                elif isinstance(mine, (int, float)) and isinstance(theirs, (int, float)) and mine != theirs:
                    res = "W" if mine > theirs else "L"
                else:
                    res = "D" if lg.soccer else "T"
                extra = ""
                if detail and detail.lower() not in ("final", "ft", "full time"):
                    tail = detail.split("/", 1)[-1] if "/" in detail else detail
                    extra = f" ({tail} innings)" if tail.isdigit() else f" ({tail})"
                row["result"] = f"{res} {mine}-{theirs}{extra}"
            else:
                row["result"] = f"{mine}-{theirs}, {detail}".strip(", ")
        elif label != "scheduled" and detail:
            row["detail"] = detail
        if label in ("scheduled", "delayed", "live"):
            tv = _tv(comp)
            if tv:
                row["tv"] = tv
        note = _note(comp)
        if note:
            row["note"] = note
        key = dt.timestamp() if dt else 0
        if label == "live" or (label in ("delayed", "suspended") and state == "in"):
            live.append(row)
        elif label == "final" or (dt and dt < now - timedelta(hours=6)):
            recent.append((key, row))
        else:
            upcoming.append((key, row))
    recent.sort(key=lambda kr: kr[0], reverse=True)
    upcoming.sort(key=lambda kr: kr[0])
    return {"live": live, "recent": [r for _, r in recent[:count]], "upcoming": [r for _, r in upcoming[:count]]}


@tool(
    "get_team_schedule",
    "One team's recent results and upcoming games from ESPN's public feed (unofficial): date and start time in the "
    "user's local time zone, opponent, home/away, result (W/L/D with score), TV, plus the team's record and "
    "standing, and any game in progress. Use it for 'when do the Yankees play next', 'how did Arsenal do last "
    "weekend', 'Lakers schedule', 'is there a Bills game Sunday', 'what time is the Celtics game'. Team sports only: "
    "nfl, college-football, nba, wnba, mens-college-basketball, womens-college-basketball, mlb, nhl, mls, nwsl, epl, "
    "laliga, bundesliga, serie-a, ligue-1, liga-mx, ucl, europa (for F1, golf and UFC use get_sports_scores). The "
    "team can be a name, city, nickname or abbreviation ('Lakers', 'LAL', 'Man City', 'Niners', 'NY Giants'). A "
    "name several teams share ('New York' in the NFL, 'Tigers' in college) returns a 'Which one?' error listing "
    "them: ask the user, or retry with the full team name.",
    {
        "league": {"type": "string", "description": "League, e.g. 'nba', 'nfl', 'epl'."},
        "team": {"type": "string", "description": "Team name, city, nickname or abbreviation."},
        "count": {"type": "integer", "description": "How many recent and upcoming games to list, 1-10. Default 5."},
    },
    ["league", "team"],
    label="Checking the schedule",
)
async def get_team_schedule(ctx, league: str, team: str, count: int = 5):
    lg = resolve_league(league)
    if lg.kind != "team":
        raise ToolError(f"{lg.name} has no team schedules; use get_sports_scores for the current or next event.")
    try:
        count = max(1, min(int(count or 5), 10))
    except (TypeError, ValueError):
        count = 5
    tz = _zone()
    t = await find_team(ctx.http, lg, team)
    data = await _schedule_data(ctx, lg, t)
    rows = _schedule_rows(data, str(t.get("id")), lg, tz, count)
    info = data.get("team") if isinstance(data.get("team"), dict) else {}
    name = info.get("displayName") or t.get("displayName") or _text(team)
    record = info.get("recordSummary")
    standing = info.get("standingSummary")
    season = (data.get("season") or {}).get("displayName") if isinstance(data.get("season"), dict) else None
    head = name + (f" ({', '.join(x for x in (record, standing) if x)})" if record or standing else "")
    bits = [head]
    if rows["live"]:
        bits.append(f"live: {rows['live'][0].get('result', '')} {rows['live'][0]['opponent']}".strip())
    if rows["recent"]:
        r = rows["recent"][0]
        bits.append(f"last: {r.get('result') or r['status']} {r['opponent']}")
    if rows["upcoming"]:
        u = rows["upcoming"][0]
        bits.append(f"next: {u.get('date')} {u['opponent']}")
    out: dict = {
        "status": " · ".join(bits),
        "team": name,
        "abbr": info.get("abbreviation") or t.get("abbreviation"),
        "record": record,
        "standing": standing,
        "season": season or info.get("seasonSummary"),
    }
    if rows["live"]:
        out["live"] = rows["live"]
    out["recent"] = rows["recent"]
    out["upcoming"] = rows["upcoming"]
    bye = data.get("byeWeek")
    if isinstance(bye, int) and bye > 0:
        out["bye_week"] = bye
    if not rows["upcoming"]:
        out["note"] = "No upcoming games on ESPN's schedule for this competition (offseason, or not yet scheduled)."
    out["timezone"] = _zone_label(_now(tz))
    out["source"] = SOURCE
    out = {k: v for k, v in out.items() if v is not None}
    return _fit(out, "recent", optional=_SCHEDULE_OPTIONAL, hint="ask for a smaller count")


# --------------------------------------------------------------------------- news
def _about(a: dict, team_id: str | None, words: list[str]) -> bool:
    """Is an ESPN article about this team (tagged with its id) or does it name the team/athlete?"""
    cats = [c for c in _l(a.get("categories")) if isinstance(c, dict)]
    if team_id and any(str(c.get("teamId") or _d(c.get("team")).get("id") or "") == team_id for c in cats):
        return True
    hay = _norm(" ".join([_text(a.get("headline")), _text(a.get("description"))]
                         + [_text(c.get("description")) for c in cats])).split()
    return any(_has_phrase(hay, w.split()) for w in words if w)


def _has_phrase(tokens: list[str], phrase: list[str]) -> bool:
    """Whole words in order, allowing a trailing 's' (possessives such as "Verstappen's" fold to 'verstappens'), so
    'kings' doesn't match 'power rankings' and 'rams' doesn't match 'programs'."""
    n = len(phrase)
    if not n:
        return False
    return any(
        all(tokens[i + j] in (w, w + "s") for j, w in enumerate(phrase)) for i in range(len(tokens) - n + 1)
    )


def _espn_articles(data: dict, tz, limit: int, team_id: str | None = None, words: list[str] | None = None) -> list[dict]:
    out = []
    for a in _l(data.get("articles")):
        if not isinstance(a, dict) or not _text(a.get("headline")):
            continue
        if (team_id or words) and not _about(a, team_id, words or []):
            continue
        pub = _parse_dt(a.get("published") or a.get("lastModified"))
        web = _d(_d(a.get("links")).get("web")).get("href")
        item = {
            "headline": clip(_text(a["headline"]), 200),
            "summary": clip(_text(a.get("description")), 240) or None,
            "published": _loc(pub, tz).isoformat(timespec="minutes") if pub else None,
            "age": relative_time(pub) if pub else None,
            "byline": _text(a.get("byline")) or None,
            "type": a.get("type"),
            "premium": True if a.get("premium") is True else None,
            "link": web if isinstance(web, str) and web.startswith("http") else None,
        }
        out.append({k: v for k, v in item.items() if v})
        if len(out) >= limit:
            break
    return out


@tool(
    "get_sports_news",
    "Latest sports headlines for one league - optionally one team or athlete - from ESPN's public news feed "
    "(unofficial), falling back to Google News: trades, injuries, signings, previews, recaps, rumors. Use it for "
    "sports news questions ('any Lakers injury news', 'latest NFL trade news', 'what's new in F1', 'Arsenal "
    "transfer news'); use get_news for non-sports news and get_sports_scores for scores. Leagues as in "
    "get_sports_scores (nfl, nba, mlb, nhl, epl, f1, ufc, pga ...).",
    {
        "league": {"type": "string", "description": "League, e.g. 'nfl', 'nba', 'epl', 'f1', 'ufc'."},
        "count": {"type": "integer", "description": "Number of stories, 1-20. Default 8."},
        "team": {"type": "string", "description": "Optional team or athlete to focus on, e.g. 'Lakers', 'Verstappen'."},
    },
    ["league"],
    label="Scanning sports news",
)
async def get_sports_news(ctx, league: str, count: int = 8, team: str | None = None):
    lg = resolve_league(league)
    try:
        limit = max(1, min(int(count or 8), 20))
    except (TypeError, ValueError):
        limit = 8
    tz = _zone()
    focus = _text(team) or None
    params: dict = {"limit": limit}
    label = lg.name
    team_id, words = None, []
    if focus:
        words = [q for q in _queries(focus, lg)]
        params["limit"] = 50  # filter a longer list down to the stories that are about them
        if lg.kind == "team":
            try:
                t = await find_team(ctx.http, lg, focus)
            except ToolError as exc:
                if "several" in str(exc):
                    raise
                t = None  # not a team we know (a player?): filter the league feed by name instead
            if t:
                team_id = str(t.get("id"))
                params["team"] = team_id  # ESPN's team filter; results are still checked below (best-effort)
                label = t.get("displayName") or focus
                words = [_norm(t.get("displayName")), _norm(t.get("shortDisplayName") or t.get("name"))]
    articles: list[dict] = []
    error = None
    try:
        data = await _get_json(ctx.http, f"{SITE}/{lg.path}/news", params, what=f"{lg.name} news", ttl=NEWS_TTL)
        articles = _espn_articles(data, tz, limit, team_id, words)
    except ToolError as exc:
        error = exc
    source = SOURCE
    if not articles:
        # Fallback: Google News search through the news tool's feed reader.
        from . import news as news_mod

        query = f"{label if label != lg.name else (focus or '')} {lg.name}".strip()
        try:
            items = await news_mod.fetch_headlines(ctx, query=query, limit=limit)
        except Exception:
            items = []
        for it in items:
            pub = it.get("published")
            articles.append({k: v for k, v in {
                "headline": it.get("title"),
                "summary": it.get("summary") or None,
                "published": _loc(pub, tz).isoformat(timespec="minutes") if pub else None,
                "age": relative_time(pub) if pub else None,
                "byline": it.get("source") or None,
                "link": it.get("link"),
            }.items() if v})
        source = "Google News" + (" (ESPN unavailable)" if error else "")
        if not articles:
            if error:
                raise error
            raise ToolError(f"No recent {label} news found on ESPN or Google News.")
    out = {
        "status": f"{len(articles)} {label} headlines",
        "league": lg.name,
        "team": label if label != lg.name else None,
        "articles": articles,
        "source": source,
    }
    return _fit({k: v for k, v in out.items() if v is not None}, "articles", optional=_NEWS_OPTIONAL,
                hint="ask for fewer stories")


# --------------------------------------------------------------------------- dashboard panel
async def scores_for_panel(ctx, leagues: list[str] | None = None, per_league: int = 8) -> list[dict]:
    """Compact live scoreboard for a dashboard panel: [{league, key, status, games: [{matchup, status, text}]}],
    only for leagues with something on ESPN's current slate. Leagues default to SPORTS_PANEL_LEAGUES."""
    keys = leagues or [k.strip() for k in _setting("SPORTS_PANEL_LEAGUES", DEFAULT_PANEL_LEAGUES).split(",") if k.strip()]

    async def one(key: str):
        res = await get_sports_scores(ctx, key)
        lg = resolve_league(key)
        rows = []
        for it in (res.get("games") or res.get("events") or [])[:per_league]:
            if "matchup" in it:
                text = it.get("summary") or it.get("detail") or it.get("start") or ""
                rows.append({"matchup": it["matchup"], "status": it.get("status"), "text": text})
            else:
                text = it.get("winner") or it.get("leader") or it.get("start") or it.get("dates") or ""
                rows.append({"matchup": it.get("event"), "status": it.get("status"), "text": text})
        return {"league": lg.name, "key": lg.key, "status": res.get("status"), "games": rows}

    results = await asyncio.gather(*(one(k) for k in keys[:8]), return_exceptions=True)
    return [r for r in results if isinstance(r, dict) and r["games"]]
