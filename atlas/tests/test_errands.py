"""Errand tools (directions, places, rides, food, parcels, currency), fully offline via httpx.MockTransport."""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from app.tools import TOOLS, ToolError, call_tool, errands
from app.tools.errands import (
    RateLimiter,
    currency_code,
    detect_carrier,
    directions_url,
    food_search_url,
    normalize_tracking,
    tracking_url,
    uber_url,
)
from conftest import Router, json_response, make_ctx, mock_http, no_network, run

REAL_HAS_DESKTOP = errands._has_desktop  # captured before the autouse fixture fakes it
ERRAND_TOOLS = ["get_directions", "find_places", "book_ride", "order_food", "track_package", "convert_currency"]


@pytest.fixture(autouse=True)
def errands_env(monkeypatch):
    """No waiting on the 1 req/s limiters, empty caches, no saved addresses, and a fake browser on a desktop."""
    monkeypatch.setattr(errands, "_NOMINATIM_LIMIT", RateLimiter(0))
    monkeypatch.setattr(errands, "_OSRM_LIMIT", RateLimiter(0))
    errands._GEO_CACHE.clear()
    errands._ROUTE_CACHE.clear()
    for key in ("HOME_ADDRESS", "WORK_ADDRESS", "HOME_CITY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(errands, "_has_desktop", lambda: True)
    opened: list[str] = []
    monkeypatch.setattr(errands.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    yield opened
    errands._GEO_CACHE.clear()
    errands._ROUTE_CACHE.clear()


@pytest.fixture
def opened(errands_env):
    return errands_env


def call(name, handler=no_network, settings=None, **args):
    async def go():
        async with mock_http(handler) as http:
            return await call_tool(name, args, make_ctx(http=http, **(settings or {})))

    return json.loads(run(go()))


def set_env(isolated, **values):
    isolated["env_path"].write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def qs(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ------------------------------------------------------------------ fixtures for the map services
AUSTIN = {"lat": "30.2672", "lon": "-97.7431", "display_name": "Austin, Travis County, Texas, United States", "name": "Austin"}
AIRPORT = {"lat": "30.1975", "lon": "-97.6664", "display_name": "Austin-Bergstrom International Airport, Austin, Texas, United States",
           "name": "Austin-Bergstrom International Airport"}


def nominatim(places_by_query):
    def handler(request):
        q = request.url.params["q"].lower()
        for key, result in places_by_query.items():
            if key in q:
                return json_response(result if isinstance(result, list) else [result])
        return json_response([])

    return handler


def osrm(distance=20000.0, duration=1100.0, code="Ok"):
    body = {"code": code, "routes": [{"distance": distance, "duration": duration}] if code == "Ok" else [], "waypoints": []}
    return json_response(body, 200 if code == "Ok" else 400)


# ------------------------------------------------------------------ registration
def test_tools_registered_with_valid_schemas():
    for name in ERRAND_TOOLS:
        t = TOOLS[name]
        params = t.parameters
        assert set(params["required"]) <= set(params["properties"]), name
        assert t.description and t.label and t.gate is None and t.preview is None
        json.dumps(t.schema())
    assert TOOLS["get_directions"].parameters["properties"]["mode"]["enum"] == ["driving", "walking", "transit", "bicycling"]


# ------------------------------------------------------------------ rate limiter
def test_rate_limiter_spaces_sequential_calls():
    now = [100.0]
    slept = []

    async def fake_sleep(d):
        slept.append(d)
        now[0] += d

    lim = RateLimiter(1.0, clock=lambda: now[0], sleep=fake_sleep)

    async def go():
        await lim.wait()  # first call is free
        await lim.wait()  # must wait a full second
        now[0] += 5  # idle long enough: no wait
        await lim.wait()
        now[0] += 0.25
        await lim.wait()  # 0.75 s left of the interval

    run(go())
    assert slept == [pytest.approx(1.0), pytest.approx(0.75)]


def test_rate_limiter_queues_concurrent_callers_and_caps_wait():
    lim = RateLimiter(1.0, max_wait=2.5, clock=lambda: 50.0)
    assert [lim.reserve() for _ in range(3)] == [0.0, 1.0, 2.0]
    with pytest.raises(ToolError, match="Too many map lookups"):
        lim.reserve()  # would have to wait 3 s


def test_rate_limiter_really_waits_between_nominatim_requests(monkeypatch):
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    clock = [10.0]
    monkeypatch.setattr(errands, "_NOMINATIM_LIMIT", RateLimiter(1.0, clock=lambda: clock[0], sleep=fake_sleep))
    router = Router({"nominatim.openstreetmap.org": json_response([AUSTIN])})

    async def go():
        async with mock_http(router) as http:
            await errands.nominatim_search(http, "Austin")
            await errands.nominatim_search(http, "Dallas")
            await errands.nominatim_search(http, "austin")  # cached: no request, no slot used

    run(go())
    assert len(router.requests) == 2 and slept == [pytest.approx(1.0)]


# ------------------------------------------------------------------ Nominatim / geocoding
def test_nominatim_request_shape_parsing_and_cache():
    router = Router({"nominatim.openstreetmap.org/search": json_response([AUSTIN, {"lat": None, "lon": None}])})

    async def go():
        async with mock_http(router) as http:
            first = await errands.geocode(http, "  Austin,   Texas ")
            again = await errands.geocode(http, "austin, texas")
            coords = await errands.geocode(http, "30.5, -97.25")  # parsed locally
            return first, again, coords

    first, again, coords = run(go())
    assert first == {"lat": 30.2672, "lon": -97.7431, "name": "Austin, Travis County, Texas, United States"}
    assert again == first and coords == {"lat": 30.5, "lon": -97.25, "name": "30.5,-97.25"}
    assert len(router.requests) == 1
    req = router.requests[0]
    assert req.url.params["format"] == "jsonv2" and req.url.params["q"] == "Austin, Texas"
    assert req.url.params["limit"] == "1"
    assert req.headers["user-agent"] == "ATLAS-assistant/1.0 (personal assistant)"


def test_nominatim_errors():
    async def go(handler, place="Nowhere"):
        async with mock_http(handler) as http:
            return await errands.geocode(http, place)

    with pytest.raises(ToolError, match="Couldn't find 'Nowhere'"):
        run(go(lambda r: json_response([])))
    errands._GEO_CACHE.clear()
    with pytest.raises(ToolError, match="limiting requests"):
        run(go(lambda r: httpx.Response(429)))
    with pytest.raises(ToolError, match="unexpected reply"):
        run(go(lambda r: json_response({"error": "x"}), "Elsewhere"))
    with pytest.raises(ToolError, match="Say which place"):
        run(go(no_network, "   "))


def test_find_places_parses_results_and_uses_home_city():
    item = {
        "lat": "30.2701", "lon": "-97.7420", "category": "amenity", "type": "pharmacy", "name": "Walgreens",
        "display_name": "Walgreens, 501, Congress Avenue, Downtown, Austin, Travis County, Texas, 78701, United States",
        "address": {"house_number": "501", "road": "Congress Avenue", "city": "Austin", "county": "Travis County",
                    "state": "Texas", "postcode": "78701", "country": "United States"},
    }
    no_name = {"lat": "30.1", "lon": "-97.1", "type": "fast_food", "name": "",
               "display_name": "12 Oak Street, Austin, Texas, United States", "address": {}}
    router = Router({"nominatim.openstreetmap.org/search": json_response([item, no_name])})
    out = call("find_places", router, {"home_city": "Austin, Texas"}, query="pharmacy", limit=50)
    params = router.requests[0].url.params
    assert params["q"] == "pharmacy near Austin, Texas" and params["limit"] == "10" and params["addressdetails"] == "1"
    first, second = out["results"]
    assert first == {
        "name": "Walgreens",
        "address": "501 Congress Avenue, Austin, Texas 78701, United States",
        "type": "pharmacy",
        "lat": 30.2701,
        "lon": -97.742,
        "maps_url": first["maps_url"],
    }
    assert qs(first["maps_url"]) == {"api": "1", "query": "Walgreens, 501 Congress Avenue, Austin, Texas 78701, United States"}
    assert second["name"] == "12 Oak Street" and second["type"] == "fast food"
    assert qs(second["maps_url"])["query"] == "12 Oak Street, Austin, Texas, United States"  # no duplicated name
    assert "no ratings" in out["note"] and "OpenStreetMap" in out["note"]
    assert qs(out["map_search_url"])["query"] == "pharmacy near Austin, Texas"
    assert "location_tip" not in out


def test_find_places_query_with_location_retry_and_no_location():
    router = Router({"nominatim.openstreetmap.org/search": json_response([])})
    out = call("find_places", router, {"home_city": "Austin"}, query="sushi in Brooklyn")
    assert [r.url.params["q"] for r in router.requests] == ["sushi in Brooklyn"]  # home city not appended
    assert out["results"] == [] and "tip" in out

    router = Router({"nominatim.openstreetmap.org/search": json_response([])})
    call("find_places", router, query="tacos", near="Denver")
    assert [r.url.params["q"] for r in router.requests] == ["tacos near Denver", "tacos, Denver"]

    router = Router({"nominatim.openstreetmap.org/search": json_response([])})
    out = call("find_places", router, query="tacos")
    assert router.requests[0].url.params["q"] == "tacos" and "location_tip" in out

    router = Router({"nominatim.openstreetmap.org/search": json_response([])})
    call("find_places", router, {"home_city": "Austin"}, query="coffee near me", near="here")
    assert router.requests[0].url.params["q"] == "coffee near Austin"


def test_string_flags_and_remove_scanned_barcode(opened):
    out = call("order_food", no_network, query="tacos", open="false")
    assert "opened" not in out and opened == []
    scanned = "42078701" + "9400100000000000000000"
    assert call("track_package", tracking_number=scanned, open="no")["tracking_number"] == "9400100000000000000000"
    assert call("track_package", tracking_number=scanned, remove="true")["removed"] == 1


# ------------------------------------------------------------------ directions
def test_directions_url_encoding():
    url = directions_url("Pike Place Market, Seattle & Co", "Space Needle", "bicycling")
    assert url.startswith("https://www.google.com/maps/dir/?api=1&origin=Space+Needle&destination=")
    assert "Pike+Place+Market%2C+Seattle+%26+Co" in url
    assert qs(url) == {"api": "1", "origin": "Space Needle", "destination": "Pike Place Market, Seattle & Co", "travelmode": "bicycling"}
    assert "origin" not in qs(directions_url("Dallas"))  # Google uses the current location


def test_mode_aliases():
    assert errands._mode("Car") == "driving" and errands._mode("walk") == "walking"
    assert errands._mode("public transport") == "transit" and errands._mode("bike") == "bicycling"
    assert errands._mode(None) == "driving"
    with pytest.raises(ToolError):
        errands._mode("teleport")


def test_get_directions_driving_with_estimate(opened):
    router = Router({"router.project-osrm.org": osrm(20000.0, 1100.0)})
    router.add("nominatim.openstreetmap.org", nominatim({"airport": AIRPORT, "austin": AUSTIN}))
    out = call("get_directions", router, {"home_city": "Austin, Texas"}, destination="Austin airport")
    assert opened == [out["url"]] and out["opened"] is True
    assert qs(out["url"]) == {"api": "1", "origin": "Austin, Texas", "destination": "Austin airport", "travelmode": "driving"}
    assert out["origin_source"].startswith("home city")
    est = out["estimate"]
    assert est["duration"] == "18 min" and est["distance"] == "12.4 mi" and est["distance_km"] == 20.0
    assert est["to"].startswith("Austin-Bergstrom") and "no live traffic" in est["source"]
    assert out["status"].startswith("About 18 min (12.4 mi) by car to Austin airport, without live traffic.")
    osrm_req = router.hits("router.project-osrm.org")[0]
    assert osrm_req.url.path == "/route/v1/driving/-97.743100,30.267200;-97.666400,30.197500"
    assert osrm_req.url.params["overview"] == "false"


def test_get_directions_metric_long_trip_and_home_address(isolated):
    set_env(isolated, HOME_ADDRESS="1 Main St, Austin TX")
    router = Router({"router.project-osrm.org": osrm(312_000.0, 4 * 3600 + 7 * 60)})
    router.add("nominatim.openstreetmap.org", nominatim({"main st": AUSTIN, "dallas": AIRPORT}))
    out = call("get_directions", router, {"units": "metric", "home_city": "Austin"}, destination="Dallas", open=False)
    assert "opened" not in out and out["origin"] == "1 Main St, Austin TX" and out["origin_source"] == "home address"
    assert out["estimate"]["distance"] == "312 km" and out["estimate"]["duration"] == "4 h 7 min"
    assert "link ready" in out["status"]


def test_get_directions_walking_uses_foot_router_then_falls_back_with_sanity_check():
    router = Router({"routing.openstreetmap.de": osrm(2000.0, 1440.0)})  # 5 km/h: plausible walking
    router.add("nominatim.openstreetmap.org", nominatim({"a st": AUSTIN, "b st": AIRPORT}))
    out = call("get_directions", router, origin="A St", destination="B St", mode="walking", open=False)
    assert out["estimate"]["duration"] == "24 min" and "approximate" not in out["estimate"]
    assert router.hits("routing.openstreetmap.de")[0].url.path.startswith("/routed-foot/route/v1/driving/")
    assert out["status"].startswith("About 24 min (1.2 mi) on foot")

    # Foot server down; the demo server answers with car speeds (6 km in 6 min) -> recomputed at 5 km/h.
    errands._ROUTE_CACHE.clear()
    router = Router({"routing.openstreetmap.de": httpx.Response(502, text="Bad gateway"),
                     "router.project-osrm.org": osrm(6000.0, 360.0)})
    router.add("nominatim.openstreetmap.org", nominatim({"a st": AUSTIN, "b st": AIRPORT}))
    out = call("get_directions", router, origin="A St", destination="B St", mode="bike", open=False)
    assert router.hits("routing.openstreetmap.de")[0].url.path.startswith("/routed-bike/")
    assert router.hits("router.project-osrm.org")[0].url.path.startswith("/route/v1/cycling/")
    est = out["estimate"]
    assert est["approximate"] is True and est["duration"] == "22 min"  # 6 km at 16 km/h
    assert out["mode"] == "bicycling" and out["status"].startswith("Roughly 22 min")


@pytest.mark.parametrize(
    "routes",
    [
        {"nominatim.openstreetmap.org": httpx.ConnectError("offline")},
        {"nominatim.openstreetmap.org": json_response([])},
        {"nominatim.openstreetmap.org": httpx.Response(429)},
        {"nominatim.openstreetmap.org": json_response([AUSTIN]), "router.project-osrm.org": osrm(code="NoRoute")},
        {"nominatim.openstreetmap.org": json_response([AUSTIN]), "router.project-osrm.org": httpx.Response(500, text="<html>")},
    ],
)
def test_get_directions_estimate_failures_still_return_the_link(routes, opened):
    out = call("get_directions", Router(routes), origin="Austin", destination="Somewhere")
    assert "error" not in out and out["estimate"] is None
    assert out["estimate_error"].startswith("No distance/time estimate") and "Maps link still works" in out["estimate_error"]
    assert opened == [out["url"]] and qs(out["url"])["destination"] == "Somewhere"
    assert out["status"] == "Opened Google Maps directions to Somewhere (driving)."


def test_get_directions_estimate_timeout_is_tolerated(monkeypatch):
    monkeypatch.setattr(errands, "ESTIMATE_TIMEOUT", 0.05)

    async def slow(request):
        await asyncio.sleep(5)

    out = call("get_directions", slow, origin="Austin", destination="Dallas", open=False)
    assert out["estimate"] is None and "took longer than 0.05 s" in out["estimate_error"]


def test_get_directions_transit_and_no_origin_make_no_requests(opened):
    out = call("get_directions", no_network, {"home_city": "Austin"}, destination="MoMA", mode="transit")
    assert out["estimate"] is None and "Transit" in out["estimate_note"] and qs(out["url"])["travelmode"] == "transit"

    out = call("get_directions", no_network, destination="MoMA")  # no origin and nothing saved
    assert "origin" not in qs(out["url"]) and out["origin"] == "your current location"
    assert "No starting point" in out["estimate_note"]

    out = call("get_directions", no_network, {"home_city": "Austin"}, destination="MoMA", origin="my location")
    assert "origin" not in qs(out["url"]) and out["estimate"] is None


def test_get_directions_home_and_work_aliases(isolated):
    out = call("get_directions", no_network, destination="home")
    assert "home address" in out["error"]
    set_env(isolated, WORK_ADDRESS="200 Office Park Dr", HOME_ADDRESS="5 Elm St")
    router = Router({"router.project-osrm.org": osrm(), "nominatim.openstreetmap.org": json_response([AUSTIN])})
    out = call("get_directions", router, destination="work", origin="home", open=False)
    assert qs(out["url"])["destination"] == "200 Office Park Dr" and qs(out["url"])["origin"] == "5 Elm St"
    assert call("get_directions", no_network, destination="here")["error"].startswith("Where to?")
    assert "Travel mode" in call("get_directions", no_network, destination="x", mode="rocket")["error"]


# ------------------------------------------------------------------ rides
def test_uber_url_format_and_encoding():
    url = uber_url("JFK Terminal 4, Queens & NY")
    assert url == ("https://m.uber.com/ul/?action=setPickup&pickup=my_location"
                   "&dropoff[formatted_address]=JFK%20Terminal%204%2C%20Queens%20%26%20NY")
    url = uber_url("SFO", "1 Market St, San Francisco")
    assert "pickup[formatted_address]=1%20Market%20St%2C%20San%20Francisco" in url and "my_location" not in url
    params = parse_qs(urlparse(url).query)
    assert params["dropoff[formatted_address]"] == ["SFO"]


def test_book_ride_uber_and_lyft(isolated, opened):
    out = call("book_ride", no_network, destination="LaGuardia Airport")
    assert out["service"] == "uber" and opened == [out["url"]] and out["opened"] is True
    assert "pickup=my_location" in out["url"] and unquote(out["url"]).endswith("dropoff[formatted_address]=LaGuardia Airport")
    assert "can't request" in out["note"] and "confirms it in Uber" in out["status"]

    set_env(isolated, HOME_ADDRESS="5 Elm St, Brooklyn")
    out = call("book_ride", no_network, destination="JFK", pickup="home")
    assert "pickup[formatted_address]=5%20Elm%20St%2C%20Brooklyn" in out["url"]

    out = call("book_ride", no_network, destination="JFK", service="Lyft")
    assert out["url"] == "https://www.lyft.com/" and opened[-1] == "https://www.lyft.com/"
    assert "JFK" in out["status"] and "no Lyft link" in out["status"]

    assert "Uber or Lyft" in call("book_ride", no_network, destination="JFK", service="bolt")["error"]


def test_book_ride_home_needs_a_real_address():
    out = call("book_ride", no_network, {"home_city": "Austin"}, destination="home")
    assert "home address" in out["error"]  # a city centre is no use as a drop-off


# ------------------------------------------------------------------ food
def test_food_search_urls():
    assert food_search_url("doordash", "pad thai") == "https://www.doordash.com/search/store/pad%20thai/"
    assert food_search_url("doordash", "a/b & c") == "https://www.doordash.com/search/store/a%20b%20%26%20c/"
    assert food_search_url("ubereats", "pad thai & rolls") == "https://www.ubereats.com/search?q=pad+thai+%26+rolls"
    assert food_search_url("grubhub", "Joe's Pizza") == "https://www.grubhub.com/search?queryText=Joe%27s+Pizza"
    assert food_search_url("grubhub", "  ") == "https://www.grubhub.com/"


def test_order_food_tool(opened):
    out = call("order_food", no_network, query="sushi", service="Uber Eats")
    assert out["service"] == "ubereats" and out["url"] == "https://www.ubereats.com/search?q=sushi"
    assert opened == [out["url"]] and out["status"] == "Opened Uber Eats search for 'sushi'"
    assert "browser_open" in out["next_steps"] and "approval" in out["next_steps"]
    out = call("order_food", no_network, query="tacos", open=False)
    assert out["service"] == "doordash" and "opened" not in out and len(opened) == 1
    assert "DoorDash, Uber Eats or Grubhub" in call("order_food", no_network, query="x", service="postmates")["error"]


# ------------------------------------------------------------------ parcels
@pytest.mark.parametrize(
    "raw, carrier, confidence, number",
    [
        ("1Z 999 AA1 01 2345 6784", "ups", "high", "1Z999AA10123456784"),
        ("1z999aa10123456784", "ups", "high", "1Z999AA10123456784"),
        ("9400 1000 0000 0000 0000 00", "usps", "medium", "9400100000000000000000"),
        ("9205590164917312751089", "usps", "medium", "9205590164917312751089"),
        ("9612019000000000000000", "fedex", "medium", "9612019000000000000000"),
        ("1234567890123456789012", "usps", "low", "1234567890123456789012"),
        ("42078701" + "9400100000000000000000", "usps", "high", "9400100000000000000000"),
        ("420787011234" + "9400100000000000000000", "usps", "high", "9400100000000000000000"),
        ("7019 0700 0000 1234 5678", "usps", "medium", "70190700000012345678"),
        ("EC123456789US", "usps", "high", "EC123456789US"),
        ("RR123456785GB", "usps", "medium", "RR123456785GB"),
        ("1234-5678-9012", "fedex", "high", "123456789012"),
        ("123456789012345", "fedex", "high", "123456789012345"),
        ("1234567890", "dhl", "medium", "1234567890"),
        ("TBA123456789012", "amazon", "high", "TBA123456789012"),
        ("ABC123XYZ", None, "none", "ABC123XYZ"),
        ("1234567", None, "none", "1234567"),
    ],
)
def test_detect_carrier_table(raw, carrier, confidence, number):
    d = detect_carrier(normalize_tracking(raw))
    assert (d["carrier"], d["confidence"], d["number"]) == (carrier, confidence, number)
    if confidence in ("medium", "low", "none"):
        assert d["alternatives"] and d["note"]  # ambiguity is spelled out


def test_tracking_urls():
    assert tracking_url("ups", "1Z999AA10123456784") == "https://www.ups.com/track?tracknum=1Z999AA10123456784"
    assert tracking_url("usps", "9400100000000000000000") == "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400100000000000000000"
    assert tracking_url("fedex", "123456789012") == "https://www.fedex.com/fedextrack/?trknbr=123456789012"
    assert tracking_url("dhl", "1234567890") == "https://www.dhl.com/us-en/home/tracking/tracking-express.html?submit=1&tracking-id=1234567890"
    assert tracking_url("amazon", "TBA1") == "https://www.amazon.com/gp/your-account/order-history"
    assert tracking_url(None, "XYZ123") == "https://www.google.com/search?q=XYZ123+tracking"


def test_track_package_opens_saves_lists_and_removes(opened):
    out = call("track_package", tracking_number="1Z999AA10123456784", label="new headphones")
    assert out["carrier"] == "ups" and out["confidence"] == "high" and out["opened"] is True
    assert opened == ["https://www.ups.com/track?tracknum=1Z999AA10123456784"]
    assert out["status"] == "Opened UPS tracking for 1Z999AA10123456784" and "not the status" in out["tip"]

    out = call("track_package", tracking_number="9400 1000 0000 0000 0000 00", open=False)
    assert out["carrier"] == "usps" and [a["carrier"] for a in out["alternatives"]] == ["FedEx"]
    assert out["alternatives"][0]["url"].startswith("https://www.fedex.com/") and len(opened) == 1

    listed = call("track_package")
    assert listed["status"] == "2 saved packages"
    assert {p["number"] for p in listed["packages"]} == {"9400100000000000000000", "1Z999AA10123456784"}
    ups = next(p for p in listed["packages"] if p["carrier"] == "ups")
    assert ups["label"] == "new headphones" and ups["url"].startswith("https://www.ups.com/")

    # Re-tracking keeps the label; removing deletes it.
    assert call("track_package", tracking_number="1z999aa10123456784", open=False)["label"] == "new headphones"
    assert call("track_package", tracking_number="1Z999AA10123456784", remove=True) == {"status": "removed", "removed": 1}
    assert call("track_package", tracking_number="1Z999AA10123456784", remove=True)["removed"] == 0
    assert call("track_package")["status"] == "1 saved package"


def test_track_package_carrier_override_unknown_and_amazon(opened):
    out = call("track_package", tracking_number="123456789012", carrier="ups", open=False)
    assert out["carrier"] == "ups" and out["confidence"] == "given" and "looks like FedEx" in out["note"]
    out = call("track_package", tracking_number="ZZ-TOP-42-AB", open=False)
    assert out["carrier"] == "unknown" and out["url"] == "https://www.google.com/search?q=ZZTOP42AB+tracking"
    assert out["alternatives"] == []
    out = call("track_package", tracking_number="TBA123456789012")
    assert out["url"] == "https://www.amazon.com/gp/your-account/order-history" and "Track package" in out["tip"]
    assert "doesn't look like a tracking number" in call("track_package", tracking_number="12 ab")["error"]


def test_saved_packages_prune_old_entries(isolated):
    old = (datetime.now().astimezone() - timedelta(days=120)).isoformat(timespec="minutes")
    data_dir = isolated["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "packages.json").write_text(json.dumps([
        {"number": "OLD1234567", "carrier": "dhl", "label": "", "added": old, "last_checked": old},
        {"number": "BAD", "carrier": "x", "last_checked": "not a date"},
        "junk",
    ]), encoding="utf-8")
    assert call("track_package")["packages"] == []


# ------------------------------------------------------------------ currency
def yahoo(prices: dict):
    """Yahoo chart handler: symbol -> price (missing symbols 404 like the real API)."""
    def handler(request):
        symbol = request.url.path.rsplit("/", 1)[-1]
        if symbol not in prices:
            return httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
        p = prices[symbol]
        meta = {"regularMarketPrice": p, "previousClose": p, "currency": "X", "regularMarketTime": 1_793_000_000}
        return json_response({"chart": {"result": [{"meta": meta, "indicators": {"quote": [{"close": [p]}]}}]}})

    return handler


def test_currency_codes_and_names():
    assert currency_code("eur") == "EUR" and currency_code(" Euros ") == "EUR" and currency_code("£") == "GBP"
    assert currency_code("US dollars") == "USD" and currency_code("yen") == "JPY" and currency_code("pounds sterling") == "GBP"
    assert currency_code("bitcoin") == "BTC" and currency_code("doge") == "DOGE"
    with pytest.raises(ToolError, match="Unknown currency"):
        currency_code("monopoly money")


def test_convert_currency_picks_the_more_precise_direction():
    # fetch_quote rounds EURUSD to 1.08 but USDEUR to 0.9230: the inverse is the precise one.
    handler = yahoo({"EURUSD=X": 1.0834, "USDEUR=X": 0.923})
    out = call("convert_currency", handler, amount=250, from_currency="EUR", to_currency="usd")
    assert out["rate"] == pytest.approx(1 / 0.923, rel=1e-5) and out["converted"] == pytest.approx(270.86, abs=0.01)
    assert out["result"] == "250 EUR = 270.86 USD" and out["inverse_rate"] == 0.923
    assert out["as_of"].startswith("2026-") and "Yahoo" in out["source"]


def test_convert_currency_inverse_only_and_cross_via_usd():
    out = call("convert_currency", yahoo({"USDJPY=X": 150.0}), amount="15,000", from_currency="JPY", to_currency="USD")
    assert out["amount"] == 15000 and out["converted"] == 100.0 and out["result"] == "15,000 JPY = 100 USD"

    handler = yahoo({"GBPUSD=X": 1.25, "USDINR=X": 80.0})  # no GBP/INR pair either way
    out = call("convert_currency", handler, amount=10, from_currency="GBP", to_currency="INR")
    assert out["via"] == "USD" and out["rate"] == pytest.approx(100.0) and out["converted"] == 1000.0


def test_convert_currency_crypto_same_currency_and_errors():
    out = call("convert_currency", yahoo({"BTC-USD": 60000.0}), amount=150, from_currency="USD", to_currency="BTC")
    assert out["converted"] == 0.0025 and out["result"] == "150 USD = 0.0025 BTC"
    out = call("convert_currency", no_network, amount=5, from_currency="usd", to_currency="$")
    assert out["converted"] == 5 and out["rate"] == 1.0 and out["source"] == "same currency"
    assert "Couldn't get a XXX to USD rate" in call("convert_currency", yahoo({}), from_currency="XXX", to_currency="USD")["error"]
    assert "Couldn't get a AAA to BBB rate" in call("convert_currency", yahoo({}), from_currency="AAA", to_currency="BBB")["error"]
    assert "amount must be a number" in call("convert_currency", no_network, amount="lots", from_currency="EUR", to_currency="USD")["error"]


# ------------------------------------------------------------------ robustness (review fixes)
def test_non_string_arguments_are_accepted(opened):
    """Models send ZIP codes and all-digit tracking numbers as JSON numbers."""
    router = Router({"nominatim.openstreetmap.org": json_response([])})
    out = call("find_places", router, query="pharmacy", near=78701)
    assert "error" not in out and router.requests[0].url.params["q"] == "pharmacy near 78701"
    out = call("get_directions", no_network, destination=78701, origin=None, mode=None, open=False)
    assert qs(out["url"])["destination"] == "78701" and out["mode"] == "driving"
    out = call("track_package", tracking_number=123456789012, label=42, open=False)
    assert out["carrier"] == "fedex" and out["label"] == "42"
    assert unquote(call("book_ride", destination=10001)["url"]).endswith("dropoff[formatted_address]=10001")
    assert call("order_food", query=42, open=False)["url"] == "https://www.doordash.com/search/store/42/"


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(500, text="<html>oops</html>"), "unavailable (HTTP 500)"),
        (httpx.Response(200, text="<html>maintenance</html>"), "unexpected reply"),
        (httpx.ConnectError("offline"), "Couldn't reach OpenStreetMap"),
        (httpx.ReadTimeout("slow"), "didn't answer in time"),
        (httpx.Response(403), "limiting requests"),
    ],
)
def test_find_places_service_failures_are_friendly(response, message):
    out = call("find_places", Router({"nominatim.openstreetmap.org": response}), query="pharmacy", near="Austin")
    assert message in out["error"] and "Traceback" not in out["error"]
    assert not errands._GEO_CACHE  # failures aren't cached


