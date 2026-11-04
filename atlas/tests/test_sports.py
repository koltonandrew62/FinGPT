"""Sports tools (ESPN scoreboard / standings / teams / schedule / news), fully offline via httpx.MockTransport.

The fixtures mirror the JSON shapes ESPN's site API returns (site.api.espn.com/apis/site/v2/... and /apis/v2/...).
"""
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.tools import TOOLS, ToolError, call_tool, sports
from app.tools.sports import HostLimiter, game_state, match_score, parse_date, resolve_league, resolve_team
from conftest import Router, json_response, make_ctx, mock_http, no_network, run

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=ET)  # Wednesday afternoon, US Eastern (EST, UTC-5)
SITE = "site.api.espn.com/apis/site/v2/sports"
V2 = "site.api.espn.com/apis/v2/sports"
SPORTS_TOOLS = ["get_sports_scores", "get_sports_standings", "get_team_schedule", "get_sports_news"]


@pytest.fixture(autouse=True)
def sports_env(monkeypatch):
    """Fixed clock and zone, no waiting on the per-host limiter, empty cache."""
    monkeypatch.setenv("SPORTS_TIMEZONE", "America/New_York")
    monkeypatch.delenv("SPORTS_PANEL_LEAGUES", raising=False)
    monkeypatch.setattr(sports, "_now", lambda tz: NOW.astimezone(tz) if tz else NOW)
    monkeypatch.setattr(sports, "_LIMITER", HostLimiter(interval=0))
    sports._CACHE.clear()
    yield
    sports._CACHE.clear()


def call(name, handler=no_network, **args):
    async def go():
        async with mock_http(handler) as http:
            return await call_tool(name, args, make_ctx(http=http))

    return json.loads(run(go()))


def ctx_call(coro_fn, handler):
    async def go():
        async with mock_http(handler) as http:
            return await coro_fn(make_ctx(http=http))

    return run(go())


# --------------------------------------------------------------------------- fixture builders (ESPN shapes)
def team(tid, abbr, location, name, display=None, short=None, slug=None, nickname=None):
    """A team as ESPN's /teams list nests it. Note ESPN's "nickname" repeats the location ("New York" for both the
    Giants and the Jets), it is not the mascot."""
    display = display or f"{location} {name}"
    return {
        "id": str(tid),
        "uid": f"s:40~l:46~t:{tid}",
        "slug": slug or display.lower().replace(" ", "-"),
        "location": location,
        "name": name,
        "nickname": nickname if nickname is not None else location,
        "abbreviation": abbr,
        "displayName": display,
        "shortDisplayName": short or name,
        "color": "000000",
        "alternateColor": "ffffff",
        "isActive": True,
        "logos": [{"href": f"https://a.espncdn.com/i/teamlogos/nba/500/{abbr.lower()}.png", "width": 500, "height": 500}],
    }


def competitor(t, home_away, score=None, winner=None, record=None, order=None, **extra):
    c = {
        "id": t["id"],
        "uid": t["uid"],
        "type": "team",
        "order": order if order is not None else (0 if home_away == "home" else 1),
        "homeAway": home_away,
        "team": t,
        "score": "0" if score is None else str(score),
        "statistics": [],
    }
    if winner is not None:
        c["winner"] = winner
    if record:
        c["records"] = [
            {"name": "overall", "abbreviation": "Any", "type": "total", "summary": record},
            {"name": "Home", "type": "home", "summary": "2-1"},
        ]
    c.update(extra)
    return c


def status(name, state, completed=False, detail="", period=0, clock="0:00", description=None):
    return {
        "clock": 0.0,
        "displayClock": clock,
        "period": period,
        "type": {
            "id": "1",
            "name": name,
            "state": state,
            "completed": completed,
            "description": description or detail,
            "detail": detail,
            "shortDetail": detail,
        },
    }


def event(eid, when, home, away, st, name=None, **comp_extra):
    comp = {
        "id": str(eid),
        "uid": f"s:40~l:46~e:{eid}~c:{eid}",
        "date": when,
        "attendance": 0,
        "type": {"id": "1", "abbreviation": "STD"},
        "timeValid": True,
        "neutralSite": False,
        "conferenceCompetition": False,
        "playByPlayAvailable": True,
        "recent": False,
        "venue": {"id": "1", "fullName": "Test Arena", "address": {"city": "Testville", "state": "NY"}, "indoor": True},
        "competitors": [home, away],
        "notes": [],
        "status": st,
        "broadcasts": [],
        "format": {"regulation": {"periods": 4}},
        "startDate": when,
        "geoBroadcasts": [],
    }
    comp.update(comp_extra)
    return {
        "id": str(eid),
        "uid": f"s:40~l:46~e:{eid}",
        "date": when,
        "name": name or f"{away['team']['displayName']} at {home['team']['displayName']}",
        "shortName": f"{away['team']['abbreviation']} @ {home['team']['abbreviation']}",
        "season": {"year": 2027, "type": 2, "slug": "regular-season"},
        "competitions": [comp],
        "links": [{"href": f"https://www.espn.com/nba/game/_/gameId/{eid}", "text": "Gamecast", "rel": ["summary", "desktop", "event"]}],
        "status": st,
    }


BOS = team(2, "BOS", "Boston", "Celtics")
NYK = team(18, "NY", "New York", "Knicks")
LAL = team(13, "LAL", "Los Angeles", "Lakers")
LAC = team(12, "LAC", "LA", "Clippers")
DEN = team(7, "DEN", "Denver", "Nuggets")
MIA = team(14, "MIA", "Miami", "Heat")
CHI = team(4, "CHI", "Chicago", "Bulls")
GS = team(9, "GS", "Golden State", "Warriors")
PHX = team(21, "PHX", "Phoenix", "Suns")
PHI = team(20, "PHI", "Philadelphia", "76ers")
MIN = team(16, "MIN", "Minnesota", "Timberwolves")
NBA_TEAMS = [BOS, NYK, LAL, LAC, DEN, MIA, CHI, GS, PHX, PHI, MIN]

# A literal in-progress game, exactly as ESPN's scoreboard nests it.
LIVE_NBA = {
    "id": "401704910",
    "uid": "s:40~l:46~e:401704910",
    "date": "2026-11-05T00:30Z",
    "name": "Boston Celtics at New York Knicks",
    "shortName": "BOS @ NY",
    "season": {"year": 2027, "type": 2, "slug": "regular-season"},
    "competitions": [
        {
            "id": "401704910",
            "date": "2026-11-05T00:30Z",
            "timeValid": True,
            "neutralSite": False,
            "venue": {"id": "1847", "fullName": "Madison Square Garden", "address": {"city": "New York", "state": "NY"}, "indoor": True},
            "competitors": [
                {
                    "id": "18", "type": "team", "order": 0, "homeAway": "home", "team": NYK, "score": "74",
                    "linescores": [{"value": 25.0}, {"value": 27.0}, {"value": 22.0}],
                    "records": [{"name": "overall", "abbreviation": "Any", "type": "total", "summary": "4-3"}],
                },
                {
                    "id": "2", "type": "team", "order": 1, "homeAway": "away", "team": BOS, "score": "78",
                    "linescores": [{"value": 30.0}, {"value": 24.0}, {"value": 24.0}],
                    "records": [{"name": "overall", "abbreviation": "Any", "type": "total", "summary": "5-2"}],
                },
            ],
            "notes": [],
            "status": {
                "clock": 272.0, "displayClock": "4:32", "period": 3,
                "type": {"id": "2", "name": "STATUS_IN_PROGRESS", "state": "in", "completed": False,
                         "description": "In Progress", "detail": "4:32 - 3rd Quarter", "shortDetail": "4:32 - 3rd"},
            },
            "broadcasts": [{"market": "home", "names": ["MSG"]}, {"market": "national", "names": ["ESPN"]}],
            "odds": [{"provider": {"id": "58", "name": "ESPN BET", "priority": 1}, "details": "BOS -3.5", "overUnder": 221.5, "spread": -3.5}],
        }
    ],
    "status": {"clock": 272.0, "displayClock": "4:32", "period": 3,
               "type": {"id": "2", "name": "STATUS_IN_PROGRESS", "state": "in", "completed": False,
                        "description": "In Progress", "detail": "4:32 - 3rd Quarter", "shortDetail": "4:32 - 3rd"}},
}


def nba_board():
    final = event(
        401704911, "2026-11-04T00:00Z",
        competitor(LAL, "home", 110, True, "4-3"), competitor(DEN, "away", 102, False, "3-4"),
        status("STATUS_FINAL", "post", True, "Final", 4),
        headlines=[{"type": "Recap", "description": "LeBron James scored 31 as the Lakers held off Denver.",
                    "shortLinkText": "LeBron's 31 lifts Lakers past Nuggets"}],
    )
    ot = event(
        401704912, "2026-11-04T01:00Z",
        competitor(CHI, "home", 119, False, "2-5"), competitor(MIA, "away", 121, True, "5-2"),
        status("STATUS_FINAL", "post", True, "Final/OT", 5),
    )
    upcoming = event(
        401704913, "2026-11-05T03:00Z",
        competitor(PHX, "home", None, None, "4-2"), competitor(GS, "away", None, None, "5-1"),
        status("STATUS_SCHEDULED", "pre", False, "Wed, November 4th at 10:00 PM EST"),
        broadcasts=[{"market": "national", "names": ["TNT"]}],
        odds=[{"provider": {"name": "ESPN BET"}, "details": "GS -2.0", "overUnder": 229.5}],
    )
    postponed = event(
        401704914, "2026-11-05T00:00Z",
        competitor(PHI, "home"), competitor(MIN, "away"),
        status("STATUS_POSTPONED", "post", False, "Postponed", description="Postponed"),
    )
    return {
        "leagues": [{
            "id": "46", "uid": "s:40~l:46", "name": "National Basketball Association", "abbreviation": "NBA",
            "slug": "nba", "season": {"year": 2027, "displayName": "2026-27", "type": {"id": "2", "type": 2, "name": "Regular Season"}},
            "calendarType": "day", "calendarIsWhitelist": True,
            "calendar": ["2026-10-21T07:00Z", "2026-11-04T08:00Z", "2026-11-05T08:00Z", "2026-11-07T08:00Z"],
        }],
        "season": {"type": 2, "year": 2027},
        "day": {"date": "2026-11-04"},
        "events": [final, ot, upcoming, postponed, LIVE_NBA],
    }


def teams_payload(teams, league_slug="nba", sport="Basketball"):
    return {
        "sports": [{
            "id": "40", "uid": "s:40", "name": sport, "slug": sport.lower(),
            "leagues": [{
                "id": "46", "uid": "s:40~l:46", "name": "League", "abbreviation": league_slug.upper(),
                "shortName": league_slug.upper(), "slug": league_slug,
                "teams": [{"team": t} for t in teams],
                "year": 2027, "season": {"year": 2027, "displayName": "2026-27"},
            }],
        }]
    }


# --------------------------------------------------------------------------- registry & basics
def test_tools_registered_with_clear_descriptions():
    for name in SPORTS_TOOLS:
        assert name in TOOLS
        t = TOOLS[name]
        assert t.gate is None and t.preview is None  # read-only public data: no approval card
        assert "ESPN" in t.description and len(t.description) > 200
        assert "league" in t.parameters["required"]
    desc = TOOLS["get_sports_scores"].description
    for league in ("nfl", "college-football", "nba", "wnba", "mens-college-basketball", "mlb", "nhl", "mls", "epl",
                   "laliga", "ucl", "f1", "ufc", "pga"):
        assert league in desc
    assert "local time" in desc
    assert TOOLS["get_team_schedule"].parameters["required"] == ["league", "team"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("NBA", ("basketball", "nba")),
        ("college football", ("football", "college-football")),
        ("NCAAF", ("football", "college-football")),
        ("college basketball", ("basketball", "mens-college-basketball")),
        ("Men's College Basketball", ("basketball", "mens-college-basketball")),
        ("wnba", ("basketball", "wnba")),
        ("MLB", ("baseball", "mlb")),
        ("hockey", ("hockey", "nhl")),
        ("MLS", ("soccer", "usa.1")),
        ("Premier League", ("soccer", "eng.1")),
        ("EPL", ("soccer", "eng.1")),
        ("the premier league table", ("soccer", "eng.1")),
        ("La Liga", ("soccer", "esp.1")),
        ("champions league", ("soccer", "uefa.champions")),
        ("eng.1", ("soccer", "eng.1")),
        ("Formula 1", ("racing", "f1")),
        ("F1", ("racing", "f1")),
        ("UFC", ("mma", "ufc")),
        ("PGA Tour", ("golf", "pga")),
        ("serie-a", ("soccer", "ita.1")),
        ("soccer/ned.1", ("soccer", "ned.1")),
        ("football/nfl", ("football", "nfl")),
    ],
)
def test_resolve_league_aliases(value, expected):
    lg = resolve_league(value)
    assert (lg.sport, lg.slug) == expected


