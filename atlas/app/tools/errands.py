"""Travel and everyday errands: directions, nearby places, rides, food delivery, parcels and currency.

Directions and place search use OpenStreetMap's free, keyless services (Nominatim for geocoding, OSRM for
routing). Their fair-use rules ask for at most one request per second and an identifying User-Agent, so
requests go through a module-level rate limiter and results are cached.

Rides and food orders only open the service's own page with the trip or search pre-filled: ATLAS never
requests a ride or pays for an order from here. The user confirms in the app, or, for food, ATLAS can carry on
in its own browser (browser_* tools), where the final checkout click asks the user to approve.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx

from ..config import DATA_DIR, load_env_file
from ..util import clip
from . import ToolError, tool
from .markets import fetch_quote

USER_AGENT = "ATLAS-assistant/1.0 (personal assistant)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_DEMO_URL = "https://router.project-osrm.org/route/v1/"
# The public OSRM demo server only has the car profile loaded (the profile in the URL is ignored), so walking
# and cycling go to the FOSSGIS servers that openstreetmap.org itself uses, which run real foot/bike profiles.
FOSSGIS_URLS = {
    "walking": "https://routing.openstreetmap.de/routed-foot/route/v1/driving/",
    "bicycling": "https://routing.openstreetmap.de/routed-bike/route/v1/driving/",
}
OSRM_PROFILES = {"driving": "driving", "walking": "walking", "bicycling": "cycling"}
# A "walking" route faster than this came from a car profile; recompute it from the distance.
NOMINAL_KMH = {"walking": 5.0, "bicycling": 16.0}
MAX_PLAUSIBLE_KMH = {"walking": 8.0, "bicycling": 30.0}
ESTIMATE_TIMEOUT = 25.0
OPEN_TIMEOUT = 15.0  # give up waiting on the system browser after this long
OSM_CREDIT = "© OpenStreetMap contributors"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def _setting(key: str, default: str = "") -> str:
    return str(os.environ.get(key) or load_env_file().get(key) or default).strip()


def _text(value) -> str:
    """Tool arguments as a single-spaced string (models sometimes send a ZIP code or tracking number as a number)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())