def test_nominatim_drops_results_with_bad_coordinates():
    bad = [{"lat": "abc", "lon": "1"}, {"lat": "95", "lon": "1"}, {"lat": "nan", "lon": "1"}, AUSTIN]
    out = call("find_places", Router({"nominatim.openstreetmap.org": json_response(bad)}), query="x", near="Austin")
    assert [r["name"] for r in out["results"]] == ["Austin"]


def test_has_desktop_detection(monkeypatch):
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(errands, "IS_WIN", False)
    monkeypatch.setattr(errands, "IS_MAC", False)
    assert REAL_HAS_DESKTOP() is False  # headless Linux: only console browsers would run
    monkeypatch.setenv("BROWSER", "w3m:lynx")
    assert REAL_HAS_DESKTOP() is False
    monkeypatch.setenv("BROWSER", "/usr/bin/wslview %s")  # WSL without WSLg hands links to Windows
    assert REAL_HAS_DESKTOP() is True
    monkeypatch.delenv("BROWSER")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert REAL_HAS_DESKTOP() is True
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setattr(errands, "IS_WIN", True)
    assert REAL_HAS_DESKTOP() is True


def test_headless_machine_returns_links_without_launching_a_browser(monkeypatch, opened):
    monkeypatch.setattr(errands, "_has_desktop", lambda: False)
    out = call("book_ride", no_network, destination="JFK")
    assert out["opened"] is False and out["status"].startswith("Couldn't open the browser") and out["url"]
    out = call("get_directions", no_network, destination="MoMA")
    assert out["opened"] is False and "link ready" in out["status"]
    assert opened == []