def test_resolve_league_errors_are_friendly():
    with pytest.raises(ToolError, match="Supported"):
        resolve_league("quidditch")
    with pytest.raises(ToolError, match="Which soccer league"):
        resolve_league("soccer")
    with pytest.raises(ToolError, match="Say which league"):
        resolve_league("")
    assert resolve_league("f1").kind == "racing"
    assert resolve_league("golf").kind == "golf"
    assert resolve_league("mma").kind == "mma"


# --------------------------------------------------------------------------- dates
TODAY = date(2026, 11, 4)  # a Wednesday


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("current", None),
        ("this week", None),
        ("today", TODAY),
        ("Tonight", TODAY),
        ("yesterday", date(2026, 11, 3)),
        ("last night", date(2026, 11, 3)),
        ("tomorrow", date(2026, 11, 5)),
        ("2026-11-08", date(2026, 11, 8)),
        ("2026-11-08T19:00:00Z", date(2026, 11, 8)),
        ("20261108", date(2026, 11, 8)),
        ("saturday", date(2026, 11, 7)),
        ("on Sunday", date(2026, 11, 8)),
        ("wednesday", TODAY),
        ("next wednesday", date(2026, 11, 11)),
        ("last sunday", date(2026, 11, 1)),
        ("last wednesday", date(2026, 10, 28)),
        ("sunday night", date(2026, 11, 8)),
        ("Nov 8", date(2026, 11, 8)),
        ("November 8th, 2026", date(2026, 11, 8)),
        ("8 November", date(2026, 11, 8)),
        ("Jan 3", date(2027, 1, 3)),  # nearest Jan 3 to early November is the coming one
        ("3 days ago", date(2026, 11, 1)),
        ("in 2 days", date(2026, 11, 6)),
    ],
)
def test_parse_date(value, expected):
    assert parse_date(value, TODAY) == expected


@pytest.mark.parametrize("value", ["2026-02-30", "someday", "13/45/2026", "1850-01-01", "Febtember 3"])
def test_parse_date_rejects_garbage(value):
    with pytest.raises(ToolError, match="couldn't read the date"):
        parse_date(value, TODAY)


def test_times_are_local_and_locale_independent(monkeypatch):
    dt = sports._parse_dt("2026-11-05T00:30Z")
    assert sports._fmt_dt(dt, ET) == "Wed Nov 4, 7:30 PM EST"
    assert sports._fmt_dt(dt, ZoneInfo("Europe/London")) == "Thu Nov 5, 12:30 AM GMT"
    assert sports._fmt_dt(dt, ET, time_valid=False) == "Wed Nov 4, time TBD"
    assert sports._fmt_dt(sports._parse_dt("2026-11-05T00:30:00+00:00"), ZoneInfo("America/Los_Angeles")) == "Wed Nov 4, 4:30 PM PST"
    assert sports._parse_dt("not a date") is None and sports._parse_dt(None) is None
    # An unknown override zone falls back to the computer's zone instead of failing.
    monkeypatch.setenv("SPORTS_TIMEZONE", "Mars/Olympus_Mons")
    assert sports._zone() is None


# --------------------------------------------------------------------------- status parsing
def test_game_state_labels():
    assert game_state(status("STATUS_SCHEDULED", "pre"))[0] == "scheduled"
    assert game_state(status("STATUS_IN_PROGRESS", "in", detail="4:32 - 3rd Quarter"))[:2] == ("live", "4:32 - 3rd Quarter")
    assert game_state(status("STATUS_HALFTIME", "in", detail="Halftime"))[:2] == ("live", "Halftime")
    assert game_state(status("STATUS_FINAL", "post", True, "Final/OT"))[:2] == ("final", "Final/OT")
    assert game_state(status("STATUS_FULL_TIME", "post", True, "FT"))[0] == "final"
    assert game_state(status("STATUS_POSTPONED", "post", False, "Postponed"))[0] == "postponed"
    assert game_state(status("STATUS_CANCELED", "post", False, "Canceled"))[0] == "canceled"
    assert game_state(status("STATUS_RAIN_DELAY", "in", False, "Rain Delay"))[0] == "delayed"
    assert game_state({})[0] == "scheduled"


# --------------------------------------------------------------------------- scoreboard: team sports
def test_nba_scoreboard_live_final_scheduled_postponed():
    router = Router({f"{SITE}/basketball/nba/scoreboard": json_response(nba_board())})
    out = call("get_sports_scores", router, league="NBA", date="today")
    assert router.requests[0].url.params["dates"] == "20261104"
    assert router.requests[0].headers["user-agent"].startswith("Mozilla/5.0")
    assert out["status"] == "NBA on Wed Nov 4: 5 games · 1 live · 1 scheduled · 2 final · 1 postponed"
    assert out["league"] == "NBA" and out["date"] == "2026-11-04" and out["source"] == "ESPN"
    assert out["timezone"].startswith("EST (UTC-05:00)")
    games = out["games"]
    assert [g["status"] for g in games] == ["live", "scheduled", "final", "final", "postponed"]  # live first

    live = games[0]
    assert live["matchup"] == "BOS @ NY"
    assert live["summary"] == "Celtics 78, Knicks 74 · 4:32 - 3rd Quarter"
    assert live["away"] == {"team": "Boston Celtics", "abbr": "BOS", "score": 78, "record": "5-2"}
    assert live["home"]["score"] == 74
    assert live["tv"] == "ESPN, MSG"  # national broadcast first
    assert live["venue"] == "Madison Square Garden, New York, NY"
    assert "start" not in live and "odds" not in live

    sched = games[1]
    assert sched["start"] == "Wed Nov 4, 10:00 PM EST"  # 03:00Z on the 5th, shown in local time
    assert sched["odds"] == "GS -2.0, O/U 229.5 (ESPN BET)"
    assert sched["tv"] == "TNT"
    assert "score" not in sched["home"] and "summary" not in sched  # ESPN sends "0" before tip-off

    lakers = next(g for g in games if g["matchup"] == "DEN @ LAL")
    assert lakers["summary"] == "Lakers 110, Nuggets 102 · Final"
    assert lakers["home"]["winner"] is True
    assert lakers["headline"] == "LeBron's 31 lifts Lakers past Nuggets"
    assert lakers["start"] == "Tue Nov 3, 7:00 PM EST"

    heat = next(g for g in games if g["matchup"] == "MIA @ CHI")
    assert heat["summary"] == "Heat 121, Bulls 119 · Final/OT"  # winner first even as the away team

    pp = games[-1]
    assert pp["status"] == "postponed" and pp["detail"] == "Postponed"
    assert "summary" not in pp and "score" not in pp["home"]
    assert all(not k.startswith("_") for g in games for k in g)


def test_team_filter_and_no_game_hint():
    calls = []

    def board(request):
        calls.append(request.url.path)
        return json_response(nba_board())

    sched = {
        "team": {"id": "12", "abbreviation": "LAC", "displayName": "LA Clippers", "recordSummary": "3-3"},
        "events": [
            event(1, "2026-11-02T03:30Z", competitor(LAC, "home", 99, False), competitor(DEN, "away", 105, True),
                  status("STATUS_FINAL", "post", True, "Final", 4)),
            event(2, "2026-11-07T03:30Z", competitor(PHX, "home"), competitor(LAC, "away"),
                  status("STATUS_SCHEDULED", "pre", False, "Fri, November 6th at 10:30 PM EST")),
        ],
    }
    router = Router({
        f"{SITE}/basketball/nba/scoreboard": board,
        f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS)),
        f"{SITE}/basketball/nba/teams/12/schedule": json_response(sched),
    })
    out = call("get_sports_scores", router, league="nba", date="today", team="celtics")
    assert [g["matchup"] for g in out["games"]] == ["BOS @ NY"]
    assert out["status"].startswith("NBA on Wed Nov 4: 1 game · 1 live")

    out = call("get_sports_scores", router, league="nba", date="today", team="LAL")
    assert [g["matchup"] for g in out["games"]] == ["DEN @ LAL"]

    # The Clippers aren't on today's board: say so, and add their last and next game.
    out = call("get_sports_scores", router, league="nba", date="today", team="Clippers")
    assert out["games"] == []
    assert out["status"] == "No NBA game for 'Clippers' on Wed Nov 4"
    assert out["last_game"]["result"] == "L 99-105"
    assert out["next_game"]["opponent"] == "at Phoenix Suns"
    assert out["next_game"]["date"] == "Fri Nov 6, 10:30 PM EST"
    assert len(calls) == 1  # the scoreboard itself came from the cache after the first call


def test_empty_day_lists_next_game_dates():
    board = nba_board()
    board["events"] = []
    router = Router({f"{SITE}/basketball/nba/scoreboard": json_response(board)})
    out = call("get_sports_scores", router, league="nba", date="2026-11-06")
    assert out["games"] == []
    assert out["status"] == "No NBA games on Fri Nov 6"
    assert out["next_dates_with_games"] == ["Sat Nov 7"]


def test_default_is_current_slate_and_nfl_week_situation_tie():
    kc = team(12, "KC", "Kansas City", "Chiefs")
    buf = team(2, "BUF", "Buffalo", "Bills")
    nyg = team(19, "NYG", "New York", "Giants")
    dal = team(6, "DAL", "Dallas", "Cowboys")
    live = event(
        401772001, "2026-11-04T18:00Z",
        competitor(buf, "home", 17, None, "7-1"), competitor(kc, "away", 21, None, "6-2"),
        status("STATUS_IN_PROGRESS", "in", False, "2:11 - 4th Quarter", 4, "2:11"),
        situation={
            "lastPlay": {"id": "1", "type": {"id": "24", "text": "Pass Reception"},
                         "text": "(Shotgun) J.Allen pass short right to K.Coleman to KC 18 for 12 yards."},
            "down": 2, "yardLine": 18, "distance": 7, "downDistanceText": "2nd & 7 at KC 18",
            "shortDownDistanceText": "2nd & 7", "possessionText": "KC 18", "isRedZone": True,
            "homeTimeouts": 2, "awayTimeouts": 1, "possession": "2",
        },
    )
    tie = event(
        401772002, "2026-11-01T18:00Z",
        competitor(dal, "home", 20, False, "4-3-1"), competitor(nyg, "away", 20, False, "2-5-1"),
        status("STATUS_FINAL", "post", True, "Final/OT", 5),
    )
    tbd = event(
        401772003, "2026-11-08T18:00Z",
        competitor(nyg, "home"), competitor(kc, "away"),
        status("STATUS_SCHEDULED", "pre", False, "TBD"), timeValid=False,
    )
    payload = {"leagues": [{"abbreviation": "NFL", "calendar": [{"label": "Regular Season", "entries": []}]}],
               "season": {"type": 2, "year": 2026}, "week": {"number": 9}, "events": [live, tie, tbd]}

    def handler(request):
        assert "dates" not in request.url.params  # no date: ESPN's current week
        return json_response(payload)

    out = call("get_sports_scores", Router({f"{SITE}/football/nfl/scoreboard": handler}), league="nfl")
    assert out["date"] == "current" and out["week"] == 9
    assert out["status"] == "NFL: 3 games · 1 live · 1 scheduled · 1 final"
    g = out["games"][0]
    assert g["summary"] == "Chiefs 21, Bills 17 · 2:11 - 4th Quarter"
    assert g["situation"] == "BUF ball, 2nd & 7 at KC 18, red zone"
    assert g["last_play"].startswith("(Shotgun) J.Allen pass")
    assert out["games"][1]["start"] == "Sun Nov 8, time TBD"
    assert out["games"][2]["summary"] == "Giants 20, Cowboys 20 · Final/OT (tie)"