def _flag(value, default: bool = True) -> bool:
    """Models sometimes send booleans as strings ("false")."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)


# --------------------------------------------------------------------------- rate limiting & caching
class RateLimiter:
    """Spaces calls at least `interval` seconds apart, across concurrent callers.

    Each caller reserves the next free slot under a thread lock and then sleeps until it, so the limiter
    isn't tied to one event loop.
    """

    def __init__(self, interval: float, max_wait: float = 10.0, clock=time.monotonic, sleep=None):
        self.interval = interval
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def reserve(self) -> float:
        """Claim the next free slot and return how many seconds the caller must wait for it."""
        with self._lock:
            now = self.clock()
            slot = max(now, self._next)
            delay = slot - now
            if delay > self.max_wait:
                raise ToolError("Too many map lookups at once. Try again in a few seconds.")
            self._next = slot + self.interval
            return delay

    async def wait(self) -> None:
        delay = self.reserve()
        if delay > 0:
            await (self.sleep or asyncio.sleep)(delay)


_NOMINATIM_LIMIT = RateLimiter(1.0)
_OSRM_LIMIT = RateLimiter(1.0)

CACHE_MAX = 256
GEO_TTL = 24 * 3600
ROUTE_TTL = 15 * 60
_GEO_CACHE: dict[tuple, tuple[float, list]] = {}
_ROUTE_CACHE: dict[tuple, tuple[float, dict]] = {}


def _cache_get(cache: dict, key, ttl: float):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(cache: dict, key, value) -> None:
    cache[key] = (time.time(), value)
    while len(cache) > CACHE_MAX:
        cache.pop(next(iter(cache)))


# --------------------------------------------------------------------------- places & geocoding
_HERE = {"here", "me", "near me", "my location", "current location", "my current location", "where i am", "my position"}
_HOME = {"home", "my home", "my house", "my place", "my apartment", "house"}
_WORK = {"work", "my work", "office", "my office", "the office", "my job"}
_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _resolve_place(text: str | None, home_city: str = "", allow_city: bool = True) -> tuple[str | None, str]:
    """Map 'home' / 'work' / 'here' to saved addresses.

    Returns (place, source); place is None for the user's current location.
    """
    place = _text(text)
    low = place.lower().strip(" .!?")
    if low in _HERE:
        return None, "current location"
    if low in _HOME:
        address = _setting("HOME_ADDRESS")
        if address:
            return address, "home address"
        if home_city and allow_city:
            return home_city, "home city (approximate: set a home address in Settings for door-to-door)"
        raise ToolError("I don't know your home address yet. Tell me the address, or add it in Settings (Home address).")
    if low in _WORK:
        address = _setting("WORK_ADDRESS")
        if address:
            return address, "work address"
        raise ToolError("I don't know your work address yet. Tell me the address, or add it in Settings (Work address).")
    return place, "as given"


async def nominatim_search(http, query: str, limit: int = 1, details: bool = False) -> list[dict]:
    """Free-text Nominatim search (rate-limited to 1 request/second, cached)."""
    q = " ".join((query or "").split())
    if not q:
        raise ToolError("Say which place to look up.")
    key = (q.lower(), int(limit), bool(details))
    cached = _cache_get(_GEO_CACHE, key, GEO_TTL)
    if cached is not None:
        return cached
    params = {"format": "jsonv2", "q": q, "limit": int(limit)}
    if details:
        params["addressdetails"] = 1
    await _NOMINATIM_LIMIT.wait()
    try:
        r = await http.get(
            NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=12
        )
    except httpx.TimeoutException:
        raise ToolError("OpenStreetMap's place search didn't answer in time. Try again in a moment.") from None
    except httpx.HTTPError as exc:
        raise ToolError(f"Couldn't reach OpenStreetMap's place search ({exc.__class__.__name__}).") from None
    if r.status_code in (403, 429):
        raise ToolError("OpenStreetMap's place search is limiting requests right now. Try again in a minute.")
    if r.status_code >= 400:
        raise ToolError(f"OpenStreetMap's place search is unavailable (HTTP {r.status_code}). Try again later, or use web_search.")
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise ToolError("The place search sent an unexpected reply.")
    results = [d for d in data if isinstance(d, dict) and _coords_ok(d)]
    _cache_put(_GEO_CACHE, key, results)
    return results


def _coords_ok(item: dict) -> bool:
    try:
        lat, lon = float(item.get("lat")), float(item.get("lon"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


async def geocode(http, place: str) -> dict:
    """Return {"lat", "lon", "name"} for a place name, address or "lat,lon" pair."""
    m = _COORDS.match(place or "")
    if m:
        lat, lon = float(m[1]), float(m[2])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {"lat": lat, "lon": lon, "name": f"{lat},{lon}"}
    results = await nominatim_search(http, place, 1)
    if not results:
        raise ToolError(f"Couldn't find '{place}' on the map.")
    best = results[0]
    return {"lat": float(best["lat"]), "lon": float(best["lon"]), "name": best.get("display_name") or place}


def _short_address(a: dict) -> str:
    street = " ".join(str(x) for x in (a.get("house_number"), a.get("road") or a.get("pedestrian")) if x)
    locality = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("suburb") or a.get("county")
    region = " ".join(str(x) for x in (a.get("state"), a.get("postcode")) if x)
    parts = [p for p in (street, locality, region, a.get("country")) if p]
    return ", ".join(dict.fromkeys(parts))


def maps_search_url(query: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": query})


def _place_entry(item: dict) -> dict:
    display = item.get("display_name") or ""
    name = item.get("name") or display.split(",")[0].strip()
    address = _short_address(item.get("address") or {}) or display
    lookup = address if not name or address.lower().startswith(name.lower()) else f"{name}, {address}"
    return {
        "name": name,
        "address": address,
        "type": str(item.get("type") or item.get("category") or "").replace("_", " "),
        "lat": round(float(item["lat"]), 6),
        "lon": round(float(item["lon"]), 6),
        "maps_url": maps_search_url(lookup),
    }


_NEAR_ME = re.compile(r"\s+(?:near\s+(?:me|here|my\s+location)|nearby|close\s+by|around\s+(?:me|here))\s*$", re.I)


# "walk-in clinic", "drive-in", "dine in" are kinds of place, not "<thing> in <location>".
_IN_COMPOUNDS = re.compile(r"\b(?:walk|drive|dine|check|plug|sign|log|pop|take|fly|drop)[\s-]+in\b", re.I)


def _has_location(query: str) -> bool:
    q = _IN_COMPOUNDS.sub(" ", query)
    return bool(re.search(r"\b(in|near|around)\s+\S", q, re.I) or "," in q)


# --------------------------------------------------------------------------- directions
_MODE_ALIASES = {
    "driving": "driving", "drive": "driving", "car": "driving", "auto": "driving",
    "walking": "walking", "walk": "walking", "foot": "walking", "on foot": "walking",
    "transit": "transit", "public transport": "transit", "public transit": "transit", "bus": "transit",
    "train": "transit", "subway": "transit", "metro": "transit",
    "bicycling": "bicycling", "bicycle": "bicycling", "bike": "bicycling", "cycling": "bicycling", "cycle": "bicycling",
}
_MODE_WORDS = {"driving": "by car", "walking": "on foot", "bicycling": "by bike", "transit": "by transit"}


def _mode(value: str | None) -> str:
    key = " ".join((value or "driving").lower().split())
    if key not in _MODE_ALIASES:
        raise ToolError("Travel mode must be driving, walking, transit or bicycling.")
    return _MODE_ALIASES[key]


def directions_url(destination: str, origin: str | None = None, mode: str = "driving") -> str:
    """Google Maps URLs API directions link. Without an origin Google starts from the user's location."""
    params = {"api": "1"}
    if origin:
        params["origin"] = origin
    params["destination"] = destination
    params["travelmode"] = mode
    return "https://www.google.com/maps/dir/?" + urlencode(params)


async def osrm_route(http, mode: str, a: dict, b: dict) -> dict:
    """Distance (m) and duration (s) between two points. No live traffic."""
    coords = f"{a['lon']:.6f},{a['lat']:.6f};{b['lon']:.6f},{b['lat']:.6f}"
    key = (mode, coords)
    cached = _cache_get(_ROUTE_CACHE, key, ROUTE_TTL)
    if cached is not None:
        return cached
    bases = ([FOSSGIS_URLS[mode]] if mode in FOSSGIS_URLS else []) + [OSRM_DEMO_URL + OSRM_PROFILES[mode] + "/"]
    last: Exception | None = None
    for base in bases:
        try:
            await _OSRM_LIMIT.wait()
            r = await http.get(base + coords, params={"overview": "false"}, headers={"User-Agent": USER_AGENT}, timeout=12)
            data = r.json()  # OSRM sends JSON bodies with its error codes too
            if not isinstance(data, dict):
                raise ValueError("unexpected reply")
            code = data.get("code")
            if code == "NoRoute":
                raise ToolError("There's no route between those places.")
            routes = data.get("routes") or []
            if code != "Ok" or not routes:
                raise ValueError(data.get("message") or code or f"HTTP {r.status_code}")
            route = {"distance_m": float(routes[0]["distance"]), "duration_s": float(routes[0]["duration"]), "approximate": False}
        except ToolError:
            raise
        except Exception as exc:
            last = exc
            continue
        if mode in NOMINAL_KMH:
            hours = route["duration_s"] / 3600
            speed = (route["distance_m"] / 1000) / hours if hours > 0 else math.inf
            if speed > MAX_PLAUSIBLE_KMH[mode]:  # a car-profile answer: estimate from typical speed instead
                route["duration_s"] = route["distance_m"] / 1000 / NOMINAL_KMH[mode] * 3600
                route["approximate"] = True
        _cache_put(_ROUTE_CACHE, key, route)
        return route
    raise ToolError(f"The routing service is unavailable ({last.__class__.__name__ if last else 'no reply'}).")


