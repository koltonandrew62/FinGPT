import json

import httpx
import pytest

from app.tools import ToolError, call_tool, markets, weather
from app.tools.markets import _r, fetch_quote, watchlist_snapshot
from app.tools.weather import geocode, get_weather_data
from conftest import Router, json_response, make_ctx, mock_http, run


def chart(price=105.0, prev=100.0, chart_prev=90.0, closes=None, **meta):
    m = {
        "currency": "USD",
        "symbol": "AAPL",
        "regularMarketPrice": price,
        "chartPreviousClose": chart_prev,
        "previousClose": prev,
        "regularMarketDayHigh": 106.123,
        "regularMarketDayLow": 99.5,
        "fiftyTwoWeekHigh": 150,
        "fiftyTwoWeekLow": 80,
        "fullExchangeName": "NasdaqGS",
        "shortName": "Apple Inc.",
        "regularMarketTime": 1_793_000_000,
    }
    m.update(meta)
    closes = closes if closes is not None else [100.0 + i * 0.01 for i in range(200)] + [None, None]
    return json_response({"chart": {"result": [{"meta": m, "indicators": {"quote": [{"close": closes}]}}], "error": None}})


def quote(handler, symbol="aapl", range_="1d"):
    async def go():
        async with mock_http(handler) as http:
            return await fetch_quote(http, symbol, range_)

    return run(go())


def test_fetch_quote_1d_uses_previous_close():
    router = Router({"query1.finance.yahoo.com/v8/finance/chart/AAPL": chart()})
    q = quote(router)
    assert q["symbol"] == "AAPL" and q["name"] == "Apple Inc."
    assert q["price"] == 105.0 and q["change"] == 5.0 and q["change_pct"] == 5.0
    assert q["day_high"] == 106.12 and q["exchange"] == "NasdaqGS" and q["currency"] == "USD"
    assert q["as_of"].startswith("2026-")
    assert 30 <= len(q["spark"]) <= 41 and None not in q["spark"]
    params = router.requests[0].url.params
    assert params["range"] == "1d" and params["interval"] == "5m"


def test_fetch_quote_longer_range_uses_chart_previous_close():
    router = Router({"query1.finance.yahoo.com/v8/finance/chart/AAPL": chart()})
    q = quote(router, range_="1mo")
    assert q["change"] == 15.0 and q["change_pct"] == pytest.approx(16.67)
    assert router.requests[0].url.params["interval"] == "1d"


def test_fetch_quote_bad_range_defaults_and_friendly_names():
    router = Router({"query1.finance.yahoo.com/v8/finance/chart/^GSPC": chart(price=5000.0, prev=4950.0, shortName="S&P 500 idx")})
    q = quote(router, "^gspc", "forever")
    assert q["name"] == "S&P 500" and q["range"] == "1d"
    assert "/chart/%5EGSPC" in str(router.requests[0].url)  # ticker is URL-encoded
    assert q["change_pct"] == pytest.approx(1.01)


def test_fetch_quote_without_price_uses_last_close():
    router = Router({"query1.finance.yahoo.com/v8/finance/chart/AAPL": chart(price=None, closes=[1.0, 2.0, None])})
    q = quote(router)
    assert q["price"] == 2.0 and q["spark"] == [1.0, 2.0]


def test_fetch_quote_unknown_ticker_does_not_retry():
    router = Router({"query1.finance.yahoo.com": httpx.Response(404)})
    with pytest.raises(ToolError, match="Unknown ticker 'ZZZZ'"):
        quote(router, "zzzz")
    assert len(router.requests) == 1


def test_fetch_quote_falls_back_to_query2_and_caches():
    router = Router({"query1.finance.yahoo.com": httpx.Response(503), "query2.finance.yahoo.com": chart()})

    async def go():
        async with mock_http(router) as http:
            a = await fetch_quote(http, "AAPL")
            b = await fetch_quote(http, "AAPL")
            return a, b

    a, b = run(go())
    assert a == b and len(router.requests) == 2


def test_fetch_quote_failures():
    with pytest.raises(ToolError, match="Market data unavailable for AAPL"):
        quote(lambda r: httpx.Response(500))
    markets._CACHE.clear()
    with pytest.raises(ToolError, match="No data for 'AAPL'"):
        quote(lambda r: json_response({"chart": {"result": None, "error": {"code": "Not Found"}}}))


def test_r_rounding():
    assert _r(None) is None
    assert _r(1234.5678) == 1234.57
    assert _r(0.123456) == 0.1235
    assert _r(-0.5, 2) == -0.5
    assert _r(3.14159, 2) == 3.14


def test_get_market_data_tool():
    def handler(r):
        if "BAD" in r.url.path:
            return httpx.Response(404)
        return chart()

    async def go():
        async with mock_http(handler) as http:
            ctx = make_ctx(http=http)
            ok = json.loads(await call_tool("get_market_data", {"symbols": "AAPL, BAD"}, ctx))
            bad = json.loads(await call_tool("get_market_data", {"symbols": ["BAD"]}, ctx))
            return ok, bad

    ok, bad = run(go())
    assert [q["symbol"] for q in ok["quotes"]] == ["AAPL"]
    assert "spark" not in ok["quotes"][0]
    assert ok["errors"] == [{"symbol": "BAD", "error": "Unknown ticker 'BAD'. Try lookup_ticker."}]
    assert "Unknown ticker" in bad["error"]
    assert "spark" in markets._CACHE["AAPL:1d"][1]  # the tool's copy doesn't strip the cached sparkline