def test_mlb_situation_runners():
    nyy = team(10, "NYY", "New York", "Yankees")
    lad = team(19, "LAD", "Los Angeles", "Dodgers")
    g = event(
        401800001, "2026-10-30T00:08Z",
        competitor(lad, "home", 3, None, "93-69"), competitor(nyy, "away", 3, None, "94-68"),
        status("STATUS_IN_PROGRESS", "in", False, "Top 7th", 7),
        situation={"balls": 1, "strikes": 2, "outs": 1, "onFirst": True, "onSecond": False, "onThird": True,
                   "batter": {"athlete": {"shortName": "A. Judge"}}},
    )
    router = Router({f"{SITE}/baseball/mlb/scoreboard": json_response({"events": [g]})})
    out = call("get_sports_scores", router, league="mlb", date="2026-10-29")
    game = out["games"][0]
    assert game["summary"] == "Yankees 3, Dodgers 3 · Top 7th"
    assert game["situation"] == "1 out, 1-2 count, runners on 1st, 3rd"


# --------------------------------------------------------------------------- scoreboard: soccer
ARS = team(359, "ARS", "Arsenal", "Arsenal", display="Arsenal", short="Arsenal")
CHE = team(363, "CHE", "Chelsea", "Chelsea", display="Chelsea", short="Chelsea")
MCI = team(382, "MNC", "Manchester City", "Manchester City", display="Manchester City", short="Man City")
MUN = team(360, "MAN", "Manchester United", "Manchester United", display="Manchester United", short="Man United")
TOT = team(367, "TOT", "Tottenham Hotspur", "Tottenham Hotspur", display="Tottenham Hotspur", short="Tottenham")
EPL_TEAMS = [ARS, CHE, MCI, MUN, TOT]


def soccer_comp(t, home_away, score, winner=None, record="5-3-2", **extra):
    c = competitor(t, home_away, score, winner, None, **extra)
    c["records"] = [{"name": "All Splits", "abbreviation": "Any", "type": "total", "summary": record}]
    c["form"] = "WDWLW"
    return c


def epl_board():
    draw = event(
        704501, "2026-11-04T15:00Z",
        soccer_comp(ARS, "home", 1, False, "6-3-1"), soccer_comp(CHE, "away", 1, False, "5-3-2"),
        status("STATUS_FULL_TIME", "post", True, "FT", 2, "90'+5'"),
        details=[
            {"type": {"id": "70", "text": "Goal"}, "clock": {"value": 1380.0, "displayValue": "23'"},
             "team": {"id": "359"}, "scoreValue": 1, "scoringPlay": True, "redCard": False, "yellowCard": False,
             "penaltyKick": False, "ownGoal": False, "shootout": False,
             "athletesInvolved": [{"id": "1", "displayName": "Bukayo Saka", "shortName": "B. Saka", "team": {"id": "359"}}]},
            {"type": {"id": "94", "text": "Yellow Card"}, "clock": {"value": 2400.0, "displayValue": "40'"},
             "team": {"id": "363"}, "scoringPlay": False, "redCard": False, "yellowCard": True,
             "athletesInvolved": [{"displayName": "Moises Caicedo", "shortName": "M. Caicedo"}]},
            {"type": {"id": "137", "text": "Penalty - Scored"}, "clock": {"value": 4020.0, "displayValue": "67'"},
             "team": {"id": "363"}, "scoreValue": 1, "scoringPlay": True, "penaltyKick": True, "ownGoal": False,
             "athletesInvolved": [{"displayName": "Cole Palmer", "shortName": "C. Palmer"}]},
            {"type": {"id": "93", "text": "Red Card"}, "clock": {"value": 5100.0, "displayValue": "85'"},
             "team": {"id": "359"}, "scoringPlay": False, "redCard": True,
             "athletesInvolved": [{"displayName": "William Saliba", "shortName": "W. Saliba"}]},
        ],
    )
    live = event(
        704502, "2026-11-04T19:30Z",
        soccer_comp(MCI, "home", 2), soccer_comp(MUN, "away", 0),
        status("STATUS_SECOND_HALF", "in", False, "67'", 2, "67'", description="Second Half"),
    )
    later = event(
        704503, "2026-11-05T20:00Z",
        soccer_comp(TOT, "home", 0), soccer_comp(ARS, "away", 0),
        status("STATUS_SCHEDULED", "pre", False, "Thu, November 5th at 3:00 PM EST"),
        broadcasts=[{"market": "national", "names": ["USA Net", "Peacock"]}],
    )
    return {"leagues": [{"abbreviation": "ENG.1", "name": "English Premier League"}], "events": [draw, live, later]}


def test_soccer_draw_live_minute_and_goals():
    router = Router({f"{SITE}/soccer/eng.1/scoreboard": json_response(epl_board())})
    out = call("get_sports_scores", router, league="premier league")
    games = out["games"]
    live, later, draw = games
    assert live["matchup"] == "MNC vs MAN"  # soccer: home team first
    assert live["summary"] == "Man City 2, Man United 0 · 67'"
    assert draw["summary"] == "Arsenal 1, Chelsea 1 · FT (draw)"
    assert draw["home"]["record"] == "6-3-1"
    assert draw["goals"] == ["23' B. Saka (ARS)", "67' C. Palmer (pen) (CHE)"]
    assert draw["red_cards"] == ["85' W. Saliba (ARS)"]
    assert later["start"] == "Thu Nov 5, 3:00 PM EST" and later["tv"] == "USA Net, Peacock"

    # Nicknames resolve without a team-list lookup: the board itself is matched.
    out = call("get_sports_scores", router, league="epl", team="Man Utd")
    assert [g["matchup"] for g in out["games"]] == ["MNC vs MAN"]
    out = call("get_sports_scores", router, league="epl", team="spurs")
    assert [g["matchup"] for g in out["games"]] == ["TOT vs ARS"]
    out = call("get_sports_scores", router, league="epl", team="arsenal")
    assert len(out["games"]) == 2


def test_soccer_penalty_shootout():
    rma = team(86, "RMA", "Real Madrid", "Real Madrid", display="Real Madrid", short="Real Madrid")
    bay = team(132, "BAY", "Bayern Munich", "Bayern Munich", display="Bayern Munich", short="Bayern")
    ko = event(
        740001, "2026-04-15T19:00Z",
        soccer_comp(rma, "home", 2, False, shootoutScore=3), soccer_comp(bay, "away", 2, True, shootoutScore=4),
        status("STATUS_FINAL_PEN", "post", True, "FT-Pens", 5),
    )
    router = Router({f"{SITE}/soccer/uefa.champions/scoreboard": json_response({"events": [ko]})})
    out = call("get_sports_scores", router, league="ucl", date="2026-04-15")
    assert out["games"][0]["summary"] == "Bayern 2, Real Madrid 2 (4-3 on penalties) · FT-Pens"
    assert out["games"][0]["away"]["pens"] == 4


# --------------------------------------------------------------------------- scoreboard: individual sports
def driver(order, name, short, winner=False):
    return {"id": str(4000 + order), "uid": f"s:2000~a:{4000 + order}", "type": "athlete", "order": order,
            "winner": winner, "startOrder": order,
            "athlete": {"fullName": name, "displayName": name, "shortName": short,
                        "flag": {"href": "https://a.espncdn.com/i/teamlogos/countries/500/ned.png", "alt": "Netherlands"}},
            "statistics": []}


DRIVERS = [("Lando Norris", "L. Norris"), ("Max Verstappen", "M. Verstappen"), ("Charles Leclerc", "C. Leclerc"),
           ("Oscar Piastri", "O. Piastri"), ("George Russell", "G. Russell"), ("Lewis Hamilton", "L. Hamilton"),
           ("Carlos Sainz", "C. Sainz"), ("Fernando Alonso", "F. Alonso"), ("Pierre Gasly", "P. Gasly"),
           ("Alex Albon", "A. Albon"), ("Yuki Tsunoda", "Y. Tsunoda")]