def _fmt_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _fmt_distance(meters: float, imperial: bool) -> str:
    if imperial and meters < 160:  # under 0.1 mi
        return f"{max(10, round(meters * 3.28084, -1)):.0f} ft"
    if not imperial and meters < 1000:
        return f"{max(10, round(meters, -1)):.0f} m"
    value, unit = (meters / 1609.344, "mi") if imperial else (meters / 1000, "km")
    return f"{value:.1f} {unit}" if value < 100 else f"{value:,.0f} {unit}"


async def _estimate(ctx, origin: str, destination: str, mode: str) -> dict:
    a = await geocode(ctx.http, origin)
    b = await geocode(ctx.http, destination)
    route = await osrm_route(ctx.http, mode, a, b)
    imperial = ctx.settings.units != "metric"
    est = {
        "duration": _fmt_duration(route["duration_s"]),
        "distance": _fmt_distance(route["distance_m"], imperial),
        "duration_min": round(route["duration_s"] / 60),
        "distance_km": round(route["distance_m"] / 1000, 1),
        "distance_mi": round(route["distance_m"] / 1609.344, 1),
        "from": clip(a["name"], 140),
        "to": clip(b["name"], 140),
        "source": f"OSRM routing on OpenStreetMap data ({OSM_CREDIT}); typical speeds, no live traffic",
    }
    if route["approximate"]:
        est["approximate"] = True
        est["source"] += f"; {mode} time estimated from the route distance at a typical {NOMINAL_KMH[mode]:g} km/h"
    return est


_CONSOLE_BROWSERS = {"lynx", "w3m", "links", "links2", "elinks", "www-browser", "browsh"}


def _has_desktop() -> bool:
    """Whether webbrowser can show a page without taking over ATLAS's terminal.

    On Linux without a display, webbrowser only finds console browsers (lynx, w3m), which run in the server's
    terminal and block until they exit. A BROWSER variable naming a GUI handler (e.g. wslview on WSL) still works.
    """
    if IS_WIN or IS_MAC or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    for entry in os.environ.get("BROWSER", "").split(os.pathsep):
        words = entry.split()
        if words and os.path.basename(words[0]).lower() not in _CONSOLE_BROWSERS:
            return True
    return False


async def _open_url(url: str) -> bool:
    """Open an https link in the user's default browser; False if there's no browser to show it in."""
    if not url.startswith("https://") or not _has_desktop():
        return False
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def settle(ok: bool) -> None:
        if not done.done():
            done.set_result(ok)

    def go():
        try:
            ok = bool(webbrowser.open(url))
        except Exception:
            ok = False
        try:
            loop.call_soon_threadsafe(settle, ok)
        except RuntimeError:  # the loop finished while the browser was starting
            pass

    # A daemon thread rather than asyncio.to_thread: a browser command that never returns must not hold an
    # executor worker or stall shutdown.
    threading.Thread(target=go, name="atlas-open-url", daemon=True).start()
    try:
        return await asyncio.wait_for(done, OPEN_TIMEOUT)
    except asyncio.TimeoutError:
        return False


@tool(
    "get_directions",
    "Directions to a place: opens Google Maps directions (origin, destination and travel mode pre-filled) in the "
    "user's browser and estimates distance and travel time from OpenStreetMap routing for driving, walking or "
    "cycling (typical speeds, no live traffic; transit times are only on the Maps page). Use for 'how do I get "
    "to…', 'how far is…', 'how long to drive/walk to…'. origin defaults to the saved home address, else the "
    "home city, else the user's current location; 'home', 'work' and 'here' are understood. Set open=false to "
    "answer how far/how long without opening Maps. Check estimate.to: if it's the wrong place, ask for a fuller address.",
    {
        "destination": {"type": "string", "description": "Address, place name or 'lat,lon', e.g. 'Austin-Bergstrom Airport'."},
        "origin": {"type": "string", "description": "Starting point. Omit for the default (home)."},
        "mode": {"type": "string", "enum": ["driving", "walking", "transit", "bicycling"], "description": "Default driving."},
        "open": {"type": "boolean", "description": "Open Google Maps in the browser (default true)."},
    },
    ["destination"],
    label="Plotting route",
)
async def get_directions(ctx, destination: str, origin: str = "", mode: str = "driving", open: bool = True):
    home_city = (ctx.settings.home_city or "").strip()
    dest, _ = _resolve_place(destination, home_city)
    if not dest:
        raise ToolError("Where to? Give a destination address or place name.")
    travel = _mode(_text(mode) or None)
    if _text(origin):
        start, source = _resolve_place(origin, home_city)
    elif _setting("HOME_ADDRESS"):
        start, source = _setting("HOME_ADDRESS"), "home address"
    elif home_city:
        start, source = home_city, "home city (approximate: set a home address in Settings for door-to-door)"
    else:
        start, source = None, "current location"

    url = directions_url(dest, start, travel)
    result: dict = {
        "status": "",
        "destination": dest,
        "origin": start or "your current location",
        "origin_source": source,
        "mode": travel,
        "url": url,
    }
    if _flag(open):
        result["opened"] = await _open_url(url)

    estimate = None
    if travel == "transit":
        result["estimate_note"] = "Transit routes, times and schedules are on the Google Maps page."
    elif start is None:
        result["estimate_note"] = (
            "No starting point to estimate from: give an origin, or add a home address in Settings. "
            "The Maps link starts from the user's current location."
        )
    else:
        try:
            estimate = await asyncio.wait_for(_estimate(ctx, start, dest, travel), timeout=ESTIMATE_TIMEOUT)
        except Exception as exc:  # the link is still useful without an estimate
            if isinstance(exc, ToolError):
                reason = str(exc)
            elif isinstance(exc, asyncio.TimeoutError):
                reason = f"the map services took longer than {ESTIMATE_TIMEOUT:g} s"
            else:
                reason = f"{exc.__class__.__name__}: {exc}".rstrip(": ")
            result["estimate_error"] = f"No distance/time estimate ({clip(reason, 160)}). The Maps link still works."
    result["estimate"] = estimate

    shown = "Opened Google Maps directions" if result.get("opened") else "Google Maps directions link ready"
    if estimate:
        traffic = ", without live traffic" if travel == "driving" else ""
        about = "Roughly" if estimate.get("approximate") else "About"
        result["status"] = f"{about} {estimate['duration']} ({estimate['distance']}) {_MODE_WORDS[travel]} to {dest}{traffic}. {shown}."
    else:
        result["status"] = f"{shown} to {dest} ({travel})."
    return result