def test_open_url_only_https_and_never_hangs(monkeypatch):
    release = threading.Event()
    calls = []

    def stuck(url, *a, **k):  # e.g. a console browser waiting for its user
        calls.append(url)
        release.wait(5)
        return True

    monkeypatch.setattr(errands.webbrowser, "open", stuck)
    monkeypatch.setattr(errands, "OPEN_TIMEOUT", 0.1)
    assert run(errands._open_url("file:///etc/passwd")) is False
    assert run(errands._open_url("javascript:alert(1)")) is False
    assert calls == []
    started = time.monotonic()
    assert run(errands._open_url("https://www.lyft.com/")) is False
    assert time.monotonic() - started < 2 and calls == ["https://www.lyft.com/"]
    release.set()


def test_saved_packages_tolerate_hand_edited_entries(isolated):
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    data_dir = isolated["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "packages.json").write_text(json.dumps([
        {"number": "1Z999AA10123456784", "last_checked": now},  # no carrier
        {"number": 123456789012, "carrier": "fedex", "last_checked": now},  # number not a string
        {"number": "9400100000000000000000", "carrier": "usps", "last_checked": "0001-01-01T00:00"},
        {"number": "1234567890", "carrier": "dhl", "label": None, "last_checked": now},
    ]), encoding="utf-8")
    listed = call("track_package")
    assert listed["status"] == "2 saved packages"
    by_number = {p["number"]: p for p in listed["packages"]}
    assert by_number["1Z999AA10123456784"]["url"] == "https://www.google.com/search?q=1Z999AA10123456784+tracking"
    assert by_number["1234567890"]["label"] == ""
    (data_dir / "packages.json").write_text("{not json", encoding="utf-8")
    assert call("track_package")["packages"] == []