def f1_board(race_done=False):
    def session(abbr, when, st, order):
        return {"id": f"{abbr}1", "uid": "x", "date": when, "startDate": when, "type": {"id": "3", "abbreviation": abbr},
                "status": st, "competitors": [driver(i + 1, *DRIVERS[j]) for i, j in enumerate(order)]}

    done = status("STATUS_FINAL", "post", True, "Final")
    race_status = done if race_done else status("STATUS_SCHEDULED", "pre", False, "Sun, November 8th at 12:00 PM EST")
    race_order = [1, 0, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    return {
        "leagues": [{"id": "2030", "name": "Formula 1", "abbreviation": "F1", "slug": "f1"}],
        "events": [{
            "id": "600052412", "uid": "s:2000~l:2030~e:600052412",
            "date": "2026-11-06T14:30Z", "endDate": "2026-11-08T19:00Z",
            "name": "Grande Prêmio de São Paulo", "shortName": "Sao Paulo GP",
            "circuit": {"id": "15", "fullName": "Autódromo José Carlos Pace", "address": {"city": "São Paulo", "country": "Brazil"}},
            "competitions": [
                session("FP1", "2026-11-06T14:30Z", done, list(range(11))),
                session("Qual", "2026-11-07T18:00Z", done, [1, 0, 3, 2, 4, 5, 6, 7, 8, 9, 10]),
                session("Race", "2026-11-08T17:00Z", race_status, race_order),
            ],
            "status": {"type": {"id": "1", "name": "STATUS_SCHEDULED", "state": "pre", "completed": False}},
        }],
    }


def test_f1_sessions_and_driver_focus():
    router = Router({f"{SITE}/racing/f1/scoreboard": json_response(f1_board())})
    out = call("get_sports_scores", router, league="formula one", team="Verstappen")
    ev = out["events"][0]
    assert out["status"].startswith("Formula 1: 1 event")
    assert ev["event"] == "Grande Prêmio de São Paulo"
    assert ev["status"] == "weekend underway"
    assert ev["circuit"] == "Autódromo José Carlos Pace, São Paulo, Brazil"
    assert ev["dates"] == "Fri Nov 6 - Sun Nov 8"
    fp1, qual, race = ev["sessions"]
    assert fp1["top"] == ["1. Lando Norris", "2. Max Verstappen", "3. Charles Leclerc"]
    assert qual["top"][0] == "1. Max Verstappen" and qual["focus"] == "P1 Max Verstappen"
    assert race["status"] == "scheduled" and race["start"] == "Sun Nov 8, 12:00 PM EST" and "top" not in race

    sports._CACHE.clear()
    router = Router({f"{SITE}/racing/f1/scoreboard": json_response(f1_board(race_done=True))})
    out = call("get_sports_scores", router, league="f1", date="sunday")
    ev = out["events"][0]
    assert ev["status"] == "final" and ev["winner"] == "Max Verstappen"
    assert len(ev["sessions"][2]["top"]) == 10  # the race shows a top ten
    assert router.requests[0].url.params["dates"] == "20261108"


def golfer(order, name, score, rounds):
    return {"id": str(9000 + order), "uid": f"s:1100~a:{9000 + order}", "type": "athlete", "order": order,
            "athlete": {"fullName": name, "displayName": name, "shortName": f"{name[0]}. {name.split()[-1]}",
                        "flag": {"alt": "United States"}},
            "score": score,
            "linescores": [{"value": float(r), "displayValue": f"{r - 71:+d}" if r != 71 else "E", "period": i + 1}
                           for i, r in enumerate(rounds)],
            "statistics": []}


def golf_board():
    players = [
        golfer(1, "Scottie Scheffler", "-15", [66, 67, 65]),
        golfer(2, "Xander Schauffele", "-13", [68, 66, 66]),
        golfer(3, "Rory McIlroy", "-13", [67, 67, 66]),
        golfer(4, "Ludvig Aberg", "-10", [69, 68, 66]),
        golfer(5, "Collin Morikawa", "E", [71, 70, 72]),
    ]
    return {
        "leagues": [{"id": "1106", "name": "PGA TOUR", "abbreviation": "PGA", "slug": "pga"}],
        "events": [{
            "id": "401703521", "uid": "s:1100~l:1106~e:401703521",
            "date": "2026-11-05T05:00Z", "endDate": "2026-11-08T05:00Z",
            "name": "World Wide Technology Championship", "shortName": "WWT Championship",
            "competitions": [{
                "id": "401703521", "date": "2026-11-05T05:00Z", "competitors": players,
                "status": {"period": 3, "type": {"id": "2", "name": "STATUS_IN_PROGRESS", "state": "in",
                                                 "completed": False, "description": "In Progress",
                                                 "detail": "Round 3 - In Progress"}},
            }],
            "status": {"type": {"id": "2", "name": "STATUS_IN_PROGRESS", "state": "in", "detail": "Round 3 - In Progress"}},
        }],
    }


def test_golf_leaderboard_ties_and_player_focus():
    router = Router({f"{SITE}/golf/pga/scoreboard": json_response(golf_board())})
    out = call("get_sports_scores", router, league="golf", team="McIlroy")
    ev = out["events"][0]
    assert ev["status"] == "live" and ev["detail"] == "Round 3 - In Progress"
    assert ev["dates"] == "Thu Nov 5 - Sun Nov 8"
    assert ev["leader"] == "Scottie Scheffler (-15)"
    board = ev["leaderboard"]
    assert [r["pos"] for r in board] == ["1", "T2", "T2", "4", "5"]
    assert board[0] == {"pos": "1", "player": "Scottie Scheffler", "score": "-15", "rounds": [66, 67, 65]}
    assert board[4]["score"] == "E"
    assert ev["players"][0]["player"] == "Rory McIlroy" and ev["players"][0]["pos"] == "T2"
    assert ev["field_size"] == 5


def fighter(order, name, record, winner=None):
    c = {"id": str(3000 + order), "type": "athlete", "order": order,
         "athlete": {"fullName": name, "displayName": name, "shortName": name.split()[-1], "flag": {"alt": "USA"}},
         "records": [{"name": "overall", "abbreviation": "TOT", "type": "total", "summary": record}]}
    if winner is not None:
        c["winner"] = winner
    return c


def test_ufc_card_results():
    final = status("STATUS_FINAL", "post", True, "Final", 2, "2:05")
    final["result"] = {"id": "3", "name": "sub", "displayName": "Submission", "shortDisplayName": "SUB"}
    card = {
        "events": [{
            "id": "600041234", "date": "2026-11-08T03:00Z", "name": "UFC Fight Night: Moreno vs. Albazi",
            "competitions": [
                {"id": "a", "date": "2026-11-08T00:00Z", "type": {"id": "8", "abbreviation": "Lightweight"},
                 "cardSegment": {"id": "2", "name": "prelims", "description": "Prelims"},
                 "venue": {"fullName": "Arena CDMX", "address": {"city": "Mexico City", "country": "Mexico"}},
                 "competitors": [fighter(1, "Jon Smith", "12-2-0", True), fighter(2, "Ray Doe", "10-4-0", False)],
                 "status": final},
                {"id": "b", "date": "2026-11-08T03:00Z", "type": {"id": "1", "abbreviation": "Flyweight"},
                 "cardSegment": {"id": "1", "name": "main", "description": "Main Card"},
                 "competitors": [fighter(1, "Brandon Moreno", "21-8-2"), fighter(2, "Amir Albazi", "17-1-0")],
                 "status": status("STATUS_SCHEDULED", "pre", False, "Sat, November 7th at 10:00 PM EST")},
            ],
            "status": {"type": {"state": "in", "name": "STATUS_IN_PROGRESS"}},
        }],
    }
    router = Router({f"{SITE}/mma/ufc/scoreboard": json_response(card)})
    out = call("get_sports_scores", router, league="UFC")
    ev = out["events"][0]
    assert ev["event"] == "UFC Fight Night: Moreno vs. Albazi" and ev["venue"] == "Arena CDMX, Mexico City, Mexico"
    first, main = ev["fights"]
    assert first == {"bout": "Jon Smith (12-2-0) vs Ray Doe (10-4-0)", "status": "final", "weight_class": "Lightweight",
                     "card": "Prelims", "winner": "Jon Smith", "method": "Submission", "round": 2, "time": "2:05"}
    assert main == {"bout": "Brandon Moreno (21-8-2) vs Amir Albazi (17-1-0)", "status": "scheduled",
                    "weight_class": "Flyweight", "card": "Main Card", "start": "Sat Nov 7, 10:00 PM EST"}
    out = call("get_sports_scores", router, league="ufc", team="Moreno")
    assert [f["bout"].split(" (")[0] for f in out["events"][0]["fights"]] == ["Brandon Moreno"]


# --------------------------------------------------------------------------- caching, errors, size
def test_scoreboard_cache_ttl_follows_live_state(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(sports.time, "time", lambda: clock[0])
    router = Router({f"{SITE}/basketball/nba/scoreboard": json_response(nba_board())})
    call("get_sports_scores", router, league="nba", date="today")
    call("get_sports_scores", router, league="nba", date="today")
    assert len(router.requests) == 1
    clock[0] += sports.LIVE_TTL + 1  # a live game: the board goes stale after ~30 s
    call("get_sports_scores", router, league="nba", date="today")
    assert len(router.requests) == 2

    finals = nba_board()
    finals["events"] = finals["events"][:2]
    assert sports._board_ttl(finals) == sports.FINAL_TTL
    assert sports._board_ttl(nba_board()) == sports.LIVE_TTL
    assert sports._board_ttl({"events": []}) == sports.UPCOMING_TTL


@pytest.mark.parametrize(
    "response,match",
    [
        (httpx.Response(503, text="<html>Service Unavailable</html>"), "unavailable right now \\(HTTP 503"),
        (httpx.Response(404, json={"code": 404, "message": "not found"}), "ESPN has no NBA scoreboard"),
        (httpx.Response(200, text="<html>not json</html>"), "unexpected reply"),
        (httpx.ConnectError("boom"), "Couldn't reach ESPN"),
        (httpx.ReadTimeout("slow"), "didn't answer in time"),
    ],
)
def test_failures_are_friendly(response, match):
    router = Router({f"{SITE}/basketball/nba/scoreboard": response})
    out = call("get_sports_scores", router, league="nba", date="today")
    assert "error" in out
    assert re.search(match, out["error"])
    assert "<html>" not in out["error"] and "Traceback" not in out["error"]


def test_429_pauses_the_host():
    router = Router({f"{SITE}/basketball/nba/scoreboard": httpx.Response(429, text="slow down")})
    out = call("get_sports_scores", router, league="nba", date="today")
    assert "limiting requests" in out["error"]
    out = call("get_sports_scores", router, league="nba", date="tomorrow")
    assert "limiting requests" in out["error"]
    assert len(router.requests) == 1  # the second call never hit ESPN during the cooldown


def test_host_limiter_spacing():
    t = [0.0]
    lim = HostLimiter(interval=0.5, max_wait=1.0, clock=lambda: t[0])
    assert lim.reserve("a") == 0
    assert lim.reserve("a") == 0.5
    assert lim.reserve("b") == 0  # per host
    assert lim.reserve("a") == 1.0
    with pytest.raises(ToolError, match="Too many"):
        lim.reserve("a")
    lim.cooldown("b", 10)
    with pytest.raises(ToolError, match="limiting"):
        lim.reserve("b")


def test_huge_college_slate_is_trimmed_under_budget():
    events = []
    for i in range(90):
        h = team(1000 + i, f"H{i}", f"Home State {i}", "Hawks")
        a = team(2000 + i, f"A{i}", f"Away Tech {i}", "Owls")
        events.append(event(
            500000 + i, "2026-11-07T17:00Z", competitor(h, "home", 28, True, "7-2"), competitor(a, "away", 14, False, "4-5"),
            status("STATUS_FINAL", "post", True, "Final", 4),
            headlines=[{"shortLinkText": "A long recap headline about a college football game " * 3}],
            broadcasts=[{"market": "national", "names": ["ESPN2"]}],
        ))
    router = Router({f"{SITE}/football/college-football/scoreboard": json_response({"events": events})})
    raw = run(_raw_call("get_sports_scores", router, league="college football", date="saturday"))
    assert len(raw) < 14000
    out = json.loads(raw)
    assert len(out["games"]) <= sports.MAX_GAMES
    assert "Showing" in out["note"] or "Trimmed" in out["note"]


async def _raw_call(name, handler, **args):
    async with mock_http(handler) as http:
        return await call_tool(name, args, make_ctx(http=http))


def test_college_team_filter_asks_for_all_division_games():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return json_response({"events": []})

    call("get_sports_scores", Router({f"{SITE}/football/college-football/scoreboard": handler}),
         league="ncaaf", date="saturday", team="Alabama")
    assert seen["groups"] == "80" and seen["dates"] == "20261107"


# --------------------------------------------------------------------------- team resolution
def test_match_score_and_resolve_team():
    nba = resolve_league("nba")
    assert resolve_team("LAL", NBA_TEAMS, nba)["id"] == "13"
    assert resolve_team("lakers", NBA_TEAMS, nba)["id"] == "13"
    assert resolve_team("Los Angeles Lakers", NBA_TEAMS, nba)["id"] == "13"
    assert resolve_team("LA Lakers", NBA_TEAMS, nba)["id"] == "13"
    assert resolve_team("Los Angeles", NBA_TEAMS, nba)["id"] == "13"
    assert resolve_team("clippers", NBA_TEAMS, nba)["id"] == "12"
    assert resolve_team("sixers", NBA_TEAMS, nba)["id"] == "20"
    assert resolve_team("wolves", NBA_TEAMS, nba)["id"] == "16"
    assert resolve_team("Golden State", NBA_TEAMS, nba)["id"] == "9"
    with pytest.raises(ToolError, match="couldn't find a NBA team called 'Sonics'"):
        resolve_team("Sonics", NBA_TEAMS, nba)

    nfl = resolve_league("nfl")
    nfl_teams = [team(19, "NYG", "New York", "Giants"), team(20, "NYJ", "New York", "Jets"),
                 team(25, "SF", "San Francisco", "49ers"), team(2, "BUF", "Buffalo", "Bills")]
    with pytest.raises(ToolError, match="matches several NFL teams: New York Giants \\(NYG\\), New York Jets \\(NYJ\\)"):
        resolve_team("New York", nfl_teams, nfl)
    assert resolve_team("niners", nfl_teams, nfl)["id"] == "25"
    assert resolve_team("NYJ", nfl_teams, nfl)["id"] == "20"

    epl = resolve_league("epl")
    assert resolve_team("Man City", EPL_TEAMS, epl)["id"] == "382"
    assert resolve_team("man utd", EPL_TEAMS, epl)["id"] == "360"
    assert resolve_team("Spurs", EPL_TEAMS, epl)["id"] == "367"
    atm = team(1068, "ATM", "Atlético Madrid", "Atlético Madrid", display="Atlético Madrid", short="Atlético")
    assert resolve_team("atletico madrid", [atm], resolve_league("laliga"))["id"] == "1068"  # accents folded
    assert resolve_team("atleti", [atm], resolve_league("laliga"))["id"] == "1068"
    mia = team(18418, "MIA", "Inter Miami CF", "Inter Miami CF", display="Inter Miami CF", short="Inter Miami")
    assert resolve_team("inter", [mia], resolve_league("mls"))["id"] == "18418"  # soccer alias doesn't block raw words

    ncaa = resolve_league("college-football")
    osu = team(194, "OSU", "Ohio State", "Buckeyes", short="Ohio State")
    ore = team(204, "ORST", "Oregon State", "Beavers", short="Oregon State")
    assert resolve_team("Ohio St", [osu, ore], ncaa)["id"] == "194"
    assert resolve_team("bama", [team(333, "ALA", "Alabama", "Crimson Tide", short="Alabama"), osu], ncaa)["id"] == "333"

    q = sports._norm("Max Verstappen")
    assert match_score(q, {"display": "Max Verstappen"}) == 98
    assert match_score("verstappen", sports._athlete_fields({"displayName": "Max Verstappen"})) == 85


# --------------------------------------------------------------------------- standings
def stat(name, abbr, value, display=None, **extra):
    s = {"name": name, "displayName": name, "shortDisplayName": abbr, "description": name, "abbreviation": abbr,
         "type": name.lower(), "value": value, "displayValue": display if display is not None else str(value)}
    s.update(extra)
    return s


def nba_entry(t, w, l, seed, gb, streak):
    return {"team": t, "stats": [
        stat("avgPointsAgainst", "OPP PPG", 108.2),
        stat("gamesBehind", "GB", float(gb), "-" if gb == 0 else str(gb)),
        stat("losses", "L", float(l)),
        stat("playoffSeed", "SEED", float(seed)),
        stat("streak", "STRK", 3.0, streak),
        stat("winPercent", "PCT", w / (w + l), f"{w / (w + l):.3f}".lstrip("0")),
        stat("wins", "W", float(w)),
        {"id": "0", "name": "overall", "abbreviation": "Any", "type": "total", "summary": f"{w}-{l}", "displayValue": f"{w}-{l}"},
    ]}


def nba_standings():
    return {
        "uid": "s:40~l:46~g:9", "id": "0", "name": "National Basketball Association", "abbreviation": "NBA",
        "children": [
            {"uid": "s:40~l:46~g:5", "id": "5", "name": "Eastern Conference", "abbreviation": "East", "isConference": True,
             "standings": {"id": "0", "name": "Eastern Conference", "displayName": "Eastern Conference", "season": 2027,
                           "seasonType": 2, "seasonDisplayName": "2026-27",
                           # ESPN doesn't promise rank order: entries here are deliberately shuffled
                           "entries": [nba_entry(NYK, 4, 3, 3, 1.5, "L1"), nba_entry(BOS, 6, 1, 1, 0, "W3"),
                                       nba_entry(MIA, 5, 2, 2, 1, "W1")]}},
            {"uid": "s:40~l:46~g:6", "id": "6", "name": "Western Conference", "abbreviation": "West", "isConference": True,
             "standings": {"name": "Western Conference", "seasonDisplayName": "2026-27",
                           "entries": [nba_entry(LAL, 4, 3, 2, 1, "W2"), nba_entry(GS, 5, 2, 1, 0, "L1")]}},
        ],
    }


def test_nba_standings_sorted_by_seed_and_group_filter():
    router = Router({f"{V2}/basketball/nba/standings": json_response(nba_standings())})
    out = call("get_sports_standings", router, league="nba")
    assert out["status"] == "NBA standings · 2 groups" and out["season"] == "2026-27"
    east = out["groups"][0]
    assert east["group"] == "Eastern Conference"
    assert [r["abbr"] for r in east["teams"]] == ["BOS", "MIA", "NY"]
    assert east["teams"][0] == {"rank": 1, "team": "Boston Celtics", "abbr": "BOS", "record": "6-1", "pct": ".857",
                                "gb": "-", "streak": "W3"}
    out = call("get_sports_standings", router, league="nba", group="west")
    assert [g["group"] for g in out["groups"]] == ["Western Conference"]
    assert out["status"] == "NBA standings · Western Conference"
    assert len(router.requests) == 1  # cached

    # A group that isn't there at either level: friendly list of what is.
    router.add(f"{V2}/basketball/nba/standings", lambda r: json_response(nba_standings()))
    sports._CACHE.clear()
    out = call("get_sports_standings", router, league="nba", group="Pacific")
    assert "No NBA group called 'Pacific'" in out["error"] and "Eastern Conference" in out["error"]


def nfl_entry(t, w, l, ties, seed):
    return {"team": t, "stats": [
        stat("wins", "W", float(w)), stat("losses", "L", float(l)), stat("ties", "T", float(ties)),
        stat("winPercent", "PCT", (w + ties / 2) / max(1, w + l + ties), ".750"),
        stat("playoffSeed", "SEED", float(seed)), stat("pointsFor", "PF", 250.0), stat("pointsAgainst", "PA", 180.0),
        stat("differential", "DIFF", 70.0, "+70"), stat("streak", "STRK", 2.0, "W2"),
        stat("clincher", "CLINCH", 0.0, "z" if seed == 1 else "-"),
    ]}


def test_nfl_divisions_via_level_3_and_ties():
    buf, mia_ = team(2, "BUF", "Buffalo", "Bills"), team(15, "MIA", "Miami", "Dolphins")
    kc, den = team(12, "KC", "Kansas City", "Chiefs"), team(7, "DEN", "Denver", "Broncos")
    conf = {"name": "National Football League", "children": [
        {"name": "American Football Conference", "abbreviation": "AFC",
         "standings": {"seasonDisplayName": "2026", "entries": [nfl_entry(kc, 7, 1, 0, 2), nfl_entry(buf, 7, 1, 0, 1),
                                                                nfl_entry(den, 5, 3, 0, 6), nfl_entry(mia_, 3, 4, 1, 12)]}},
        {"name": "National Football Conference", "abbreviation": "NFC", "standings": {"entries": []}},
    ]}
    divisions = {"name": "National Football League", "children": [
        {"name": "American Football Conference", "abbreviation": "AFC", "children": [
            {"name": "AFC East", "abbreviation": "AFC East",
             "standings": {"entries": [nfl_entry(mia_, 3, 4, 1, 12), nfl_entry(buf, 7, 1, 0, 1)]}},
            {"name": "AFC West", "abbreviation": "AFC West",
             "standings": {"entries": [nfl_entry(den, 5, 3, 0, 6), nfl_entry(kc, 7, 1, 0, 2)]}},
        ]},
    ]}

    def handler(request):
        return json_response(divisions if request.url.params.get("level") == "3" else conf)

    router = Router({f"{V2}/football/nfl/standings": handler})
    out = call("get_sports_standings", router, league="nfl", group="AFC")
    assert [r["abbr"] for r in out["groups"][0]["teams"]] == ["BUF", "KC", "DEN", "MIA"]
    assert out["groups"][0]["teams"][0]["clinched"] == "z"
    out = call("get_sports_standings", router, league="nfl", group="AFC East")
    assert [g["group"] for g in out["groups"]] == ["AFC East"]
    rows = out["groups"][0]["teams"]
    assert [r["abbr"] for r in rows] == ["BUF", "MIA"]
    assert rows[1]["record"] == "3-4-1"  # ties only shown when there are some
    assert rows[0]["record"] == "7-1"
    assert router.requests[-1].url.params["level"] == "3"


def test_soccer_table_points_and_notes():
    def entry(t, rank, w, d, l, pts, gd, note=None):
        e = {"team": t, "stats": [
            stat("gamesPlayed", "GP", float(w + d + l)), stat("losses", "L", float(l)),
            stat("pointDifferential", "GD", float(gd), f"{gd:+d}"), stat("points", "P", float(pts)),
            stat("pointsAgainst", "A", 10.0), stat("pointsFor", "F", 10.0 + gd), stat("rank", "R", float(rank)),
            stat("rankChange", "RC", 0.0), stat("ties", "D", float(d)), stat("wins", "W", float(w)),
            stat("deductions", "DED", 0.0), stat("ppg", "PPG", 2.1),
            {"name": "overall", "abbreviation": "Any", "type": "total", "summary": f"{w}-{d}-{l}"},
        ]}
        if note:
            e["note"] = {"color": "#81D6AC", "description": note, "rank": rank}
        return e

    table = {"name": "English Premier League", "abbreviation": "ENG.1", "children": [
        {"name": "English Premier League", "abbreviation": "ENG.1",
         "standings": {"seasonDisplayName": "2026-27", "entries": [
             entry(CHE, 3, 6, 2, 3, 20, 5), entry(ARS, 1, 8, 2, 1, 26, 14, "Champions League"),
             entry(MCI, 2, 7, 2, 2, 23, 11, "Champions League"), entry(TOT, 18, 2, 3, 6, 9, -9, "Relegation")]}},
    ]}
    router = Router({f"{V2}/soccer/eng.1/standings": json_response(table)})
    out = call("get_sports_standings", router, league="epl")
    rows = out["groups"][0]["teams"]
    assert [r["abbr"] for r in rows] == ["ARS", "MNC", "CHE", "TOT"]
    assert rows[0] == {"rank": 1, "team": "Arsenal", "abbr": "ARS", "record": "8-2-1", "pts": 26, "gp": 11, "gd": "+14",
                       "note": "Champions League"}
    assert rows[-1]["note"] == "Relegation" and rows[-1]["gd"] == "-9"


def test_nested_college_conferences_flatten_and_cap():
    def entry(t, w, l):
        return {"team": t, "stats": [stat("wins", "W", float(w)), stat("losses", "L", float(l)),
                                     stat("winPercent", "PCT", w / (w + l), f"{w / (w + l):.3f}")]}

    def conf_teams(prefix, n):
        return [entry(team(f"{prefix}{i}", f"{prefix}{i}", f"{prefix} School {i}", "Tigers"), 8 - (i % 8), i % 8)
                for i in range(n)]

    payload = {"name": "NCAA Division I", "children": [
        {"name": "Mid-American Conference", "abbreviation": "MAC", "children": [
            {"name": "MAC East", "abbreviation": "MAC East", "standings": {"entries": conf_teams("E", 6)}},
            {"name": "MAC West", "abbreviation": "MAC West", "standings": {"entries": conf_teams("W", 6)}},
        ]},
        {"name": "Southeastern Conference", "abbreviation": "SEC", "standings": {"entries": conf_teams("S", 16)}},
        {"name": "Big Ten Conference", "abbreviation": "Big Ten", "standings": {"entries": conf_teams("B", 18)}},
        {"name": "Atlantic Coast Conference", "abbreviation": "ACC", "standings": {"entries": conf_teams("A", 17)}},
        {"name": "Big 12 Conference", "abbreviation": "Big 12", "standings": {"entries": conf_teams("T", 16)}},
    ]}
    flat = sports._flatten_groups(payload)
    assert [g["name"] for g in flat] == ["MAC East", "MAC West", "Southeastern Conference", "Big Ten Conference",
                                         "Atlantic Coast Conference", "Big 12 Conference"]
    router = Router({f"{V2}/football/college-football/standings": json_response(payload)})
    out = call("get_sports_standings", router, league="college football")
    assert all(len(g["teams"]) <= 5 for g in out["groups"]) and "top 5" in out["note"]
    out = call("get_sports_standings", router, league="college football", group="SEC")
    assert len(out["groups"][0]["teams"]) == 16 and out["groups"][0]["teams"][0]["pct"] == "1.000"
    out = call("get_sports_standings", router, league="college football", group="MAC")
    assert [g["group"] for g in out["groups"]] == ["MAC East", "MAC West"]


def test_nhl_and_f1_standings():
    def nhl_entry(t, w, l, otl, pts):
        return {"team": t, "stats": [stat("wins", "W", float(w)), stat("losses", "L", float(l)),
                                     stat("otLosses", "OTL", float(otl)), stat("points", "PTS", float(pts)),
                                     stat("gamesPlayed", "GP", float(w + l + otl)), stat("streak", "STRK", 1.0, "W1")]}

    bru, tor = team(1, "BOS", "Boston", "Bruins"), team(21, "TOR", "Toronto", "Maple Leafs")
    nhl = {"children": [{"name": "Eastern Conference", "abbreviation": "East",
                         "standings": {"entries": [nhl_entry(bru, 7, 4, 2, 16), nhl_entry(tor, 9, 3, 1, 19)]}}]}
    f1 = {"name": "Formula 1", "children": [
        {"name": "Driver Standings", "abbreviation": "Driver Standings", "standings": {"entries": [
            {"athlete": {"displayName": "Lando Norris", "flag": {"alt": "UK"}},
             "stats": [stat("rank", "RK", 2.0), stat("championshipPts", "PTS", 357.0)]},
            {"athlete": {"displayName": "Max Verstappen", "flag": {"alt": "Netherlands"}},
             "stats": [stat("rank", "RK", 1.0), stat("championshipPts", "PTS", 393.0)]},
        ]}},
        {"name": "Constructor Standings", "abbreviation": "Constructor Standings", "standings": {"entries": [
            {"team": {"displayName": "McLaren", "abbreviation": "MCL"}, "stats": [stat("rank", "RK", 1.0), stat("points", "PTS", 608.0)]},
        ]}},
    ]}
    router = Router({f"{V2}/hockey/nhl/standings": json_response(nhl), f"{V2}/racing/f1/standings": json_response(f1)})
    rows = call("get_sports_standings", router, league="nhl")["groups"][0]["teams"]
    assert rows[0] == {"rank": 1, "team": "Toronto Maple Leafs", "abbr": "TOR", "record": "9-3-1", "pts": 19, "gp": 13, "streak": "W1"}
    out = call("get_sports_standings", router, league="f1")
    assert out["groups"][0]["teams"][0] == {"rank": 1, "team": "Max Verstappen", "pts": 393}
    out = call("get_sports_standings", router, league="f1", group="constructors")
    assert out["groups"][0]["teams"] == [{"rank": 1, "team": "McLaren", "abbr": "MCL", "pts": 608}]
    assert "UFC has no league standings" in call("get_sports_standings", router, league="ufc")["error"]


def test_standings_empty_offseason():
    router = Router({f"{V2}/basketball/wnba/standings": json_response({"name": "WNBA", "children": []})})
    out = call("get_sports_standings", router, league="wnba")
    assert "no WNBA standings right now" in out["error"]


# --------------------------------------------------------------------------- team schedule
def schedule_competitor(t, home_away, score=None, winner=None):
    c = {"id": t["id"], "type": "team", "order": 0 if home_away == "home" else 1, "homeAway": home_away,
         "team": {k: t[k] for k in ("id", "location", "nickname", "abbreviation", "displayName", "shortDisplayName", "logos")},
         "record": [{"id": "0", "abbreviation": "Any", "displayName": "Overall", "type": "total", "displayValue": "4-3"}]}
    if score is not None:
        c["score"] = {"value": float(score), "displayValue": str(score)}
    if winner is not None:
        c["winner"] = winner
    return c


def schedule_event(eid, when, home, away, st, **comp_extra):
    comp = {"id": str(eid), "date": when, "attendance": 18997, "type": {"id": "1", "abbreviation": "STD"},
            "timeValid": True, "neutralSite": False, "boxscoreAvailable": True, "ticketsAvailable": False,
            "venue": {"fullName": "Crypto.com Arena", "address": {"city": "Los Angeles", "state": "CA"}},
            "competitors": [home, away], "notes": [], "status": st}
    comp.update(comp_extra)
    return {"id": str(eid), "date": when, "name": "x", "shortName": "x",
            "season": {"year": 2027, "displayName": "2026-27"},
            "seasonType": {"id": "2", "type": 2, "name": "Regular Season", "abbreviation": "reg"},
            "timeValid": True, "competitions": [comp], "links": []}


def lakers_schedule():
    done = status("STATUS_FINAL", "post", True, "Final", 4)
    return {
        "timestamp": "2026-11-04T19:00:00Z", "status": "success",
        "season": {"year": 2027, "type": 2, "name": "Regular Season", "displayName": "2026-27"},
        "team": {"id": "13", "abbreviation": "LAL", "location": "Los Angeles", "name": "Lakers",
                 "displayName": "Los Angeles Lakers", "recordSummary": "4-3", "seasonSummary": "2026-27",
                 "standingSummary": "6th in Western Conference"},
        "events": [
            schedule_event(1, "2026-10-22T02:00Z", schedule_competitor(LAL, "home", 110, True), schedule_competitor(MIN, "away", 103, False), done),
            schedule_event(2, "2026-10-25T02:30Z", schedule_competitor(GS, "home", 120, True), schedule_competitor(LAL, "away", 112, False), done),
            schedule_event(3, "2026-11-01T02:30Z", schedule_competitor(LAL, "home", 118, True), schedule_competitor(PHX, "away", 117, False),
                           status("STATUS_FINAL", "post", True, "Final/OT", 5)),
            schedule_event(4, "2026-11-04T00:00Z", schedule_competitor(LAL, "home", 110, True), schedule_competitor(DEN, "away", 102, False), done),
            schedule_event(5, "2026-11-07T03:30Z", schedule_competitor(PHX, "home"), schedule_competitor(LAL, "away"),
                           status("STATUS_SCHEDULED", "pre", False, "Fri, November 6th at 10:30 PM EST"),
                           broadcasts=[{"type": {"id": "1", "shortName": "TV"}, "market": {"id": "1", "type": "National"},
                                        "media": {"shortName": "ESPN"}, "lang": "en", "region": "us"},
                                       {"type": {"id": "1", "shortName": "TV"}, "market": {"id": "2", "type": "Home"},
                                        "media": {"shortName": "Spectrum SN"}, "lang": "en", "region": "us"}]),
            schedule_event(6, "2026-11-09T03:30Z", schedule_competitor(LAL, "home"), schedule_competitor(BOS, "away"),
                           status("STATUS_SCHEDULED", "pre", False, "Sun, November 8th at 10:30 PM EST")),
        ],
        "byeWeek": 0,
    }


def test_team_schedule_recent_and_upcoming():
    router = Router({
        f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS)),
        f"{SITE}/basketball/nba/teams/13/schedule": json_response(lakers_schedule()),
    })
    out = call("get_team_schedule", router, league="nba", team="Lakers", count=3)
    assert out["status"] == ("Los Angeles Lakers (4-3, 6th in Western Conference) · last: W 110-102 vs Denver Nuggets"
                             " · next: Fri Nov 6, 10:30 PM EST at Phoenix Suns")
    assert out["record"] == "4-3" and out["season"] == "2026-27" and out["abbr"] == "LAL"
    assert [r["result"] for r in out["recent"]] == ["W 110-102", "W 118-117 (OT)", "L 112-120"]
    assert out["recent"][0] == {"date": "Tue Nov 3", "opponent": "vs Denver Nuggets", "status": "final", "result": "W 110-102"}
    assert out["upcoming"][0] == {"date": "Fri Nov 6, 10:30 PM EST", "opponent": "at Phoenix Suns", "status": "scheduled",
                                  "tv": "ESPN, Spectrum SN"}
    assert out["upcoming"][1]["opponent"] == "vs Boston Celtics"
    assert "bye_week" not in out
    # The team list is cached for the next lookup.
    call("get_team_schedule", router, league="nba", team="LAL")
    assert len([r for r in router.requests if r.url.path.endswith("/nba/teams")]) == 1