@tool(
    "find_places",
    "Find real places by type or name near a location with OpenStreetMap search, e.g. query 'pharmacy' near "
    "'Austin, Texas', 'sushi' near 'Brooklyn', 'gas station', 'Walgreens', 'coffee'. Returns name, address, "
    "coordinates, type and a Google Maps link for each. It has NO ratings, reviews, prices or opening hours: for "
    "those use web_search, or browser_open a result's maps_url. near defaults to the user's home city. Use "
    "get_directions for travel time and reservation_search to book a restaurant table.",
    {
        "query": {"type": "string", "description": "Kind of place or a name, e.g. 'pharmacy', 'thai restaurant', 'Costco'."},
        "near": {"type": "string", "description": "City, neighbourhood or address. Default: home city."},
        "limit": {"type": "integer", "description": "1-10 results (default 5)."},
    },
    ["query"],
    label="Searching the map",
    timeout=40,
)
async def find_places(ctx, query: str, near: str = "", limit: int = 5):
    query = _NEAR_ME.sub("", _text(query))  # "near me" means the default area
    if not query:
        raise ToolError("Say what kind of place to look for.")
    try:
        n = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        n = 5
    home_city = (ctx.settings.home_city or "").strip()
    default_area = home_city or _setting("HOME_ADDRESS")
    area, _ = _resolve_place(near, home_city) if _text(near) else (None, "")
    if area is None and not _has_location(query):
        area = default_area or None

    search = f"{query} near {area}" if area else query
    items = await nominatim_search(ctx.http, search, n, details=True)
    if not items and area:
        search = f"{query}, {area}"
        items = await nominatim_search(ctx.http, search, n, details=True)
    places = [_place_entry(i) for i in items[:n]]
    result: dict = {
        "query": search,
        "results": places,
        "map_search_url": maps_search_url(search),
        "note": f"From OpenStreetMap ({OSM_CREDIT}): no ratings, reviews, prices or opening hours. For those use "
        "web_search or browser_open a maps_url.",
    }
    if not places:
        result["tip"] = "Nothing found on OpenStreetMap: try a broader term, or web_search / open map_search_url."
    if not area and not _has_location(query):
        result["location_tip"] = "No location given or saved (home city), so results may be anywhere: ask where."
    return result


# --------------------------------------------------------------------------- rides
def uber_url(dropoff: str, pickup: str | None = None) -> str:
    """Uber universal deep link (developer.uber.com, Ride Requests deep links)."""
    parts = ["action=setPickup"]
    parts.append(f"pickup[formatted_address]={quote(pickup, safe='')}" if pickup else "pickup=my_location")
    parts.append(f"dropoff[formatted_address]={quote(dropoff, safe='')}")
    return "https://m.uber.com/ul/?" + "&".join(parts)


LYFT_URL = "https://www.lyft.com/"
_RIDE_ALIASES = {"uber": "uber", "lyft": "lyft", "uberx": "uber", "uber x": "uber"}


@tool(
    "book_ride",
    "Set up an Uber or Lyft ride: opens Uber in the user's browser with the drop-off (and pickup, if given) "
    "pre-filled, or the Lyft website. ATLAS cannot request, pay for or confirm a ride: the user picks the ride "
    "type, checks the fare and confirms in Uber/Lyft, so never say a ride is booked. pickup defaults to the "
    "user's current location; 'home'/'work' use the saved addresses. For travel time use get_directions.",
    {
        "destination": {"type": "string", "description": "Drop-off address or place name."},
        "pickup": {"type": "string", "description": "Pickup address. Omit for the current location."},
        "service": {"type": "string", "enum": ["uber", "lyft"], "description": "Default uber."},
    },
    ["destination"],
    label="Hailing a ride",
)
async def book_ride(ctx, destination: str, pickup: str = "", service: str = "uber"):
    svc = _RIDE_ALIASES.get((_text(service) or "uber").lower())
    if not svc:
        raise ToolError("I can set up rides with Uber or Lyft.")
    home_city = (ctx.settings.home_city or "").strip()
    dest, _ = _resolve_place(destination, home_city, allow_city=False)
    if not dest:
        raise ToolError("Where to? Give the drop-off address or place name.")
    start, _ = _resolve_place(pickup, home_city, allow_city=False) if _text(pickup) else (None, "")

    if svc == "uber":
        url = uber_url(dest, start)
        opened = await _open_url(url)
        shown = "Opened Uber" if opened else "Couldn't open the browser; Uber link ready"
        return {
            "status": f"{shown} with drop-off {dest}. The user chooses the ride and confirms it in Uber.",
            "service": "uber",
            "url": url,
            "opened": opened,
            "pickup": start or "current location (Uber locates the user; check the pickup pin)",
            "dropoff": dest,
            "note": "ATLAS can't request, pay for or confirm rides. Fares, ride types and arrival times appear in "
            "Uber, which may ask the user to sign in first.",
        }
    opened = await _open_url(LYFT_URL)
    shown = "Opened lyft.com" if opened else "Couldn't open the browser; the link is lyft.com"
    return {
        "status": f"{shown}. ATLAS has no Lyft link that pre-fills the trip, so the user enters pickup and "
        f"drop-off ({dest}) there or in the Lyft phone app, then confirms the ride.",
        "service": "lyft",
        "url": LYFT_URL,
        "opened": opened,
        "pickup": start or "current location",
        "dropoff": dest,
        "note": "ATLAS can't request, pay for or confirm rides. Offer Uber instead if the user wants the trip pre-filled.",
    }