def test_watchlist_snapshot_skips_failures():
    def handler(r):
        return chart() if "AAPL" in r.url.path else httpx.Response(404)

    async def go():
        async with mock_http(handler) as http:
            return await watchlist_snapshot(make_ctx(http=http, watchlist=["AAPL", "NOPE"]))

    out = run(go())
    assert [q["symbol"] for q in out] == ["AAPL"]


def test_lookup_ticker():
    router = Router({"query2.finance.yahoo.com/v1/finance/search": json_response({"quotes": [{"symbol": "NVDA", "shortname": "NVIDIA Corporation", "quoteType": "EQUITY", "exchDisp": "NASDAQ"}]})})

    async def go():
        async with mock_http(router) as http:
            return json.loads(await call_tool("lookup_ticker", {"query": "nvidia"}, make_ctx(http=http)))

    assert run(go()) == {"matches": [{"symbol": "NVDA", "name": "NVIDIA Corporation", "type": "EQUITY", "exchange": "NASDAQ"}]}


# --------------------------------------------------------------------- weather
GEO = {
    "results": [
        {"name": "Austin", "admin1": "Minnesota", "country": "United States", "country_code": "US", "latitude": 43.67, "longitude": -92.97},
        {"name": "Austin", "admin1": "Texas", "country": "United States", "country_code": "US", "latitude": 30.27, "longitude": -97.74},
    ]
}
FORECAST = {
    "current": {"temperature_2m": 72.4, "apparent_temperature": 70.1, "relative_humidity_2m": 40, "weather_code": 2, "wind_speed_10m": 8.5, "is_day": 0, "precipitation": 0},
    "daily": {
        "time": ["2026-11-04", "2026-11-05"],
        "weather_code": [2, 63],
        "temperature_2m_max": [75.0, 68.0],
        "temperature_2m_min": [55.0, 50.0],
        "precipitation_probability_max": [10, 80],
        "uv_index_max": [5.1, 2.0],
        "sunrise": ["2026-11-04T07:01", "2026-11-05T07:02"],
        "sunset": ["2026-11-04T17:45", "2026-11-05T17:44"],
    },
}


def weather_router():
    return Router({"geocoding-api.open-meteo.com/v1/search": json_response(GEO), "api.open-meteo.com/v1/forecast": json_response(FORECAST)})


def test_geocode_uses_region_hint_and_caches():
    router = weather_router()

    async def go():
        async with mock_http(router) as http:
            a = await geocode(http, "Austin, Texas")
            b = await geocode(http, "austin, texas ")
            c = await geocode(http, "Austin")
            return a, b, c

    a, b, c = run(go())
    assert a == {"name": "Austin", "region": "Texas", "country": "United States", "latitude": 30.27, "longitude": -97.74}
    assert b == a
    assert c["region"] == "Minnesota"  # no hint: first result
    assert len(router.requests) == 2
    assert router.requests[0].url.params["name"] == "Austin"


def test_geocode_not_found():
    async def go():
        async with mock_http(Router({"geocoding-api.open-meteo.com/v1/search": json_response({})})) as http:
            return await geocode(http, "Atlantis")

    with pytest.raises(ToolError, match="Couldn't find a place called 'Atlantis'"):
        run(go())


def test_get_weather_data_imperial():
    router = weather_router()

    async def go():
        async with mock_http(router) as http:
            return await get_weather_data(make_ctx(http=http, home_city="Austin, Texas"))

    out = run(go())
    assert out["location"] == "Austin, Texas, United States"
    assert out["units"] == {"temp": "°F", "wind": "mph"}
    assert out["current"] == {"temperature": 72.4, "feels_like": 70.1, "humidity": 40, "wind": 8.5, "condition": "Partly cloudy", "code": 2, "is_day": False}
    assert out["forecast"][1] == {"date": "2026-11-05", "condition": "Rain", "high": 68.0, "low": 50.0, "precip_chance": 80, "uv_max": 2.0, "sunrise": "07:02", "sunset": "17:44"}
    params = router.hits("forecast")[0].url.params
    assert params["temperature_unit"] == "fahrenheit" and params["wind_speed_unit"] == "mph"
    assert params["latitude"] == "30.27" and params["forecast_days"] == "4"


def test_get_weather_tool_metric_days_clamped_and_cached():
    router = weather_router()

    async def go():
        async with mock_http(router) as http:
            ctx = make_ctx(http=http, units="metric")
            a = json.loads(await call_tool("get_weather", {"location": "Austin", "days": 30}, ctx))
            b = json.loads(await call_tool("get_weather", {"location": "Austin", "days": 30}, ctx))
            return a, b

    a, b = run(go())
    assert a == b and a["units"] == {"temp": "°C", "wind": "km/h"}
    forecasts = router.hits("forecast")
    assert len(forecasts) == 1
    assert forecasts[0].url.params["forecast_days"] == "7" and forecasts[0].url.params["temperature_unit"] == "celsius"


def test_get_weather_ip_fallback():
    router = Router(
        {
            "ipapi.co/json/": json_response({"error": True, "reason": "RateLimited"}),
            "ipwho.is/": json_response({"city": "Lisbon", "region": "Lisbon", "country": "Portugal", "latitude": 38.72, "longitude": -9.14}),
            "api.open-meteo.com/v1/forecast": json_response(FORECAST),
        }
    )

    async def go():
        async with mock_http(router) as http:
            return await get_weather_data(make_ctx(http=http))

    assert run(go())["location"] == "Lisbon, Lisbon, Portugal"


def test_get_weather_ip_fallback_fails():
    async def go():
        async with mock_http(lambda r: httpx.Response(500, text="nope")) as http:
            return await get_weather_data(make_ctx(http=http))

    with pytest.raises(ToolError, match="Set HOME_CITY"):
        run(go())