def test_soccer_schedule_merges_fixtures():
    done = status("STATUS_FULL_TIME", "post", True, "FT")
    results = {"team": {"id": "359", "displayName": "Arsenal", "recordSummary": "8-2-1", "standingSummary": "1st in English Premier League"},
               "events": [schedule_event(10, "2026-11-01T15:00Z", schedule_competitor(ARS, "home", 2, False),
                                         schedule_competitor(CHE, "away", 2, False), done)]}
    fixtures = {"team": results["team"],
                "events": [schedule_event(11, "2026-11-08T16:30Z", schedule_competitor(TOT, "home"), schedule_competitor(ARS, "away"),
                                          status("STATUS_SCHEDULED", "pre", False, "Sun, November 8th at 11:30 AM EST"))]}

    def handler(request):
        return json_response(fixtures if request.url.params.get("fixture") == "true" else results)

    router = Router({f"{SITE}/soccer/eng.1/teams": json_response(teams_payload(EPL_TEAMS, "eng.1", "Soccer")),
                     f"{SITE}/soccer/eng.1/teams/359/schedule": handler})
    out = call("get_team_schedule", router, league="epl", team="gunners")
    assert out["recent"][0]["result"] == "D 2-2"
    assert out["upcoming"][0] == {"date": "Sun Nov 8, 11:30 AM EST", "opponent": "at Tottenham Hotspur", "status": "scheduled"}
    assert "next: Sun Nov 8, 11:30 AM EST at Tottenham Hotspur" in out["status"]