# --------------------------------------------------------------------------- food delivery
FOOD_SERVICES = {
    "doordash": ("DoorDash", "https://www.doordash.com/"),
    "ubereats": ("Uber Eats", "https://www.ubereats.com/"),
    "grubhub": ("Grubhub", "https://www.grubhub.com/"),
}
_FOOD_ALIASES = {"doordash": "doordash", "door dash": "doordash", "ubereats": "ubereats", "uber eats": "ubereats",
                 "uber_eats": "ubereats", "uber": "ubereats", "grubhub": "grubhub", "grub hub": "grubhub"}


def food_search_url(service: str, query: str) -> str:
    q = " ".join(_text(query).replace("/", " ").split())
    if not re.search(r"\w", q):  # nothing searchable (and no bare '..' path segment for DoorDash)
        return FOOD_SERVICES[service][1]
    if service == "doordash":
        return f"https://www.doordash.com/search/store/{quote(q, safe='')}/"
    if service == "ubereats":
        return "https://www.ubereats.com/search?" + urlencode({"q": q})
    return "https://www.grubhub.com/search?" + urlencode({"queryText": q})


@tool(
    "order_food",
    "Start a food delivery order: opens DoorDash, Uber Eats or Grubhub search results for a dish, cuisine or "
    "restaurant (e.g. 'pad thai', 'Chipotle', 'pizza'). The delivery address, cart and payment are handled on the "
    "site. To place the order for the user, call with open=false, then browser_open the returned url (ATLAS's "
    "signed-in browser) and pick the restaurant and items; the final 'Place order' click asks the user to approve. "
    "Never say food is ordered until a checkout was approved and confirmed. For table bookings use reservation_search.",
    {
        "query": {"type": "string", "description": "Dish, cuisine or restaurant name."},
        "service": {"type": "string", "enum": ["doordash", "ubereats", "grubhub"], "description": "Default doordash."},
        "open": {"type": "boolean", "description": "Open the page in the user's browser (default true)."},
    },
    ["query"],
    label="Opening food delivery",
)
async def order_food(ctx, query: str, service: str = "doordash", open: bool = True):
    svc = _FOOD_ALIASES.get((_text(service) or "doordash").lower())
    if not svc:
        raise ToolError("I can open DoorDash, Uber Eats or Grubhub.")
    name = FOOD_SERVICES[svc][0]
    q = _text(query)
    url = food_search_url(svc, q)
    what = f"{name} search for '{q}'" if url != FOOD_SERVICES[svc][1] else f"{name} home page"
    result = {
        "status": f"{what} ready",
        "service": svc,
        "query": q,
        "url": url,
        "next_steps": f"Choosing a restaurant, the cart and checkout happen on {name}. To finish it for the user, "
        "browser_open this url and continue with browser_click/browser_fill; the final order button needs the "
        "user's approval. Never type card numbers or passwords: ask the user to do that in the window.",
    }
    if _flag(open):
        result["opened"] = await _open_url(url)
        result["status"] = f"Opened {what}" if result["opened"] else f"Couldn't open the browser; {what} link ready"
    return result


# --------------------------------------------------------------------------- package tracking
CARRIER_NAMES = {"ups": "UPS", "usps": "USPS", "fedex": "FedEx", "dhl": "DHL Express", "amazon": "Amazon"}
_CARRIER_ALIASES = {"ups": "ups", "usps": "usps", "us postal service": "usps", "postal service": "usps", "fedex": "fedex",
                    "fed ex": "fedex", "dhl": "dhl", "dhl express": "dhl", "amazon": "amazon", "amazon logistics": "amazon"}
AMAZON_ORDERS_URL = "https://www.amazon.com/gp/your-account/order-history"
PACKAGES_KEEP_DAYS = 60
PACKAGES_MAX = 30
_PKG_LOCK = threading.Lock()


def normalize_tracking(raw: str) -> str:
    return re.sub(r"[\s\-.]", "", raw or "").upper()


def tracking_url(carrier: str, number: str) -> str:
    n = quote(number, safe="")
    if carrier == "ups":
        return f"https://www.ups.com/track?tracknum={n}"
    if carrier == "usps":
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={n}"
    if carrier == "fedex":
        return f"https://www.fedex.com/fedextrack/?trknbr={n}"
    if carrier == "dhl":
        return f"https://www.dhl.com/us-en/home/tracking/tracking-express.html?submit=1&tracking-id={n}"
    if carrier == "amazon":
        return AMAZON_ORDERS_URL  # Amazon's tracker needs the order, not just the number
    # Unknown format, or a carrier without a link here (OnTrac, Canada Post...): search with its name.
    other = "" if carrier in (None, "", "unknown", "search") else str(carrier)
    return "https://www.google.com/search?" + urlencode({"q": " ".join(x for x in (other, number, "tracking") if x)})