def test_track_package_other_named_carrier_wins_over_a_format_guess(opened):
    out = call("track_package", tracking_number="1234567890", carrier="Purolator", open=False)
    assert out["carrier"] == "Purolator" and out["confidence"] == "given"
    assert out["url"] == "https://www.google.com/search?q=Purolator+1234567890+tracking"
    assert out["alternatives"][0] == {"carrier": "DHL Express", "url": tracking_url("dhl", "1234567890")}
    assert "No direct tracking link for Purolator" in out["note"] and "DHL Express" in out["note"]
    listed = call("track_package")["packages"]
    assert listed[0]["carrier"] == "Purolator" and listed[0]["url"] == out["url"]
    assert call("track_package", tracking_number="1234567890", carrier="auto", open=False)["carrier"] == "dhl"
    assert "enum" not in TOOLS["track_package"].parameters["properties"]["carrier"]


def test_track_package_label_is_clipped():
    out = call("track_package", tracking_number="1Z999AA10123456784", label="x" * 500, open=False)
    assert len(out["label"]) <= 80


@pytest.mark.parametrize(
    "raw, value",
    [(250, 250.0), ("15,000", 15000.0), ("1,234,567.5", 1234567.5), ("12,50", 12.5), ("$250", 250.0),
     ("€ 1 000", 1000.0), (None, 1.0), ("", 1.0), (-3, -3.0)],
)
def test_parse_amount(raw, value):
    assert errands.parse_amount(raw) == value