def test_schedule_errors():
    router = Router({f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS)),
                     f"{SITE}/basketball/nba/teams/13/schedule": httpx.Response(404, json={"code": 404})})
    assert "ESPN has no schedule for the Los Angeles Lakers" in call("get_team_schedule", router, league="nba", team="lakers")["error"]
    assert "couldn't find a NBA team called 'Seattle Sonics'" in call("get_team_schedule", router, league="nba", team="Seattle Sonics")["error"]
    assert "no team schedules" in call("get_team_schedule", no_network, league="f1", team="McLaren")["error"]
    router = Router({f"{SITE}/basketball/nba/teams": json_response({"sports": []})})
    sports._CACHE.clear()
    assert "didn't return a team list" in call("get_team_schedule", router, league="nba", team="lakers")["error"]


# --------------------------------------------------------------------------- news
def article(i, headline, team_id=None, team_name=None, premium=False):
    cats = [{"id": 1, "type": "league", "uid": "s:40~l:46", "description": "NBA", "sportId": 46, "leagueId": 46}]
    if team_id:
        cats.append({"id": 2, "type": "team", "uid": f"s:40~l:46~t:{team_id}", "teamId": team_id,
                     "description": team_name, "team": {"id": team_id, "description": team_name}})
    return {
        "id": 40000000 + i, "nowId": f"1-{i}", "contentKey": f"{i}-1-5-1", "dataSourceIdentifier": f"abc{i}",
        "type": "HeadlineNews" if i % 2 else "Story",
        "headline": headline, "description": f"{headline}. More details inside.",
        "lastModified": "2026-11-04T18:10:00Z", "published": "2026-11-04T17:00:00Z",
        "images": [{"url": "https://a.espncdn.com/photo/x.jpg", "width": 1296, "height": 729}],
        "premium": premium,
        "links": {"web": {"href": f"https://www.espn.com/nba/story/_/id/{40000000 + i}/x"},
                  "mobile": {"href": "https://m.espn.com/x"}, "api": {"news": {"href": "https://api.espn.com/x"}}},
        "categories": cats, "byline": "Adrian Wojnarowski" if i == 1 else None,
    }


def test_sports_news_league_and_team():
    payload = {"header": "NBA News", "articles": [
        article(1, "Lakers' LeBron James questionable with ankle soreness", 13, "Los Angeles Lakers"),
        article(2, "Celtics extend Tatum", 2, "Boston Celtics", premium=True),
        article(3, "Power rankings: week 2"),
    ]}
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return json_response(payload)

    router = Router({f"{SITE}/basketball/nba/news": handler,
                     f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS))})
    out = call("get_sports_news", router, league="nba", count=2)
    assert out["status"] == "2 NBA headlines" and out["source"] == "ESPN"
    first = out["articles"][0]
    assert first["headline"].startswith("Lakers' LeBron James questionable")
    assert first["link"] == "https://www.espn.com/nba/story/_/id/40000001/x"
    assert first["published"] == "2026-11-04T12:00-05:00" and first["byline"] == "Adrian Wojnarowski"
    assert out["articles"][1]["premium"] is True
    assert seen[0]["limit"] == "2"

    out = call("get_sports_news", router, league="nba", team="lakers")
    assert seen[-1]["team"] == "13"
    assert [a["headline"][:6] for a in out["articles"]] == ["Lakers"]  # only stories about the Lakers
    assert out["team"] == "Los Angeles Lakers"