def detect_carrier(number: str) -> dict:
    """Best-guess carrier from the number format.

    Formats overlap: 20-22 digit numbers are used by USPS and by FedEx (Ground Economy parcels are delivered by
    USPS with USPS-style numbers), and other carriers also use 10- or 12-digit numbers. So the result carries a
    confidence and the plausible alternatives.
    """
    n = number

    def guess(carrier, confidence, alternatives=(), note="", tracking=None):
        return {"carrier": carrier, "confidence": confidence, "number": tracking or n,
                "alternatives": list(alternatives), "note": note}

    if re.fullmatch(r"1Z[0-9A-Z]{16}", n):
        return guess("ups", "high")
    if re.fullmatch(r"TBA\d{9,15}", n):
        return guess("amazon", "high", note="Amazon Logistics number: its status is on the order in the user's Amazon account.")
    m = re.fullmatch(r"[A-Z]{2}\d{9}([A-Z]{2})", n)
    if m:
        if m[1] == "US":
            return guess("usps", "high")
        return guess("usps", "medium", ["search"], f"International postal item from country code {m[1]}: the sender's "
                     "postal service tracks it there, and USPS once it reaches the US.")
    if not n.isdigit():
        return guess(None, "none", ["search"], "Unrecognised format (not UPS, USPS, FedEx, DHL Express or Amazon).")
    length = len(n)
    if length in (30, 34) and n.startswith("420"):
        return guess("usps", "high", note="Scanned USPS barcode: dropped the 420+ZIP routing prefix.", tracking=n[-22:])
    if length == 22:
        if n.startswith("96"):
            return guess("fedex", "medium", ["usps"], "22-digit numbers starting 96 are usually FedEx Ground.")
        if n[:2] in ("91", "92", "93", "94", "95"):
            return guess("usps", "medium", ["fedex"], "USPS-style barcode; FedEx Ground Economy and other shippers "
                         "also hand parcels to USPS with these numbers.")
        return guess("usps", "low", ["fedex"], "22 digits: could be USPS or FedEx.")
    if length in (20, 21):
        return guess("usps", "medium", ["fedex"], f"{length} digits: usually USPS, sometimes FedEx.")
    if length in (12, 15):
        return guess("fedex", "high", note=f"{length}-digit numbers are FedEx {'Express' if length == 12 else 'Ground'}.")
    if length == 10:
        return guess("dhl", "medium", ["search"], "10-digit numbers are usually DHL Express waybills; other carriers use 10 digits too.")
    return guess(None, "none", ["search"], f"A {length}-digit number doesn't match UPS, USPS, FedEx, DHL or Amazon formats.")


def _packages_path():
    return DATA_DIR / "packages.json"


def _load_packages() -> list[dict]:
    try:
        data = json.loads(_packages_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        {**p, "carrier": str(p.get("carrier") or "unknown"), "label": str(p.get("label") or "")}
        for p in data
        if isinstance(p, dict) and isinstance(p.get("number"), str) and p["number"]
    ]


def _save_packages(items: list[dict]) -> None:
    path = _packages_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _checked_at(p: dict) -> datetime:
    try:
        dt = datetime.fromisoformat(str(p.get("last_checked") or p.get("added")))
        return dt.astimezone() if dt.tzinfo is None else dt
    except (ValueError, OverflowError, OSError):  # Windows can't localise dates near year 1 or 9999
        return datetime.min.replace(tzinfo=timezone.utc)


def _fresh(items: list[dict]) -> list[dict]:
    """Drop packages not looked at for PACKAGES_KEEP_DAYS and keep the most recent PACKAGES_MAX."""
    cutoff = datetime.now().astimezone() - timedelta(days=PACKAGES_KEEP_DAYS)
    items = sorted((p for p in items if _checked_at(p) >= cutoff), key=_checked_at, reverse=True)
    return items[:PACKAGES_MAX]


def _remember_package(number: str, carrier: str | None, label: str) -> dict:
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    with _PKG_LOCK:
        items = _load_packages()
        old = next((p for p in items if p["number"] == number), None)
        entry = {
            "number": number,
            "carrier": carrier or "unknown",
            "label": label or (old or {}).get("label", ""),
            "added": (old or {}).get("added", now),
            "last_checked": now,
        }
        items = [p for p in items if p["number"] != number] + [entry]
        _save_packages(_fresh(items))
    return entry