@pytest.mark.parametrize("raw", [True, "lots", "1e999", "nan", [1], {"v": 1}])
def test_parse_amount_rejects(raw):
    with pytest.raises(ToolError, match="amount must be a"):
        errands.parse_amount(raw)


def test_convert_currency_outage_is_reported_as_an_outage():
    router = Router({"query1.finance.yahoo.com": httpx.ConnectError("down"), "query2.finance.yahoo.com": httpx.ConnectError("down")})
    out = call("convert_currency", router, amount=10, from_currency="EUR", to_currency="GBP")
    assert "Couldn't reach Yahoo Finance" in out["error"] and "Check the currency codes" not in out["error"]
    symbols = {r.url.path.rsplit("/", 1)[-1] for r in router.requests}
    assert symbols == {"EURGBP=X", "GBPEUR=X"}  # no pointless second round through USD


def test_has_location_ignores_walk_in_and_drive_in():
    assert not errands._has_location("walk-in clinic") and not errands._has_location("drive in theater")
    assert errands._has_location("walk in clinic in Austin") and errands._has_location("sushi in Brooklyn")
    assert errands._has_location("pizza, Austin")


def test_short_distances_are_readable():
    assert errands._fmt_distance(40, True) == "130 ft" and errands._fmt_distance(40, False) == "40 m"
    assert errands._fmt_distance(850, False) == "850 m" and errands._fmt_distance(1609.344, True) == "1.0 mi"


def test_food_search_url_without_searchable_text_is_the_home_page():
    assert food_search_url("doordash", "..") == "https://www.doordash.com/"
    assert food_search_url("ubereats", " / ") == "https://www.ubereats.com/"


def test_rate_limiter_is_shared_across_threads_and_event_loops():
    lim = RateLimiter(1.0, max_wait=1000, clock=lambda: 7.0, sleep=lambda d: asyncio.sleep(0))
    delays = []
    lock = threading.Lock()

    def worker():
        async def go():
            for _ in range(20):
                d = lim.reserve()
                await lim.sleep(d)
                with lock:
                    delays.append(d)

        asyncio.run(go())  # each thread runs its own event loop

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(delays) == [float(i) for i in range(120)]  # every caller got its own 1 s slot


def test_track_package_still_returns_the_link_when_the_list_cannot_be_saved(monkeypatch):
    def locked(items):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(errands, "_save_packages", locked)
    out = call("track_package", tracking_number="1Z999AA10123456784", open=False)
    assert out["url"].startswith("https://www.ups.com/") and "Permission denied" in out["save_error"]
    assert "Permission denied" in call("track_package", tracking_number="1Z999AA10123456784", remove=True)["error"]