def test_sports_news_falls_back_to_google_news():
    rss = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Google News</title>
    <item><title>Verstappen takes pole in Brazil - Reuters</title><link>https://news.google.com/rss/articles/abc</link>
    <pubDate>Wed, 04 Nov 2026 16:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
    </channel></rss>"""
    router = Router({f"{SITE}/racing/f1/news": httpx.Response(502, text="<html>Bad gateway</html>"),
                     "news.google.com/rss/search": httpx.Response(200, text=rss)})
    out = call("get_sports_news", router, league="f1", team="Verstappen")
    assert out["source"] == "Google News (ESPN unavailable)"
    assert out["articles"][0]["headline"] == "Verstappen takes pole in Brazil"
    assert out["articles"][0]["byline"] == "Reuters"
    q = router.hits("news.google.com")[0].url.params["q"]
    assert "Verstappen" in q and "Formula 1" in q

    # Both down: the ESPN error comes through, friendly.
    sports._CACHE.clear()
    router = Router({f"{SITE}/racing/f1/news": httpx.Response(502), "news.google.com": httpx.Response(503)})
    from app.tools import news as news_mod
    news_mod._CACHE.clear()
    out = call("get_sports_news", router, league="f1")
    assert "ESPN's sports feed is unavailable" in out["error"]


# --------------------------------------------------------------------------- panel & summaries
def test_panel_helper_skips_empty_leagues(monkeypatch):
    monkeypatch.setenv("SPORTS_PANEL_LEAGUES", "nba, epl, mlb, quidditch")
    router = Router({
        f"{SITE}/basketball/nba/scoreboard": json_response(nba_board()),
        f"{SITE}/soccer/eng.1/scoreboard": json_response(epl_board()),
        f"{SITE}/baseball/mlb/scoreboard": json_response({"events": []}),
    })
    panel = ctx_call(lambda ctx: sports.scores_for_panel(ctx), router)
    assert [p["key"] for p in panel] == ["nba", "epl"]
    assert panel[0]["games"][0] == {"matchup": "BOS @ NY", "status": "live", "text": "Celtics 78, Knicks 74 · 4:32 - 3rd Quarter"}
    assert panel[1]["games"][1]["text"] == "Thu Nov 5, 3:00 PM EST"


def test_chip_summaries_read_well():
    from app.agent import _summarize

    router = Router({f"{SITE}/basketball/nba/scoreboard": json_response(nba_board())})
    raw = run(_raw_call("get_sports_scores", router, league="nba", date="today"))
    summary, ok = _summarize(raw)
    assert ok and summary.startswith("NBA on Wed Nov 4: 5 games")
    raw = run(_raw_call("get_sports_scores", no_network, league="nba", date="someday"))
    summary, ok = _summarize(raw)
    assert not ok and "couldn't read the date" in summary


def test_malformed_payloads_never_crash():
    junk_events = [
        None, "x", 5, {}, {"competitions": None}, {"competitions": ["bad", None, {}]},
        {"competitions": [{"competitors": [None, "x", {"team": None, "score": {"value": None}}], "status": "weird"}]},
        {"competitions": [{"competitors": [{"team": {"displayName": "Solo FC"}, "homeAway": "home", "score": "1"}],
                           "status": {"type": {"state": "post", "completed": True}}}]},
    ]
    for path, league in [("basketball/nba", "nba"), ("soccer/eng.1", "epl"), ("racing/f1", "f1"),
                         ("golf/pga", "pga"), ("mma/ufc", "ufc")]:
        sports._CACHE.clear()
        router = Router({f"{SITE}/{path}/scoreboard": json_response({"events": junk_events, "leagues": "nope", "week": 3})})
        out = call("get_sports_scores", router, league=league, team="solo")
        assert "error" not in out, (league, out)
        assert out["source"] == "ESPN"
    router = Router({f"{V2}/basketball/nba/standings": json_response(
        {"children": [None, {"name": "East", "standings": {"entries": [None, {"team": {"displayName": "A"}, "stats": [None, "x"]}]}}]})})
    out = call("get_sports_standings", router, league="nba")
    assert out["groups"][0]["teams"] == [{"rank": 1, "team": "A"}]


def test_golf_cut_players_and_dict_scores():
    board = golf_board()
    players = board["events"][0]["competitions"][0]["competitors"]
    players[0]["score"] = {"value": -15.0, "displayValue": "-15"}
    players.append(golfer(6, "Cut Player", "CUT", [75, 76]))
    router = Router({f"{SITE}/golf/pga/scoreboard": json_response(board)})
    ev = call("get_sports_scores", router, league="pga")["events"][0]
    assert [r["pos"] for r in ev["leaderboard"]] == ["1", "T2", "T2", "4", "5", "6"]
    assert ev["leaderboard"][0]["score"] == "-15" and ev["leaderboard"][5]["score"] == "CUT"


def test_unknown_espn_slug_reports_not_found():
    router = Router({f"{SITE}/soccer/xyz.9/scoreboard": httpx.Response(400, json={"code": 400, "message": "Failed to get league"})})
    out = call("get_sports_scores", router, league="soccer/xyz.9")
    assert out["error"] == "ESPN has no XYZ.9 scoreboard for the current slate."


def test_fit_drops_optional_fields_before_items():
    out = {"status": "x", "games": [{"matchup": f"G{i}", "headline": "h" * 400, "venue": "v" * 100} for i in range(20)]}
    fitted = sports._fit(out, "games", budget=3000)
    assert len(fitted["games"]) == 20 and all("headline" not in g for g in fitted["games"])
    assert len(json.dumps(fitted)) <= 3000


# --------------------------------------------------------------------------- review fixes: names, news, calendars, sizes
@pytest.mark.parametrize("mascot_nicknames", [False, True])
def test_shared_cities_and_mascots_ask_which_one(mascot_nicknames):
    """ESPN's /teams list repeats the city in "nickname"; whichever shape arrives, a shared city must be ambiguous."""
    def nfl(tid, abbr, loc, name):
        return team(tid, abbr, loc, name, nickname=name if mascot_nicknames else None)

    lg = resolve_league("nfl")
    teams = [nfl(19, "NYG", "New York", "Giants"), nfl(20, "NYJ", "New York", "Jets"),
             nfl(14, "LAR", "Los Angeles", "Rams"), nfl(24, "LAC", "Los Angeles", "Chargers"),
             nfl(12, "KC", "Kansas City", "Chiefs"), nfl(9, "GB", "Green Bay", "Packers")]
    for query, options in [("New York", "New York Giants \\(NYG\\), New York Jets \\(NYJ\\)"),
                           ("Los Angeles", "Los Angeles Rams \\(LAR\\), Los Angeles Chargers \\(LAC\\)"),
                           ("LA", "Los Angeles Rams \\(LAR\\), Los Angeles Chargers \\(LAC\\)"),
                           ("ny", "New York Giants")]:
        with pytest.raises(ToolError, match=f"matches several NFL teams: {options}.*Which one"):
            resolve_team(query, teams, lg)
    # City shorthand in front of the team name is how people say it.
    assert resolve_team("LA Rams", teams, lg)["id"] == "14"
    assert resolve_team("NY Giants", teams, lg)["id"] == "19"
    assert resolve_team("NY Jets", teams, lg)["id"] == "20"
    assert resolve_team("KC Chiefs", teams, lg)["id"] == "12"
    assert resolve_team("GB Packers", teams, lg)["id"] == "9"
    assert resolve_team("Kansas City", teams, lg)["id"] == "12"


def test_espn_spellings_and_mascot_ties():
    nba = resolve_league("nba")
    # ESPN spells the Clippers' city "LA": the full city name still finds them, and plain "LA" is the Clippers.
    assert resolve_team("Los Angeles Clippers", NBA_TEAMS, nba)["id"] == "12"
    assert resolve_team("LA Clippers", NBA_TEAMS, nba)["id"] == "12"
    assert resolve_team("Los Angeles Lakers", NBA_TEAMS, nba)["id"] == "13"
    assert resolve_team("philly", NBA_TEAMS, nba)["id"] == "20"
    # College mascots are shared: never settle them by list order.
    cfb = resolve_league("college-football")
    tigers = [team(2, "AUB", "Auburn", "Tigers", short="Auburn"), team(99, "LSU", "LSU", "Tigers", short="LSU"),
              team(228, "CLEM", "Clemson", "Tigers", short="Clemson"), team(142, "MIZ", "Missouri", "Tigers", short="Missouri")]
    with pytest.raises(ToolError, match="matches several College Football teams: Auburn Tigers \\(AUB\\), LSU Tigers"):
        resolve_team("Tigers", tigers, cfb)
    assert resolve_team("LSU", tigers, cfb)["id"] == "99"
    assert resolve_team("Clemson Tigers", tigers, cfb)["id"] == "228"

    # Through the tool: the question reaches the model as a friendly error, with no schedule fetched.
    nfl_teams = [team(19, "NYG", "New York", "Giants"), team(20, "NYJ", "New York", "Jets")]
    router = Router({f"{SITE}/football/nfl/teams": json_response(teams_payload(nfl_teams, "nfl", "Football"))})
    out = call("get_team_schedule", router, league="nfl", team="New York")
    assert "Which one?" in out["error"] and not router.hits("/schedule")


def test_unknown_team_on_scoreboard_says_why():
    router = Router({f"{SITE}/basketball/nba/scoreboard": json_response(nba_board()),
                     f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS))})
    out = call("get_sports_scores", router, league="nba", date="today", team="Lakerz")
    assert out["games"] == [] and "couldn't find a NBA team called 'Lakerz'" in out["note"]


@pytest.mark.parametrize("value,key", [("nfl football", "nfl"), ("NBA basketball", "nba"), ("premier league soccer", "epl"),
                                       ("MLS soccer", "mls"), ("nhl hockey", "nhl"), ("la liga results", "laliga")])
def test_league_names_with_a_sport_word(value, key):
    assert resolve_league(value).key == key


def test_news_trimming_keeps_every_headline():
    long = "Trade deadline tracker: every deal, rumor and signing across the league " * 3
    payload = {"articles": [dict(article(i, f"{i}: {long}"), description=long * 2, byline="A Long Byline, ESPN Staff Writer")
                            for i in range(25)]}
    router = Router({f"{SITE}/basketball/nba/news": json_response(payload)})
    raw = run(_raw_call("get_sports_news", router, league="nba", count=20))
    assert len(raw) < 14000
    out = json.loads(raw)
    assert out["articles"] and all(a["headline"].split(":")[0].isdigit() for a in out["articles"])
    assert all("link" in a for a in out["articles"])


def test_news_team_filter_matches_whole_words():
    sac = team(23, "SAC", "Sacramento", "Kings")
    payload = {"articles": [
        article(1, "NBA power rankings: who climbed in week 2"),  # 'kings' is inside 'rankings': not about them
        article(2, "Kings' Fox scores 40 in comeback"),  # untagged, but names them (possessive)
        article(3, "De'Aaron Fox questionable", 23, "Sacramento Kings"),  # tagged with the team id
        article(4, "Heat check: the hottest shooters"),
    ]}
    router = Router({f"{SITE}/basketball/nba/news": json_response(payload),
                     f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS + [sac]))})
    out = call("get_sports_news", router, league="nba", team="kings")
    assert [a["headline"][:10] for a in out["articles"]] == ["Kings' Fox", "De'Aaron F"]
    assert sports._has_phrase(["verstappens", "pole"], ["verstappen"])
    assert not sports._has_phrase(["programs", "cut"], ["rams"])


def test_standings_eliminated_is_not_a_clinch():
    nyj, buf = team(20, "NYJ", "New York", "Jets"), team(2, "BUF", "Buffalo", "Bills")
    payload = {"children": [{"name": "American Football Conference", "abbreviation": "AFC", "standings": {"entries": [
        dict(nfl_entry(nyj, 2, 9, 0, 15), stats=[s for s in nfl_entry(nyj, 2, 9, 0, 15)["stats"] if s["name"] != "clincher"]
             + [stat("clincher", "CLINCH", 0.0, "e")]),
        nfl_entry(buf, 9, 2, 0, 1),
    ]}}]}
    rows = call("get_sports_standings", Router({f"{V2}/football/nfl/standings": json_response(payload)}), league="nfl")["groups"][0]["teams"]
    assert rows[0]["abbr"] == "BUF" and rows[0]["clinched"] == "z"
    assert rows[1]["eliminated"] is True and "clinched" not in rows[1]