@tool(
    "track_package",
    "Track a parcel: detects the carrier from the tracking number (UPS, USPS, FedEx, DHL Express, Amazon) and "
    "opens the carrier's official tracking page (for another carrier the user names, e.g. OnTrac or Canada Post, "
    "pass its name as carrier to get a web search), and saves the number (with an optional label like 'new "
    "headphones') so the user can ask about their packages later. Omit tracking_number to list saved packages; "
    "remove=true deletes one. The result is the link, not the delivery status: to read the status, browser_open "
    "the url (carrier pages need JavaScript; fetch_url may work for some) and summarise it.",
    {
        "tracking_number": {"type": "string", "description": "As printed; spaces and dashes are fine. Omit to list saved packages."},
        "carrier": {"type": "string", "description": "auto (default: detect from the number), ups, usps, fedex, dhl, "
                    "amazon, or another carrier's name (e.g. 'OnTrac', 'Canada Post'), which is searched on the web."},
        "label": {"type": "string", "description": "What's in it / who it's from, to remember it by."},
        "open": {"type": "boolean", "description": "Open the tracking page in the browser (default true)."},
        "remove": {"type": "boolean", "description": "Delete this number from the saved packages instead."},
    },
    [],
    label="Tracking package",
)
async def track_package(ctx, tracking_number: str = "", carrier: str = "auto", label: str = "", open: bool = True,
                        remove: bool = False):
    if not _text(tracking_number):
        with _PKG_LOCK:
            items = _fresh(_load_packages())
        packages = [{**p, "url": tracking_url(p["carrier"], p["number"])} for p in items]
        return {
            "status": f"{len(packages)} saved package{'s' if len(packages) != 1 else ''}",
            "packages": packages,
            "tip": "Give a tracking number to open its page; browser_open a url to read the latest status.",
        }
    number = normalize_tracking(_text(tracking_number))
    if not re.fullmatch(r"[A-Z0-9]{6,40}", number):
        raise ToolError("That doesn't look like a tracking number (6-40 letters and digits).")
    if _flag(remove, False):
        forms = {number, detect_carrier(number)["number"]}  # also the 22-digit form of a scanned USPS barcode
        with _PKG_LOCK:
            items = _load_packages()
            keep = [p for p in items if p["number"] not in forms]
            try:
                _save_packages(keep)
            except OSError as exc:
                raise ToolError(f"Couldn't update the saved packages file ({exc.strerror or exc}).") from None
        return {"status": "removed" if len(keep) != len(items) else "not saved", "removed": len(items) - len(keep)}

    det = detect_carrier(number)
    wanted = _text(carrier)
    chosen = _CARRIER_ALIASES.get(wanted.lower())
    # A carrier the user named that has no link here (OnTrac, Canada Post...) wins over a format guess.
    other = "" if chosen or wanted.lower() in ("", "auto", "unknown", "any") else re.sub(r"[^\w .&'-]", "", wanted)[:40].strip()
    note = det["note"]
    detected = det["carrier"]
    if chosen:
        if detected and chosen != detected:
            note = f"Using {CARRIER_NAMES[chosen]} as asked, though the format looks like {CARRIER_NAMES[detected]}."
        elif not detected:
            note = f"Using {CARRIER_NAMES[chosen]} as asked; the number doesn't match its usual format."
        det = {**det, "carrier": chosen, "confidence": "given", "alternatives": [c for c in det["alternatives"] if c != chosen]}
    elif other:
        note = f"No direct tracking link for {other} here, so the link is a web search for the number."
        if detected:
            note += f" The format also fits {CARRIER_NAMES[detected]}: see alternatives."
        alts = ([detected] if detected else []) + [c for c in det["alternatives"] if c not in ("search", detected)]
        det = {**det, "carrier": other, "confidence": "given", "alternatives": alts}
    url = tracking_url(det["carrier"], det["number"])
    alternatives = [
        {"carrier": CARRIER_NAMES.get(c, "web search"), "url": tracking_url(c if c in CARRIER_NAMES else None, det["number"])}
        for c in det["alternatives"]
        if c != det["carrier"] and not (c == "search" and det["carrier"] is None)  # the main url is the search then
    ]
    save_error = ""
    try:
        entry = _remember_package(det["number"], det["carrier"], clip(_text(label), 80))
    except OSError as exc:  # e.g. the file is locked or read-only: the link is still what was asked for
        entry, save_error = {"label": clip(_text(label), 80)}, f"Couldn't save it to the package list ({exc.strerror or exc})."
    name = CARRIER_NAMES.get(det["carrier"]) or (f"{other} (web search)" if other else "a web search")
    result = {
        "status": f"{name} tracking for {det['number']}",
        "tracking_number": det["number"],
        "carrier": det["carrier"] or "unknown",
        "carrier_name": name,
        "confidence": det["confidence"],
        "url": url,
        "alternatives": alternatives,
        "label": entry["label"],
        "tip": "This is the tracking link, not the status: browser_open the url to read the latest scan and delivery date.",
    }
    if note:
        result["note"] = note
    if save_error:
        result["save_error"] = save_error
    if det["carrier"] == "amazon":
        result["tip"] = "The link is Amazon's order history: the user (or browser_open) finds the order and clicks 'Track package'."
    if _flag(open):
        result["opened"] = await _open_url(url)
        if result["opened"]:
            result["status"] = f"Opened {name} tracking for {det['number']}"
    return result


# --------------------------------------------------------------------------- currency
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LTC", "BNB", "USDT", "USDC"}
_CURRENCY_NAMES = {
    "$": "USD", "us$": "USD", "dollar": "USD", "us dollar": "USD", "buck": "USD", "€": "EUR", "euro": "EUR",
    "£": "GBP", "pound": "GBP", "pound sterling": "GBP", "sterling": "GBP", "british pound": "GBP", "¥": "JPY",
    "yen": "JPY", "japanese yen": "JPY", "yuan": "CNY", "renminbi": "CNY", "rmb": "CNY", "₹": "INR", "rupee": "INR",
    "indian rupee": "INR", "won": "KRW", "₩": "KRW", "franc": "CHF", "swiss franc": "CHF", "canadian dollar": "CAD",
    "australian dollar": "AUD", "mexican peso": "MXN", "peso": "MXN", "real": "BRL", "reais": "BRL", "ruble": "RUB",
    "rouble": "RUB", "lira": "TRY", "rand": "ZAR", "krona": "SEK", "krone": "NOK", "zloty": "PLN", "shekel": "ILS",
    "dirham": "AED", "riyal": "SAR", "baht": "THB", "dong": "VND", "rupiah": "IDR", "ringgit": "MYR",
    "hong kong dollar": "HKD", "singapore dollar": "SGD", "new zealand dollar": "NZD", "bitcoin": "BTC", "ether": "ETH",
    "ethereum": "ETH", "solana": "SOL", "dogecoin": "DOGE",
}


def currency_code(value: str) -> str:
    raw = " ".join(str(value or "").split())
    low = raw.lower()
    # Names first: 'yen' or 'won' would otherwise pass as (non-existent) codes YEN / WON.
    for key in (low, low[:-1] if low.endswith("s") else low, low.replace("s ", " ")):
        if key in _CURRENCY_NAMES:
            return _CURRENCY_NAMES[key]
    if re.fullmatch(r"[A-Za-z]{3}", raw) or raw.upper() in CRYPTO:
        return raw.upper()
    raise ToolError(f"Unknown currency '{value}'. Use a 3-letter code such as USD, EUR, GBP or JPY.")