def test_weekly_and_event_calendars_point_ahead():
    nfl_board = {"leagues": [{"abbreviation": "NFL", "calendarType": "list", "calendar": [
        {"label": "Preseason", "value": "1", "entries": [
            {"label": "Hall of Fame Weekend", "detail": "Jul 30-Aug 5", "value": "1",
             "startDate": "2026-07-30T07:00Z", "endDate": "2026-08-05T06:59Z"}]},
        {"label": "Regular Season", "value": "2", "startDate": "2026-09-10T07:00Z", "endDate": "2027-01-07T07:59Z", "entries": [
            {"label": "Week 9", "alternateLabel": "Wk 9", "detail": "Oct 29-Nov 4", "value": "9",
             "startDate": "2026-10-29T07:00Z", "endDate": "2026-11-04T07:59Z"},
            {"label": "Week 10", "alternateLabel": "Wk 10", "detail": "Nov 5-11", "value": "10",
             "startDate": "2026-11-05T08:00Z", "endDate": "2026-11-12T07:59Z"},
            {"label": "Week 11", "alternateLabel": "Wk 11", "detail": "Nov 12-18", "value": "11",
             "startDate": "2026-11-12T08:00Z", "endDate": "2026-11-19T07:59Z"}]}]}],
        "season": {"type": 2, "year": 2026}, "week": {"number": 9}, "events": []}
    out = call("get_sports_scores", Router({f"{SITE}/football/nfl/scoreboard": json_response(nfl_board)}), league="nfl", date="today")
    assert out["status"] == "No NFL games on Wed Nov 4"
    assert out["coming_up"] == ["Week 10 (Nov 5-11)", "Week 11 (Nov 12-18)"]
    assert "omit date for the current week" in out["note"]

    f1_cal = {"leagues": [{"abbreviation": "F1", "calendar": [
        {"label": "Mexico City Grand Prix", "startDate": "2026-10-23T18:30Z", "endDate": "2026-10-25T20:00Z"},
        {"label": "São Paulo Grand Prix", "startDate": "2026-11-06T14:30Z", "endDate": "2026-11-08T19:00Z",
         "event": {"$ref": "http://sports.core.api.espn.com/v2/sports/racing/leagues/f1/events/600052412"}},
        {"label": "Las Vegas Grand Prix", "startDate": "2026-11-20T00:30Z", "endDate": "2026-11-22T06:00Z"}]}], "events": []}
    sports._CACHE.clear()
    out = call("get_sports_scores", Router({f"{SITE}/racing/f1/scoreboard": json_response(f1_cal)}), league="f1", date="today")
    assert out["events"] == []
    assert out["coming_up"] == ["São Paulo Grand Prix (Fri Nov 6 - Sun Nov 8)", "Las Vegas Grand Prix (Thu Nov 19 - Sun Nov 22)"]
    assert "omit date for the current or next event" in out["note"]


def test_college_default_view_is_labelled():
    ala, aub = team(333, "ALA", "Alabama", "Crimson Tide", short="Alabama"), team(2, "AUB", "Auburn", "Tigers", short="Auburn")
    g = event(401, "2026-11-07T20:30Z", competitor(ala, "home", curatedRank={"current": 5}),
              competitor(aub, "away", curatedRank={"current": 99}), status("STATUS_SCHEDULED", "pre", False, "Sat 3:30 PM"))
    router = Router({f"{SITE}/football/college-football/scoreboard": json_response({"events": [g]})})
    out = call("get_sports_scores", router, league="cfb", date="saturday")
    assert "Top 25" in out["note"]
    assert out["games"][0]["home"]["rank"] == 5 and "rank" not in out["games"][0]["away"]  # 99 = unranked


def test_429_honours_retry_after():
    router = Router({f"{SITE}/basketball/nba/scoreboard": httpx.Response(429, headers={"Retry-After": "120"}, text="slow")})
    out = call("get_sports_scores", router, league="nba", date="today")
    assert "about 120 seconds" in out["error"]
    out = call("get_sports_standings", Router({f"{V2}/basketball/nba/standings": no_network}), league="nba")
    assert "limiting requests" in out["error"]  # site.api.espn.com is paused for every endpoint


def test_cache_drops_expired_entries_first(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sports.time, "time", lambda: clock[0])
    monkeypatch.setattr(sports, "CACHE_MAX", 3)
    sports._cache_put(("old",), 10, {"n": 0})
    sports._cache_put(("a",), 600, {"n": 1})
    sports._cache_put(("b",), 600, {"n": 2})
    clock[0] += 60  # "old" has expired
    sports._cache_put(("a",), 600, {"n": 11})  # refreshed: moves to the back
    sports._cache_put(("c",), 600, {"n": 3})
    assert list(sports._CACHE) == [("b",), ("a",), ("c",)]
    sports._cache_put(("d",), 600, {"n": 4})  # nothing expired: the least recently stored goes
    assert list(sports._CACHE) == [("a",), ("c",), ("d",)] and sports._cache_get(("a",)) == {"n": 11}


def test_host_limiter_waits_across_concurrent_callers():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    lim = HostLimiter(interval=0.5, max_wait=5, clock=lambda: 100.0, sleep=fake_sleep)

    async def go():
        import asyncio
        await asyncio.gather(*(lim.wait("site.api.espn.com") for _ in range(3)))

    run(go())
    run(go())  # a second event loop shares the same reservations
    assert slept == [0.5, 1.0, 1.5, 2.0, 2.5]


def _nba_season(last_game_utc, season_type=2):
    done = status("STATUS_FINAL", "post", True, "Final", 4)
    return {"season": {"year": 2027, "type": season_type, "name": "Regular Season", "displayName": "2026-27"},
            "requestedSeason": {"year": 2027, "type": season_type, "name": "Regular Season", "displayName": "2026-27"},
            "team": {"id": "13", "abbreviation": "LAL", "displayName": "Los Angeles Lakers", "recordSummary": "50-32"},
            "events": [schedule_event(1, "2026-04-01T02:00Z", schedule_competitor(LAL, "home", 110, True), schedule_competitor(MIN, "away", 103, False), done),
                       schedule_event(2, last_game_utc, schedule_competitor(GS, "home", 120, True), schedule_competitor(LAL, "away", 112, False), done)]}


def test_playoff_games_are_merged_after_the_regular_season():
    playoffs = {"events": [schedule_event(9, "2026-11-06T02:00Z", schedule_competitor(LAL, "home"), schedule_competitor(DEN, "away"),
                                          status("STATUS_SCHEDULED", "pre", False, "Thu 9:00 PM"),
                                          notes=[{"type": "event", "headline": "West 1st Round - Game 1"}])]}

    def handler(request):
        return json_response(playoffs if request.url.params.get("seasontype") == "3" else _nba_season("2026-11-02T02:30Z"))

    router = Router({f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS)),
                     f"{SITE}/basketball/nba/teams/13/schedule": handler})
    out = call("get_team_schedule", router, league="nba", team="lakers")
    assert out["upcoming"][0]["opponent"] == "vs Denver Nuggets" and out["upcoming"][0]["note"] == "West 1st Round - Game 1"
    assert out["recent"][0]["result"] == "L 112-120"

    # Deep in the offseason: no extra request.
    sports._CACHE.clear()
    router = Router({f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS)),
                     f"{SITE}/basketball/nba/teams/13/schedule": lambda r: json_response(_nba_season("2026-06-01T02:30Z"))})
    out = call("get_team_schedule", router, league="nba", team="lakers")
    assert out["upcoming"] == [] and "No upcoming games" in out["note"]
    assert all("seasontype" not in r.url.params for r in router.requests)

    # The postseason lookup failing is harmless.
    sports._CACHE.clear()

    def flaky(request):
        if request.url.params.get("seasontype"):
            return httpx.Response(500, text="<html>oops</html>")
        return json_response(_nba_season("2026-11-02T02:30Z"))

    router = Router({f"{SITE}/basketball/nba/teams": json_response(teams_payload(NBA_TEAMS)),
                     f"{SITE}/basketball/nba/teams/13/schedule": flaky})
    out = call("get_team_schedule", router, league="nba", team="lakers")
    assert "error" not in out and out["recent"][0]["result"] == "L 112-120"


def test_schedule_never_puts_odd_ids_in_the_url():
    odd = dict(LAL, id="../../news")
    router = Router({f"{SITE}/basketball/nba/teams": json_response(teams_payload([odd]))})
    out = call("get_team_schedule", router, league="nba", team="lakers")
    assert "no usable id" in out["error"] and len(router.requests) == 1


def test_one_huge_standings_table_stays_under_budget():
    def entry(i):
        t = team(5000 + i, f"T{i}", f"Somewhere Quite Far Away {i}", "Fighting Long Mascots")
        return {"team": t, "note": {"description": "Promotion play-off qualification place"},
                "stats": [stat("rank", "R", float(i + 1)), stat("wins", "W", 20.0), stat("losses", "L", 10.0),
                          stat("ties", "D", 5.0), stat("points", "P", 65.0), stat("gamesPlayed", "GP", 35.0),
                          stat("pointDifferential", "GD", 12.0, "+12")]}

    payload = {"children": [{"name": "Giant League", "standings": {"entries": [entry(i) for i in range(120)]}}]}
    raw = run(_raw_call("get_sports_standings", Router({f"{V2}/soccer/xyz.2/standings": json_response(payload)}), league="soccer/xyz.2"))
    assert len(raw) < 14000
    out = json.loads(raw)
    rows = out["groups"][0]["teams"]
    assert rows[0]["rank"] == 1 and len(rows) < 120 and "rows" in out["note"]


def test_many_college_conferences_stay_under_budget():
    def entry(t, w, l):
        return {"team": t, "stats": [stat("wins", "W", float(w)), stat("losses", "L", float(l)),
                                     stat("winPercent", "PCT", w / (w + l), f"{w / (w + l):.3f}"),
                                     stat("streak", "STRK", 1.0, "W1")]}

    confs = [{"name": f"Conference Number {c} Athletic Association", "abbreviation": f"C{c}",
              "standings": {"entries": [entry(team(c * 100 + i, f"S{c}{i}", f"University of Place {c}-{i}", "Mascots"), 10 - i, i + 1)
                                        for i in range(11)]}} for c in range(32)]
    router = Router({f"{V2}/basketball/mens-college-basketball/standings": json_response({"children": confs})})
    raw = run(_raw_call("get_sports_standings", router, league="college basketball"))
    assert len(raw) < 14000
    out = json.loads(raw)
    assert all(len(g["teams"]) <= 5 for g in out["groups"]) and "pass group" in out["note"]


def test_espn_day_stamps_keep_their_day_west_of_eastern(monkeypatch):
    """Golf days and TBD kick-offs are stamped at midnight Eastern; a Pacific user must still see the right day,
    while real start times are converted as usual."""
    monkeypatch.setenv("SPORTS_TIMEZONE", "America/Los_Angeles")
    router = Router({f"{SITE}/golf/pga/scoreboard": json_response(golf_board())})
    ev = call("get_sports_scores", router, league="pga")["events"][0]
    assert ev["dates"] == "Thu Nov 5 - Sun Nov 8"  # not "Wed Nov 4 - Sat Nov 7"

    ala, aub = team(333, "ALA", "Alabama", "Crimson Tide", short="Alabama"), team(2, "AUB", "Auburn", "Tigers", short="Auburn")
    tbd = event(402, "2026-11-07T05:00Z", competitor(ala, "home"), competitor(aub, "away"),
                status("STATUS_SCHEDULED", "pre", False, "TBD"), timeValid=False)
    night = event(403, "2026-11-08T03:30Z", competitor(aub, "home"), competitor(ala, "away"),
                  status("STATUS_SCHEDULED", "pre", False, "Sat 10:30 PM EST"))
    router = Router({f"{SITE}/football/college-football/scoreboard": json_response({"events": [tbd, night]})})
    games = call("get_sports_scores", router, league="college-football")["games"]
    assert games[0]["start"] == "Sat Nov 7, time TBD"
    assert games[1]["start"] == "Sat Nov 7, 7:30 PM PST"
    assert sports._date_span(sports._parse_dt("2026-11-06T14:30Z"), sports._parse_dt("2026-11-08T19:00Z"),
                             ZoneInfo("Asia/Tokyo")) == "Fri Nov 6 - Mon Nov 9"  # real times: the user's own days


@pytest.mark.parametrize(
    "response,match",
    [
        (httpx.Response(200, text=""), "unexpected reply"),
        (httpx.Response(200, json=[1, 2, 3]), "unexpected reply"),
        (httpx.Response(200, json={"code": 400, "message": "Failed to get league"}), "ESPN has no NBA scoreboard"),
        (httpx.Response(403, text="<html>Access denied</html>"), "unavailable right now \\(HTTP 403"),
        (httpx.RemoteProtocolError("peer closed connection"), "Couldn't reach ESPN"),
    ],
)
def test_more_failures_are_friendly(response, match):
    out = call("get_sports_scores", Router({f"{SITE}/basketball/nba/scoreboard": response}), league="nba", date="today")
    assert re.search(match, out["error"]) and "<html>" not in out["error"]


def test_redirects_are_followed():
    target = f"https://{SITE}/basketball/nba/scoreboard-v2"
    router = Router({f"{SITE}/basketball/nba/scoreboard": httpx.Response(301, headers={"Location": target}),
                     f"{SITE}/basketball/nba/scoreboard-v2": json_response(nba_board())})
    out = call("get_sports_scores", router, league="nba", date="today")
    assert out["status"].startswith("NBA on Wed Nov 4: 5 games")