def _ticker(base: str, quote_: str) -> str | None:
    if base in CRYPTO:
        return None if quote_ in CRYPTO else f"{base}-{quote_}"
    if quote_ in CRYPTO:
        return None
    return f"{base}{quote_}=X"


def _rounding_error(price: float) -> float:
    # fetch_quote rounds prices to 2 decimals at or above 1 and to 4 below, so the same rate can be far more
    # precise quoted one way round (USDEUR 0.9231) than the other (EURUSD 1.08).
    return (0.005 if price >= 1 else 0.00005) / price


class _RatesUnavailable(ToolError):
    """Yahoo couldn't be reached (as opposed to the pair not existing)."""


def _is_outage(exc: BaseException) -> bool:
    # fetch_quote raises ToolError("Unknown ticker ...") / ("No data for ...") when a pair doesn't exist, and
    # ToolError("Market data unavailable ...") when Yahoo can't be reached.
    return not isinstance(exc, ToolError) or str(exc).startswith("Market data unavailable")


async def _pair_rate(http, base: str, quote_: str) -> dict:
    options = [(sym, inv) for sym, inv in ((_ticker(base, quote_), False), (_ticker(quote_, base), True)) if sym]
    if not options:
        raise ToolError(f"No direct {base}/{quote_} market.")
    quotes = await asyncio.gather(*(fetch_quote(http, sym) for sym, _ in options), return_exceptions=True)
    candidates = []
    outages = []
    for (sym, inverted), q in zip(options, quotes):
        if isinstance(q, BaseException):
            if isinstance(q, asyncio.CancelledError):
                raise q
            if _is_outage(q):
                outages.append(q)
            continue
        if not isinstance(q, dict):
            continue
        try:
            price = float(q.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and math.isfinite(price):
            candidates.append((_rounding_error(price), 1 / price if inverted else price, sym, q.get("as_of")))
    if not candidates:
        if outages:
            raise _RatesUnavailable(
                f"Couldn't reach Yahoo Finance for the {base}/{quote_} rate right now ({clip(str(outages[0]), 120)}). "
                "Try again shortly."
            )
        raise ToolError(f"No {base}/{quote_} rate available.")
    _, rate, sym, as_of = min(candidates)
    return {"rate": rate, "symbols": [sym], "as_of": as_of}


def _round_sig(value: float, digits: int = 6) -> float:
    return float(f"{value:.{digits}g}")


def _fmt_amount(value: float, code: str) -> str:
    if code in CRYPTO:
        return f"{value:,.8f}".rstrip("0").rstrip(".")
    return f"{value:,.0f}" if value == int(value) and abs(value) >= 1 else f"{value:,.2f}"


def parse_amount(amount) -> float:
    """A number, or text like '15,000', '12,50' (decimal comma), '$250' or '1 000'."""
    if amount is None or amount == "":
        return 1.0
    if isinstance(amount, bool) or isinstance(amount, (dict, list)):
        raise ToolError("amount must be a number.")
    text = re.sub(r"[\s_\u00a0\u202f]", "", str(amount)).strip("$€£¥₹₩")
    if re.fullmatch(r"-?\d+,\d{1,2}", text):  # 12,50 -> 12.50 (a thousands comma always has 3 digits after it)
        text = text.replace(",", ".")
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        raise ToolError("amount must be a number.") from None
    if not math.isfinite(value):
        raise ToolError("amount must be a finite number.")
    return value


@tool(
    "convert_currency",
    "Convert money between currencies at the current market rate (Yahoo Finance FX, mid-market, may be a few "
    "minutes delayed), e.g. 250 EUR to USD, 10000 JPY to GBP, 0.5 BTC to EUR. Use ISO codes (USD, EUR, GBP, JPY, "
    "INR, CAD, MXN...); common names ('euros') and symbols ('£') also work. Banks and cards charge a spread on top; "
    "for historical rates use web_search.",
    {
        "amount": {"type": "number", "description": "Amount to convert (default 1)."},
        "from_currency": {"type": "string", "description": "e.g. 'EUR'"},
        "to_currency": {"type": "string", "description": "e.g. 'USD'"},
    },
    ["from_currency", "to_currency"],
    label="Converting currency",
)
async def convert_currency(ctx, from_currency: str, to_currency: str, amount: float = 1):
    value = parse_amount(amount)
    src, dst = currency_code(from_currency), currency_code(to_currency)
    if src == dst:
        info = {"rate": 1.0, "symbols": [], "as_of": None}
    else:
        try:
            info = await _pair_rate(ctx.http, src, dst)
        except _RatesUnavailable:
            raise
        except ToolError:
            if "USD" in (src, dst):
                raise ToolError(f"Couldn't get a {src} to {dst} rate. Check the currency codes.")
            try:
                leg1, leg2 = await asyncio.gather(_pair_rate(ctx.http, src, "USD"), _pair_rate(ctx.http, "USD", dst))
            except _RatesUnavailable:
                raise
            except ToolError:
                raise ToolError(f"Couldn't get a {src} to {dst} rate. Check the currency codes.")
            info = {"rate": leg1["rate"] * leg2["rate"], "symbols": leg1["symbols"] + leg2["symbols"],
                    "as_of": leg1["as_of"] or leg2["as_of"], "via": "USD"}
    converted = value * info["rate"]
    out = {
        "result": f"{_fmt_amount(value, src)} {src} = {_fmt_amount(round(converted, 8 if dst in CRYPTO else 2), dst)} {dst}",
        "amount": value,
        "from": src,
        "to": dst,
        "converted": round(converted, 8 if dst in CRYPTO else 2),
        "rate": _round_sig(info["rate"]),
        "inverse_rate": _round_sig(1 / info["rate"]) if info["rate"] else None,
        "as_of": info["as_of"],
        "source": "Yahoo Finance market rate (mid-market; banks and card networks add a spread)" if info["symbols"] else "same currency",
    }
    if info.get("via"):
        out["via"] = info["via"]
    return out
