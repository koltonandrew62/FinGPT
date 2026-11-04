"""Live finance & economy sources (CoinGecko, SEC EDGAR, Nasdaq, CNN, FRED), fully offline via httpx.MockTransport.

Fixtures mirror the real APIs' JSON/CSV shapes."""
import json
from datetime import date, timedelta

import httpx
import pytest

from app import config
from app.tools import TOOLS, ToolError, call_tool, data_sources as ds, markets
from conftest import Router, json_response, make_ctx, mock_http, no_network, run

DS_TOOLS = ["get_crypto", "get_sec_filings", "get_company_financials", "get_earnings_calendar", "get_market_sentiment", "get_economic_data"]
KEYS = ("COINGECKO_API_KEY", "SEC_CONTACT_EMAIL", "FRED_API_KEY")


@pytest.fixture(autouse=True)
def ds_env(monkeypatch):
    """Empty caches, no configured keys and no real waiting on the per-host throttles."""
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ds, "_LIMITS", {host: ds.Throttle(0, name=t.name) for host, t in ds._LIMITS.items()})
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ds, "_sleep", fake_sleep)
    ds._CACHE.clear()
    yield sleeps
    ds._CACHE.clear()


def call(name, handler=no_network, **args):
    async def go():
        async with mock_http(handler) as http:
            return await call_tool(name, args, make_ctx(http=http))

    raw = run(go())
    return json.loads(raw), raw


def call_fn(fn, handler=no_network, **args):
    async def go():
        async with mock_http(handler) as http:
            return await fn(make_ctx(http=http), **args)

    return run(go())


def set_env(isolated, **values):
    isolated["env_path"].write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def yahoo_chart(symbol, price, prev):
    meta = {
        "currency": "USD", "symbol": symbol, "regularMarketPrice": price, "chartPreviousClose": prev, "previousClose": prev,
        "regularMarketDayHigh": price * 1.01, "regularMarketDayLow": price * 0.99, "fiftyTwoWeekHigh": price * 1.5,
        "fiftyTwoWeekLow": price * 0.5, "fullExchangeName": "CCC", "shortName": symbol, "regularMarketTime": 1_793_800_000,
    }
    return json_response({"chart": {"result": [{"meta": meta, "indicators": {"quote": [{"close": [prev, price]}]}}], "error": None}})


def test_tools_registered_read_only_and_described():
    for name in DS_TOOLS:
        assert name in TOOLS, name
        assert name not in config.SENSITIVE_TOOLS and TOOLS[name].gate is None
        assert len(TOOLS[name].description) > 150
    assert "15 min" in TOOLS["get_market_sentiment"].description
    assert "get_market_data" in TOOLS["get_crypto"].description


# =========================================================================== throttle
def test_throttle_spacing_respects_sec_ten_per_second():
    t = ds.Throttle(9, 1, max_wait=100, clock=lambda: 0.0)
    delays = [t.reserve() for _ in range(20)]
    assert delays[0] == 0 and all(b > a for a, b in zip(delays, delays[1:]))
    assert sum(1 for d in delays if d < 1.0) <= 10  # never more than 10 requests inside the first second


def test_throttle_burst_then_paced_and_max_wait():
    t = ds.Throttle(2, 3, max_wait=1.0, clock=lambda: 50.0)
    assert [t.reserve() for _ in range(3)] == [0, 0, 0]
    assert t.reserve() == pytest.approx(0.5) and t.reserve() == pytest.approx(1.0)
    with pytest.raises(ToolError, match="Too many requests"):
        t.reserve()


def test_requests_go_through_host_throttle(ds_env, monkeypatch):
    sec = ds.Throttle(9, 1, max_wait=100, clock=lambda: 0.0, name="SEC EDGAR")
    monkeypatch.setitem(ds._LIMITS, "www.sec.gov", sec)
    monkeypatch.setitem(ds._LIMITS, "data.sec.gov", sec)
    router = Router({"www.sec.gov/files/company_tickers.json": json_response(TICKERS), "data.sec.gov": json_response(APPLE_SUBMISSIONS)})
    call("get_sec_filings", router, ticker="AAPL")
    assert len(router.requests) == 2 and ds_env == [pytest.approx(1 / 9)]


# =========================================================================== CoinGecko
def cg_coin(cid, sym, name, rank, price, mcap, vol, h1, h24, d7, ath_pct):
    return {
        "id": cid, "symbol": sym, "name": name, "image": f"https://coin-images.coingecko.com/coins/images/1/large/{cid}.png",
        "current_price": price, "market_cap": mcap, "market_cap_rank": rank, "fully_diluted_valuation": mcap * 1.05,
        "total_volume": vol, "high_24h": price * 1.02, "low_24h": price * 0.97, "price_change_24h": price * h24 / 100,
        "price_change_percentage_24h": h24, "market_cap_change_24h": mcap * h24 / 100, "market_cap_change_percentage_24h": h24,
        "circulating_supply": 19_790_000.0, "total_supply": 21_000_000.0, "max_supply": 21_000_000.0, "ath": price * 1.1,
        "ath_change_percentage": ath_pct, "ath_date": "2025-10-06T18:57:42.558Z", "atl": 67.81, "atl_change_percentage": 143000.1,
        "atl_date": "2013-07-06T00:00:00.000Z", "roi": None, "last_updated": "2026-11-04T15:30:12.345Z",
        "price_change_percentage_1h_in_currency": h1, "price_change_percentage_24h_in_currency": h24,
        "price_change_percentage_7d_in_currency": d7,
    }


BTC = cg_coin("bitcoin", "btc", "Bitcoin", 1, 97123.0, 1_923_456_789_012, 45_678_901_234, 0.1532, 2.11345, -1.9876, -10.184)
ETH = cg_coin("ethereum", "eth", "Ethereum", 2, 3456.78, 416_789_012_345, 21_345_678_901, -0.2, -0.53, 4.2, -29.1)
USDT = cg_coin("tether", "usdt", "Tether", 3, 1.0003, 183_456_789_012, 98_765_432_109, 0.01, 0.02, -0.01, -24.6)
PEPE = cg_coin("pepe", "pepe", "Pepe", 31, 0.00001234567, 5_191_234_567, 1_234_567_890, 1.2, 12.345, 30.1, -55.0)
CG_GLOBAL = {
    "data": {
        "active_cryptocurrencies": 17234, "upcoming_icos": 0, "ongoing_icos": 49, "ended_icos": 3376, "markets": 1250,
        "total_market_cap": {"btc": 35_123_456.7, "eth": 987_654_321.0, "usd": 3_412_345_678_901.2, "eur": 3_150_000_000_000.0},
        "total_volume": {"btc": 1_271_234.1, "usd": 123_456_789_012.3, "eur": 114_000_000_000.0},
        "market_cap_percentage": {"btc": 56.37, "eth": 12.21, "usdt": 5.38, "xrp": 4.1},
        "market_cap_change_percentage_24h_usd": 1.7234, "updated_at": 1_793_800_000,
    }
}
CG_TRENDING = {
    "coins": [
        {"item": {"id": "pepe", "coin_id": 29850, "name": "Pepe", "symbol": "PEPE", "market_cap_rank": 31, "thumb": "https://x/thumb.png",
                  "small": "https://x/small.png", "large": "https://x/large.png", "slug": "pepe", "price_btc": 1.27e-10, "score": 0,
                  "data": {"price": 0.00001234567, "price_btc": "0.000000000127", "price_change_percentage_24h": {"usd": 12.345, "btc": 10.1},
                           "market_cap": "$5,191,234,567", "market_cap_btc": "53412.1", "total_volume": "$1,234,567,890",
                           "total_volume_btc": "12701.2", "sparkline": "https://www.coingecko.com/coins/29850/sparkline.svg", "content": None}}},
        {"item": {"id": "sui", "coin_id": 26375, "name": "Sui", "symbol": "SUI", "market_cap_rank": 22, "slug": "sui", "price_btc": 3.6e-5, "score": 1,
                  "data": {"price": "$3.51", "price_change_percentage_24h": {"usd": -2.5}, "market_cap": "$11,234,567,890"}}},
    ],
    "nfts": [{"id": "pudgy-penguins", "name": "Pudgy Penguins", "symbol": "PPG", "floor_price_in_usd_24h_percentage_change": 3.1}],
    "categories": [{"id": 1, "name": "Meme", "market_cap_1h_change": 0.5, "slug": "meme-token"}],
}
CG_SEARCH_PEPE = {
    "coins": [
        {"id": "pepe", "name": "Pepe", "api_symbol": "pepe", "symbol": "PEPE", "market_cap_rank": 31, "thumb": "t", "large": "l"},
        {"id": "pepecoin-network", "name": "PepeCoin", "api_symbol": "pepecoin-network", "symbol": "PEPECOIN", "market_cap_rank": 1500, "thumb": "t", "large": "l"},
        {"id": "pepe-2", "name": "Pepe 2.0", "api_symbol": "pepe-2", "symbol": "PEPE2.0", "market_cap_rank": None, "thumb": "t", "large": "l"},
    ],
    "exchanges": [], "icos": [], "categories": [], "nfts": [],
}


def cg_router(markets=None, glob=None, trending=None, search=None):
    def markets_handler(request):
        if markets is not None and not isinstance(markets, list):
            return markets(request) if callable(markets) else markets
        coins = markets if markets is not None else [BTC, ETH, USDT]
        ids = request.url.params.get("ids")
        if ids:
            coins = [c for c in [BTC, ETH, USDT, PEPE] if c["id"] in ids.split(",")]
        return json_response(coins[: int(request.url.params.get("per_page", 100))])

    return Router(
        {
            "api.coingecko.com/api/v3/coins/markets": markets_handler,
            "api.coingecko.com/api/v3/global": glob or json_response(CG_GLOBAL),
            "api.coingecko.com/api/v3/search/trending": trending or json_response(CG_TRENDING),
            "api.coingecko.com/api/v3/search": search or json_response(CG_SEARCH_PEPE),
        }
    )


def test_crypto_overview_markets_global_trending():
    router = cg_router()
    out, raw = call("get_crypto", router)
    assert out["status"] == "Crypto market $3.41T (+1.7% 24h) · BTC $97,123 (+2.1%) · ETH $3,457 (-0.5%)"
    btc = out["coins"][0]
    assert btc == {
        "rank": 1, "name": "Bitcoin", "symbol": "BTC", "price": 97123.0, "change_1h_pct": 0.15, "change_24h_pct": 2.11,
        "change_7d_pct": -1.99, "market_cap": "$1.92T", "volume_24h": "$45.68B", "from_ath_pct": -10.2, "id": "bitcoin",
    }
    assert out["global"] == {
        "total_market_cap": "$3.41T", "change_24h_pct": 1.72, "volume_24h": "$123.46B", "btc_dominance_pct": 56.4,
        "eth_dominance_pct": 12.2, "active_coins": 17234,
    }
    assert out["trending"][0] == {"name": "Pepe", "symbol": "PEPE", "rank": 31, "price_usd": 1.23457e-05, "change_24h_pct": 12.35}
    assert out["trending"][1]["price_usd"] == 3.51  # older payloads send the price as a "$3.51" string
    params = router.hits("/coins/markets")[0].url.params
    assert params["vs_currency"] == "usd" and params["price_change_percentage"] == "1h,24h,7d" and params["per_page"] == "10"
    assert all("x-cg-demo-api-key" not in r.headers for r in router.requests)
    assert "image" not in raw and "atl_date" not in raw and len(raw) < 6000


def test_crypto_top_25_stays_compact():
    many = [cg_coin(f"coin{i}", f"c{i}", f"Coin {i}", i + 1, 10.0 + i, 1e9 * (30 - i), 1e8, 0.1, 0.2, 0.3, -5.0) for i in range(25)]
    out, raw = call("get_crypto", cg_router(markets=many), top=25)
    assert len(out["coins"]) == 25 and len(raw) < 9000


def test_crypto_lookup_common_symbol_and_search():
    router = cg_router()
    out, _ = call("get_crypto", router, query="BTC and pepe")
    assert [c["symbol"] for c in out["coins"]] == ["BTC", "PEPE"]
    assert out["coins"][1]["price"] == 1.23457e-05
    assert out["status"].startswith("Bitcoin $97,123 (+2.1% 24h) · Pepe $0.00001235 (+12.3% 24h)")  # no "e-05" read aloud
    assert len(router.hits("/search?")) == 1 and router.hits("/search?")[0].url.params["query"] == "pepe"  # BTC needs no search
    assert router.hits("/coins/markets")[0].url.params["ids"] == "bitcoin,pepe"
    assert "global" not in out


def test_crypto_trending_only():
    out, _ = call("get_crypto", cg_router(), query="trending")
    assert "Trending: PEPE, SUI" in out["status"] and "coins" not in out and len(out["trending"]) == 2


def test_crypto_unknown_coin_is_friendly():
    out, _ = call("get_crypto", cg_router(search=json_response({"coins": [], "exchanges": [], "icos": [], "categories": [], "nfts": []})), query="zzzcoin")
    assert "doesn't list a coin called 'zzzcoin'" in out["error"]


def test_crypto_demo_key_sent_but_never_shown(isolated):
    set_env(isolated, COINGECKO_API_KEY="CG-demoKEY123456")
    router = cg_router(markets=json_response({"status": {"error_code": 10002, "error_message": "Invalid API key CG-demoKEY123456"}}, 401))
    router.add("query1.finance.yahoo.com/v8/finance/chart/ETH-USD", yahoo_chart("ETH-USD", 3400.0, 3500.0))
    out, raw = call("get_crypto", router, query="ethereum, pepe")
    assert router.requests[0].headers["x-cg-demo-api-key"] == "CG-demoKEY123456"
    # ETH has a Yahoo fallback, so the answer still comes through and names the problem; PEPE has none.
    assert [c["symbol"] for c in out["coins"]] == ["ETH"] and out["not_found"] == ["Pepe (no fallback price)"]
    assert "COINGECKO_API_KEY" in out["unavailable"][0] and "CG-demoKEY123456" not in raw


def test_crypto_bad_key_without_fallback_names_the_setting(isolated):
    set_env(isolated, COINGECKO_API_KEY="CG-demoKEY123456")
    out, raw = call("get_crypto", cg_router(search=json_response({"status": {"error_code": 10002}}, 401)), query="pepe")
    assert "COINGECKO_API_KEY" in out["error"] and "CG-demoKEY123456" not in raw


def test_crypto_429_falls_back_to_yahoo():
    router = cg_router(markets=json_response({"status": {"error_code": 429, "error_message": "You've exceeded the Rate Limit."}}, 429))
    router.add("query1.finance.yahoo.com/v8/finance/chart/BTC-USD", yahoo_chart("BTC-USD", 97000.0, 95000.0))
    router.add("query1.finance.yahoo.com/v8/finance/chart/ETH-USD", yahoo_chart("ETH-USD", 3400.0, 3500.0))
    out, _ = call("get_crypto", router, query="bitcoin, eth")
    assert out["source"].startswith("Yahoo Finance (fallback")
    assert [(c["symbol"], c["price"]) for c in out["coins"]] == [("BTC", 97000.0), ("ETH", 3400.0)]
    assert out["coins"][0]["change_24h_pct"] == 2.11
    assert "rate-limiting" in out["unavailable"][0]


def test_crypto_overview_429_falls_back_to_yahoo_major_coins():
    limited = json_response({"status": {"error_code": 429}}, 429)
    router = Router({"api.coingecko.com": limited})
    for sym in ds.YAHOO_DEFAULT:
        router.add(f"query1.finance.yahoo.com/v8/finance/chart/{sym}-USD", yahoo_chart(f"{sym}-USD", 10.0, 9.0))
    out, _ = call("get_crypto", router, top=3)
    assert [c["symbol"] for c in out["coins"]] == ["BTC", "ETH", "XRP"]
    assert len(out["unavailable"]) == 3 and "global" not in out


def test_crypto_429_with_no_fallback_is_friendly():
    router = Router({"api.coingecko.com": json_response({"status": {"error_code": 429}}, 429), "query1.finance.yahoo.com": httpx.Response(503), "query2.finance.yahoo.com": httpx.Response(503)})
    out, _ = call("get_crypto", router, query="pepe")
    assert "CoinGecko is rate-limiting" in out["error"] and "COINGECKO_API_KEY" in out["error"]
    out, _ = call("get_crypto", router)
    assert "CoinGecko is rate-limiting" in out["error"]


def test_crypto_results_are_cached():
    router = cg_router()
    call("get_crypto", router)
    n = len(router.requests)
    call("get_crypto", router)
    assert len(router.requests) == n == 3


def test_crypto_pulse_never_raises_and_caches_failure_briefly():
    def down(request):
        raise httpx.ConnectError("offline")

    router = Router({"api.coingecko.com": down, "query1.finance.yahoo.com": down, "query2.finance.yahoo.com": down})
    out = call_fn(ds.crypto_pulse, router)
    assert out["coins"] == [] and out["error"] and "Traceback" not in out["error"]
    n = len(router.requests)
    call_fn(ds.crypto_pulse, router)
    assert len(router.requests) == n  # the failure is cached, so a refresh loop doesn't hammer the API


def test_crypto_pulse_success_cached_and_stale_on_failure():
    good = call_fn(ds.crypto_pulse, cg_router())
    assert good["coins"][0] == {"symbol": "BTC", "name": "Bitcoin", "price": 97123.0, "change_24h_pct": 2.11, "change_7d_pct": -1.99}
    assert good["global"] == {"total_market_cap": "$3.41T", "change_24h_pct": 1.72, "btc_dominance_pct": 56.4}
    assert call_fn(ds.crypto_pulse, no_network) == good  # cached
    stamp, value = ds._CACHE[("panel_crypto",)]
    ds._CACHE.clear()
    ds._CACHE[("panel_crypto",)] = (stamp - 600, value)  # expired, but recent enough to show as stale
    router = Router({"api.coingecko.com": httpx.Response(503), "query1.finance.yahoo.com": httpx.Response(503), "query2.finance.yahoo.com": httpx.Response(503)})
    stale = call_fn(ds.crypto_pulse, router)
    assert stale["stale"] is True and stale["coins"] == good["coins"]


# =========================================================================== SEC EDGAR
TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "3": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "4": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "5": {"cik_str": 1418091, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
}


def filing_columns(rows):
    """EDGAR's columnar filing arrays, as in submissions' filings.recent and (top level) the older pages."""
    cols = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "act", "form", "fileNumber", "filmNumber",
            "items", "core_type", "size", "isXBRL", "isInlineXBRL", "primaryDocument", "primaryDocDescription"]
    recent = {c: [] for c in cols}
    for accn, filed, report, form, items, doc, desc in rows:
        for c, v in zip(cols, [accn, filed, report, f"{filed}T16:30:12.000Z", "34", form, "001-36743", "251234567", items,
                               form, 123456, 1, 1, doc, desc]):
            recent[c].append(v)
    return recent


def submissions(rows):
    recent = filing_columns(rows)
    return {
        "cik": "320193", "entityType": "operating", "sic": "3571", "sicDescription": "Electronic Computers", "ownerOrg": "06 Technology",
        "insiderTransactionForOwnerExists": 0, "insiderTransactionForIssuerExists": 1, "name": "Apple Inc.", "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"], "ein": "942404110", "category": "Large accelerated filer", "fiscalYearEnd": "0926",
        "stateOfIncorporation": "CA", "addresses": {"business": {"street1": "ONE APPLE PARK WAY", "city": "CUPERTINO"}},
        "phone": "(408) 996-1010", "flags": "", "formerNames": [],
        "filings": {"recent": recent, "files": [{"name": "CIK0000320193-submissions-001.json", "filingCount": 1210, "filingFrom": "1994-01-26", "filingTo": "2015-03-02"}]},
    }


APPLE_SUBMISSIONS = submissions([
    ("0000320193-26-000105", "2026-11-03", "2026-10-31", "4", "", "xslF345X05/wk-form4_1762212345.xml", "FORM 4"),
    ("0000320193-26-000101", "2026-10-31", "2026-09-26", "10-K", "", "aapl-20260926.htm", "10-K"),
    ("0000320193-26-000099", "2026-10-30", "2026-10-30", "8-K", "2.02,9.01", "aapl-20261030.htm", "8-K"),
    ("0001140361-26-038000", "2026-10-15", "", "SC 13G/A", "", "ef20012345_sc13ga.htm", "SC 13G/A"),
    ("0000320193-26-000077", "2026-08-01", "2026-06-27", "10-Q", "", "aapl-20260627.htm", "10-Q"),
    ("0000320193-26-000070", "2026-07-10", "2025-09-27", "10-K/A", "", "aapl-20250927a.htm", "10-K/A"),
    ("0001308179-26-000010", "2026-01-09", "2026-02-24", "DEF 14A", "", "aapl4359751-def14a.htm", "DEF 14A"),
])


OLDER_PAGE = filing_columns([  # CIK0000320193-submissions-001.json: older filings, same columns at top level
    ("0000320193-15-000001", "2015-02-27", "2015-02-25", "4", "", "xslF345X03/wf-form4_142507.xml", "FORM 4"),
    ("0001193125-14-383437", "2014-10-27", "2014-09-27", "10-K", "", "d783162d10k.htm", "FORM 10-K"),
])


def sec_router(subs=APPLE_SUBMISSIONS, concepts=None, tickers=None, older=OLDER_PAGE):
    concepts = concepts or {}

    def data_sec(request):
        path = request.url.path
        if path.startswith("/submissions/") and "-submissions-" in path:
            return older if isinstance(older, httpx.Response) else json_response(older)
        if path.startswith("/submissions/"):
            return subs(request) if callable(subs) else json_response(subs)
        concept = path.rsplit("/", 1)[-1].removesuffix(".json")
        hit = concepts.get(concept)
        if hit is None:
            return httpx.Response(404, text="<html><body>NoSuchKey</body></html>")
        return hit if isinstance(hit, httpx.Response) else json_response(hit)

    return Router({"www.sec.gov/files/company_tickers.json": tickers or json_response(TICKERS), "data.sec.gov": data_sec})


def test_sec_filings_recent_with_direct_document_links():
    router = sec_router()
    out, raw = call("get_sec_filings", router, ticker="aapl", limit=5)
    assert out["company"] == "Apple Inc." and out["ticker"] == "AAPL" and out["cik"] == "0000320193"
    assert router.hits("data.sec.gov")[0].url.path == "/submissions/CIK0000320193.json"  # zero-padded to 10 digits
    first, tenk, eightk = out["filings"][:3]
    assert first == {
        "form": "4", "filed": "2026-11-03", "report_date": "2026-10-31", "description": "Insider transaction (Form 4)",
        "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/xslF345X05/wk-form4_1762212345.xml",
    }
    assert tenk["url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101/aapl-20260926.htm"
    assert tenk["description"] == "Annual report"
    assert eightk["description"] == "Current report: results of operations (earnings)"
    assert out["filings"][3]["description"] == "Passive 5%+ ownership report (amendment)" and out["filings"][3]["report_date"] is None
    assert len(out["filings"]) == 5 and out["status"].startswith("Apple Inc. (AAPL): 5 recent filings, latest 4 on 2026-11-03")
    for r in router.requests:
        assert r.headers["user-agent"] == "ATLAS personal assistant (contact not configured)"


def test_sec_filings_form_filter_normalises_and_includes_amendments():
    router = sec_router()
    out, _ = call("get_sec_filings", router, ticker="AAPL", forms=["10k", "Form 10-Q"])
    # 'recent' had three; fewer than the limit, so the newest older page is searched too.
    assert [f["form"] for f in out["filings"]] == ["10-K", "10-Q", "10-K/A", "10-K"]
    assert out["filings"][-1] == {"form": "10-K", "filed": "2014-10-27", "report_date": "2014-09-27", "description": "Annual report",
                                  "url": "https://www.sec.gov/Archives/edgar/data/320193/000119312514383437/d783162d10k.htm"}
    assert router.hits("data.sec.gov")[-1].url.path == "/submissions/CIK0000320193-submissions-001.json"
    out, _ = call("get_sec_filings", sec_router(), ticker="AAPL", forms="proxy")
    assert [f["form"] for f in out["filings"]] == ["DEF 14A"]
    out, _ = call("get_sec_filings", sec_router(), ticker="AAPL", forms=["S-1"])
    assert out["filings"] == [] and "no S-1 filings" in out["status"]


def test_sec_contact_email_goes_in_user_agent(isolated):
    set_env(isolated, SEC_CONTACT_EMAIL="jane.doe@example.com")
    router = sec_router()
    call("get_sec_filings", router, ticker="AAPL")
    assert {r.headers["user-agent"] for r in router.requests} == {"ATLAS personal assistant jane.doe@example.com"}


def test_sec_403_tells_user_to_set_contact_email(isolated):
    blocked = httpx.Response(403, text="<html><h1>Your Request Originates from an Undeclared Automated Tool</h1></html>")
    out, raw = call("get_sec_filings", sec_router(tickers=blocked), ticker="AAPL")
    assert "SEC_CONTACT_EMAIL" in out["error"] and "Settings" in out["error"] and "<html" not in raw
    set_env(isolated, SEC_CONTACT_EMAIL="jane@example.com")
    out, _ = call("get_sec_filings", sec_router(tickers=blocked), ticker="AAPL")
    assert "10 requests a second" in out["error"] and "SEC_CONTACT_EMAIL" not in out["error"]


def test_sec_resolves_names_class_shares_and_ciks():
    router = sec_router(subs=lambda req: json_response(dict(APPLE_SUBMISSIONS, name="BERKSHIRE HATHAWAY INC", tickers=["BRK-B", "BRK-A"])))
    out, _ = call("get_sec_filings", router, ticker="BRK.B")
    assert out["cik"] == "0001067983" and router.hits("data.sec.gov")[0].url.path == "/submissions/CIK0001067983.json"
    out, _ = call("get_sec_filings", sec_router(), ticker="Apple")
    assert out["ticker"] == "AAPL"  # "Apple Inc." beats "Apple Hospitality REIT"
    ds._CACHE.clear()
    router = Router({"data.sec.gov": json_response(APPLE_SUBMISSIONS)})  # no ticker list needed for a CIK
    out, _ = call("get_sec_filings", router, ticker="CIK 320193")
    assert out["cik"] == "0000320193" and out["ticker"] == "AAPL" and len(router.requests) == 1
    assert ds._cik10(789019) == "0000789019" and ds._cik10("0000320193") == "0000320193"
    out, _ = call("get_sec_filings", sec_router(), ticker="NOTAREALCO")
    assert "isn't in the SEC's list" in out["error"]


def test_sec_ticker_list_is_cached():
    router = sec_router()
    call("get_sec_filings", router, ticker="AAPL")
    call("get_sec_filings", router, ticker="MSFT")
    assert len(router.hits("company_tickers.json")) == 1


# ---------------------------------------------------------------- XBRL fundamentals
FY_START = {2022: date(2021, 9, 26), 2023: date(2022, 9, 25), 2024: date(2023, 10, 1), 2025: date(2024, 9, 29), 2026: date(2025, 9, 28)}
FY_END = {2022: date(2022, 9, 24), 2023: date(2023, 9, 30), 2024: date(2024, 9, 28), 2025: date(2025, 9, 27), 2026: date(2026, 9, 26)}
REVENUE = {
    2023: [117_154_000_000, 94_836_000_000, 81_797_000_000, 89_498_000_000],
    2024: [119_575_000_000, 90_753_000_000, 85_777_000_000, 94_930_000_000],
    2025: [124_300_000_000, 95_359_000_000, 94_036_000_000, 102_466_000_000],
    2026: [138_000_000_000, 104_100_000_000, 101_200_000_000, 110_700_000_000],
}
NET_INCOME = {
    2023: [29_998_000_000, 24_160_000_000, 19_881_000_000, 22_956_000_000],
    2024: [33_916_000_000, 23_636_000_000, 21_448_000_000, 14_736_000_000],
    2025: [36_330_000_000, 24_780_000_000, 23_434_000_000, 27_466_000_000],
    2026: [40_100_000_000, 27_000_000_000, 25_500_000_000, 29_900_000_000],
}
EPS = {2023: [1.88, 1.52, 1.26, 1.46], 2024: [2.18, 1.53, 1.40, 0.97], 2025: [2.40, 1.65, 1.57, 1.85], 2026: [2.70, 1.83, 1.74, 2.03]}
OCF = {
    2023: [34_005_000_000, 28_560_000_000, 26_380_000_000, 21_598_000_000],
    2024: [39_895_000_000, 22_690_000_000, 28_858_000_000, 26_811_000_000],
    2025: [29_935_000_000, 23_952_000_000, 27_867_000_000, 29_728_000_000],
    2026: [41_000_000_000, 25_000_000_000, 30_000_000_000, 33_000_000_000],
}


def quarter_bounds(fy):
    s = FY_START[fy]
    ends = [s + timedelta(days=90), s + timedelta(days=181), s + timedelta(days=272), FY_END[fy]]
    starts = [s] + [e + timedelta(days=1) for e in ends[:3]]
    return starts, ends


def concept(tag, quarterly, unit="USD", ytd_only=False, extra=()):
    """Facts the way 10-Qs/10-Ks report them: 3-month figures (unless ytd_only, as for cash flow), 6- and
    9-month YTD figures, full years, and the prior-year comparatives every filing repeats."""
    facts = []

    def add(start, end, val, fy, fp, form, filed, accn, frame=None):
        f = {"start": start.isoformat(), "end": end.isoformat(), "val": round(val, 2) if unit != "USD" else int(val),
             "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed.isoformat()}
        if frame:
            f["frame"] = frame
        facts.append(f)

    for fy in sorted(quarterly):
        starts, ends = quarter_bounds(fy)
        for qi in range(3):
            accn = f"0000320193-{str(fy)[2:]}-0000{qi + 1:02d}"
            filed = ends[qi] + timedelta(days=35)
            for year in (fy, fy - 1):
                if year not in quarterly:
                    continue
                ys, ye = quarter_bounds(year)
                q = quarterly[year]
                if not ytd_only:
                    add(ys[qi], ye[qi], q[qi], fy, f"Q{qi + 1}", "10-Q", filed, accn)
                if qi > 0 or ytd_only:
                    add(FY_START[year], ye[qi], sum(q[: qi + 1]), fy, f"Q{qi + 1}", "10-Q", filed, accn)
        accn = f"0000320193-{str(fy)[2:]}-000099"
        filed = FY_END[fy] + timedelta(days=35)
        for year in (fy, fy - 1, fy - 2):
            if year in quarterly:
                add(FY_START[year], FY_END[year], sum(quarterly[year]), fy, "FY", "10-K", filed, accn, frame=f"CY{year - 1}")
    facts.extend(extra)
    return {"cik": 320193, "taxonomy": "us-gaap", "tag": tag, "label": tag, "description": f"{tag} description", "entityName": "Apple Inc.", "units": {unit: facts}}


NOISE = [  # an 8-K fact (earnings release) with a bogus value for the latest quarter: must be ignored
    {"start": "2026-06-28", "end": "2026-09-26", "val": 1, "accn": "0000320193-26-000098", "fy": 2026, "fp": "Q4", "form": "8-K", "filed": "2026-10-30"},
]
OLD_REVENUE = {  # the pre-2018 concept, retired long ago
    "cik": 320193, "taxonomy": "us-gaap", "tag": "SalesRevenueNet", "label": "Revenues", "entityName": "Apple Inc.",
    "units": {"USD": [{"start": "2016-09-25", "end": "2017-09-30", "val": 229_234_000_000, "accn": "0000320193-17-000070", "fy": 2017, "fp": "FY", "form": "10-K", "filed": "2017-11-03"}]},
}


def full_concepts(revenue_tag="RevenueFromContractWithCustomerExcludingAssessedTax"):
    return {
        revenue_tag: concept(revenue_tag, REVENUE, extra=NOISE),
        "NetIncomeLoss": concept("NetIncomeLoss", NET_INCOME),
        "EarningsPerShareDiluted": concept("EarningsPerShareDiluted", EPS, unit="USD/shares"),
        "NetCashProvidedByUsedInOperatingActivities": concept("NetCashProvidedByUsedInOperatingActivities", OCF, ytd_only=True),
    }


def m(v):
    return round(v / 1e6, 1)


def growth(new, old):
    return round((new - old) / abs(old) * 100, 1)


def test_parse_concept_dedupes_frames_and_never_counts_ytd_as_quarters():
    parsed = ds.parse_concept(concept("Revenues", REVENUE, extra=NOISE))
    vals = [p["val"] for p in parsed["quarters"]]
    expected = [v for fy in sorted(REVENUE) for v in REVENUE[fy]]
    assert vals == expected  # 16 discrete quarters: comparatives deduped, 6M/9M totals never treated as quarters
    q4s = [p for p in parsed["quarters"] if p["end"] in FY_END.values()]
    assert all(p["derived"] for p in q4s) and len(q4s) == 4  # Q4 = FY - 9M (no 10-Q for Q4; the 8-K fact is ignored)
    assert [p["val"] for p in parsed["annuals"]] == [sum(REVENUE[fy]) for fy in sorted(REVENUE)]
    assert parsed["labels"][FY_END[2026]] == (2026, "FY") and parsed["labels"][quarter_bounds(2026)[1][1]] == (2026, "Q2")
    assert parsed["latest_end"] == FY_END[2026]


def test_parse_concept_derives_quarters_from_ytd_cash_flow():
    parsed = ds.parse_concept(concept("NetCashProvidedByUsedInOperatingActivities", OCF, ytd_only=True))
    assert [p["val"] for p in parsed["quarters"]] == [v for fy in sorted(OCF) for v in OCF[fy]]
    q1, q2 = parsed["quarters"][-4], parsed["quarters"][-3]
    assert q1["derived"] is False and q2["derived"] is True  # Q1 is itself a 3-month YTD figure; Q2 = 6M - 3M


def test_parse_concept_prefers_latest_filed_restatement():
    s, e = quarter_bounds(2025)[0][0], quarter_bounds(2025)[1][0]
    facts = [
        {"start": s.isoformat(), "end": e.isoformat(), "val": 100, "accn": "a1", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-01-31"},
        {"start": s.isoformat(), "end": e.isoformat(), "val": 104, "accn": "a2", "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-01-30"},
        {"start": s.isoformat(), "end": e.isoformat(), "val": 999, "accn": "a3", "fy": 2025, "fp": "Q1", "form": "S-4", "filed": "2026-03-01"},
        {"start": s.isoformat(), "end": (e + timedelta(days=370)).isoformat(), "val": 5, "accn": "a2", "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-01-30"},
    ]
    parsed = ds.parse_concept({"tag": "X", "units": {"USD": facts}})
    assert [p["val"] for p in parsed["quarters"]] == [104]
    assert parsed["labels"][e] == (2025, "Q1")  # a2 is about FY2026 Q1: its comparative doesn't relabel the period


def test_yoy_maths():
    def p(end, val):
        return {"end": end, "val": val}

    hist = [p(date(2025, 6, 28), -50.0), p(date(2025, 9, 27), 0.0), p(date(2025, 12, 27), 80.0)]
    assert ds.yoy_pct(hist, p(date(2026, 6, 27), 25.0)) == 150.0  # loss to profit: abs() denominator
    assert ds.yoy_pct(hist, p(date(2026, 9, 26), 10.0)) is None  # prior zero
    assert ds.yoy_pct(hist, p(date(2026, 12, 26), 100.0)) == 25.0
    assert ds.yoy_pct(hist, p(date(2026, 3, 28), 1.0)) is None  # nothing a year earlier


def test_company_financials_end_to_end():
    router = sec_router(concepts=full_concepts())
    out, raw = call("get_company_financials", router, ticker="AAPL")
    assert out["company"] == "Apple Inc." and out["cik"] == "0000320193"
    paths = {r.url.path for r in router.hits("data.sec.gov")}
    assert "/api/xbrl/companyconcept/CIK0000320193/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json" in paths
    q = out["quarters"]
    assert len(q) == 8 and [r["period"] for r in q[:5]] == ["FY2026 Q4", "FY2026 Q3", "FY2026 Q2", "FY2026 Q1", "FY2025 Q4"]
    latest = q[0]
    assert latest["end"] == "2026-09-26"
    assert latest["revenue"] == m(110_700_000_000) and latest["revenue_yoy_pct"] == growth(110.7e9, 102.466e9)
    assert latest["net_income"] == m(29_900_000_000) and latest["net_income_yoy_pct"] == growth(29.9e9, 27.466e9)
    assert latest["eps_diluted"] == 2.03 and latest["eps_diluted_yoy_pct"] == growth(2.03, 1.85)
    assert latest["operating_cash_flow"] == m(33_000_000_000) and latest["operating_cash_flow_yoy_pct"] == growth(33e9, 29.728e9)
    assert set(latest["derived"]) == {"revenue", "net_income", "eps_diluted", "operating_cash_flow"}
    q2 = q[2]
    assert q2["revenue"] == m(104_100_000_000) and q2["derived"] == ["operating_cash_flow"]  # 3-month figure filed; OCF from YTD
    assert q2["operating_cash_flow"] == m(25_000_000_000)
    assert q[-1]["period"] == "FY2025 Q1" and q[-1]["revenue_yoy_pct"] == growth(124.3e9, 119.575e9)
    ytd6 = m(sum(REVENUE[2026][:2]))
    assert all(r["revenue"] != ytd6 for r in q)
    years = out["fiscal_years"]
    assert [y["period"] for y in years] == ["FY2026", "FY2025", "FY2024"]
    assert years[0]["revenue"] == m(sum(REVENUE[2026])) and years[0]["revenue_yoy_pct"] == growth(sum(REVENUE[2026]), sum(REVENUE[2025]))
    assert years[0]["eps_diluted"] == round(sum(EPS[2026]), 2)
    assert "derived" not in years[0]
    assert out["status"].startswith("Apple Inc. FY2026 Q4, revenue $110.70B (+8.0% YoY), net income $29.90B, EPS $2.03")
    assert "note" in out and "missing" not in out and len(raw) < 7000


def test_company_financials_revenue_concept_fallbacks():
    concepts = full_concepts("Revenues")  # no RevenueFromContract... (404)
    router = sec_router(concepts=concepts)
    out, _ = call("get_company_financials", router, ticker="AAPL")
    assert out["concepts"]["revenue"] == "Revenues" and out["quarters"][0]["revenue"] == m(110_700_000_000)
    # A retired concept (only 2017 data) is passed over for a fresher one.
    concepts = full_concepts("Revenues")
    concepts["RevenueFromContractWithCustomerExcludingAssessedTax"] = dict(OLD_REVENUE, tag="RevenueFromContractWithCustomerExcludingAssessedTax")
    concepts["SalesRevenueNet"] = OLD_REVENUE
    out, _ = call("get_company_financials", sec_router(concepts=concepts), ticker="AAPL")
    assert out["concepts"]["revenue"] == "Revenues"


def test_company_financials_no_xbrl_and_errors():
    out, _ = call("get_company_financials", sec_router(concepts={}), ticker="AAPL")
    assert "no US-GAAP financial data" in out["error"]
    ds._CACHE.clear()  # missing concepts (404) are cached for hours
    concepts = {k: httpx.Response(503) for k in full_concepts()}
    out, _ = call("get_company_financials", sec_router(concepts=concepts), ticker="AAPL")
    assert "SEC EDGAR is unavailable right now (HTTP 503)" in out["error"]
    concepts = full_concepts()
    del concepts["EarningsPerShareDiluted"]
    out, _ = call("get_company_financials", sec_router(concepts=concepts), ticker="AAPL")
    assert out["missing"] == ["eps_diluted"] and "eps_diluted" not in out["quarters"][0] and out["quarters"][0]["revenue"] is not None


# =========================================================================== Nasdaq earnings
HEADERS = {"time": "Time", "symbol": "Symbol", "name": "Company Name", "marketCap": "Market Cap", "fiscalQuarterEnding": "Fiscal Quarter Ending",
           "epsForecast": "Consensus EPS* Forecast", "noOfEsts": "# of Ests", "lastYearRptDt": "Last Year's Report Date", "lastYearEPS": "Last year's EPS*"}


def nasdaq_day(rows, as_of="Wed, Nov 4, 2026"):
    return {"data": {"asOf": as_of, "headers": HEADERS, "rows": rows}, "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


def er(symbol, name, mcap, time_, eps, ests, last_eps, last_date="11/6/2025"):
    return {"lastYearRptDt": last_date, "lastYearEPS": last_eps, "time": time_, "symbol": symbol, "name": name, "marketCap": mcap,
            "fiscalQuarterEnding": "Sep/2026", "epsForecast": eps, "noOfEsts": ests}


NOV4 = nasdaq_day([
    er("LCID", "Lucid Group, Inc.", "$7,023,456,789", "time-pre-market", "($0.25)", "8", "($0.28)"),
    er("QCOM", "QUALCOMM Incorporated", "$185,123,456,789", "time-after-hours", "$2.87", "11", "$2.69"),
    er("TINY", "Tiny Corp", "N/A", "time-not-supplied", "", "N/A", "N/A", "N/A"),
    er("ARM", "Arm Holdings plc American Depositary Shares", "$160,987,654,321", "time-after-hours", "$0.33", "14", "$0.30"),
])


def test_earnings_calendar_sorted_by_market_cap():
    router = Router({"api.nasdaq.com/api/calendar/earnings": json_response(NOV4)})
    out, _ = call("get_earnings_calendar", router, date="2026-11-04")
    assert [r["symbol"] for r in out["earnings"]] == ["QCOM", "ARM", "LCID", "TINY"]
    assert out["date"] == "2026-11-04" and out["earnings"][0] == {  # one day scanned: the date is top-level only
        "symbol": "QCOM", "company": "QUALCOMM Incorporated", "time": "after market close",
        "eps_forecast": 2.87, "estimates": 11, "last_year_eps": 2.69, "last_year_report_date": "2025-11-06",
        "fiscal_quarter": "Sep/2026", "market_cap": "$185.12B",
    }
    lcid, tiny = out["earnings"][2], out["earnings"][3]
    assert lcid["eps_forecast"] == -0.25 and lcid["last_year_eps"] == -0.28 and lcid["time"] == "before market open"
    assert tiny["eps_forecast"] is None and tiny["estimates"] is None and tiny["market_cap"] is None and tiny["time"] == "not specified"
    assert out["status"] == "4 companies report earnings on Wed Nov 4, 2026" and out["count"] == 4
    req = router.requests[0]
    assert req.url.params["date"] == "2026-11-04" and "Mozilla/5.0" in req.headers["user-agent"]
    assert "application/json" in req.headers["accept"]


def test_earnings_calendar_limit_and_weekend():
    router = Router({"api.nasdaq.com/api/calendar/earnings": json_response(NOV4)})
    out, _ = call("get_earnings_calendar", router, date="2026-11-04", limit=2)
    assert len(out["earnings"]) == 2 and out["count"] == 4 and "the 2 largest shown" in out["status"]
    weekend = Router({"api.nasdaq.com/api/calendar/earnings": json_response(nasdaq_day(None, "Sat, Nov 7, 2026"))})
    out, _ = call("get_earnings_calendar", weekend, date="2026-11-07")
    assert out["earnings"] == [] and out["status"] == "No earnings scheduled on Sat Nov 7, 2026 (weekend)"


def test_earnings_calendar_symbol_search_across_weekdays():
    days = {
        "2026-11-06": nasdaq_day([er("AAPL", "Apple Inc.", "$4,000,000,000,000", "time-after-hours", "$1.77", "25", "$1.64")]),
        "2026-11-09": nasdaq_day([]),
        "2026-11-10": nasdaq_day([er("NVDA", "NVIDIA Corporation", "$4,912,345,678,901", "time-after-hours", "$1.25", "38", "$0.81"), er("XYZ", "Block", "$40,000,000,000", "time-after-hours", "$0.90", "30", "$0.88")]),
    }
    router = Router({"api.nasdaq.com/api/calendar/earnings": lambda req: json_response(days[req.url.params["date"]])})
    out, _ = call("get_earnings_calendar", router, date="2026-11-06", days=3, symbols=["$nvda", "TSLA"])
    assert out["dates_scanned"] == ["2026-11-06", "2026-11-09", "2026-11-10"]  # weekend skipped
    assert [r["symbol"] for r in out["earnings"]] == ["NVDA"] and out["earnings"][0]["date"] == "2026-11-10"
    assert out["status"] == "NVDA reports 2026-11-10 (after market close)" and out["not_found"] == ["TSLA"]


def test_earnings_calendar_failures_are_friendly():
    out, raw = call("get_earnings_calendar", Router({"api.nasdaq.com": httpx.Response(403, text="<html>Access Denied</html>")}), date="2026-11-04")
    assert "Nasdaq's earnings calendar" in out["error"] and "web_search" in out["error"] and "<html" not in raw

    def slow(request):
        raise httpx.ReadTimeout("timed out")

    out, _ = call("get_earnings_calendar", Router({"api.nasdaq.com": slow}), date="2026-11-04")
    assert out["error"] == "Nasdaq's earnings calendar didn't answer in time. Try again in a moment."
    bad = {"data": None, "message": None, "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Invalid date"}]}}
    out, _ = call("get_earnings_calendar", Router({"api.nasdaq.com": json_response(bad)}), date="2026-11-04")
    assert "couldn't answer for 2026-11-04" in out["error"]
    out, _ = call("get_earnings_calendar", no_network, date="sometime soon")
    assert "YYYY-MM-DD" in out["error"]


def test_parse_day():
    wed = date(2026, 11, 4)
    assert ds.parse_day("", wed) == wed and ds.parse_day("Tomorrow", wed) == date(2026, 11, 5)
    assert ds.parse_day("friday", wed) == date(2026, 11, 6) and ds.parse_day("wednesday", wed) == wed
    assert ds.parse_day("next wednesday", wed) == date(2026, 11, 11) and ds.parse_day("monday", wed) == date(2026, 11, 9)
    assert ds.parse_day("2026-12-01", wed) == date(2026, 12, 1) and ds.parse_day("11/05/2026", wed) == date(2026, 11, 5)


# =========================================================================== CNN Fear & Greed + VIX
def fg_component(score, rating, n=250):
    return {"timestamp": 1_793_893_452_000.0, "score": score, "rating": rating,
            "data": [{"x": 1_762_214_400_000.0 + i * 86_400_000, "y": 40 + i % 30, "rating": "neutral"} for i in range(n)]}


CNN_FG = {
    "fear_and_greed": {"score": 62.3428571428571, "rating": "greed", "timestamp": "2026-11-04T15:44:12+00:00", "previous_close": 58.1714285714286,
                       "previous_1_week": 44.6, "previous_1_month": 71.2285714285714, "previous_1_year": 21.8},
    "fear_and_greed_historical": fg_component(62.34, "greed"),
    "market_momentum_sp500": fg_component(78.4, "extreme greed"),
    "market_momentum_sp125": fg_component(74.0, "greed"),
    "stock_price_strength": fg_component(55.2, "neutral"),
    "stock_price_breadth": fg_component(48.0, "neutral"),
    "put_call_options": fg_component(65.0, "greed"),
    "market_volatility_vix": fg_component(50.0, "neutral"),
    "market_volatility_vix_50": fg_component(50.0, "neutral"),
    "junk_bond_demand": fg_component(70.1, "greed"),
    "safe_haven_demand": fg_component(80.3, "extreme greed"),
}


def sentiment_router(cnn=None, vix=None):
    return Router({
        "production.dataviz.cnn.io/index/fearandgreed/graphdata": cnn or json_response(CNN_FG),
        "query1.finance.yahoo.com/v8/finance/chart/^VIX": vix or yahoo_chart("^VIX", 14.23, 15.01),
    })


def test_market_sentiment_fear_greed_and_vix():
    router = sentiment_router()
    out, raw = call("get_market_sentiment", router)
    fg = out["fear_greed"]
    assert fg["score"] == 62 and fg["rating"] == "greed" and fg["as_of"] == "2026-11-04T15:44:12+00:00"
    assert fg["previous_close"] == {"score": 58, "rating": "greed"} and fg["one_week_ago"] == {"score": 45, "rating": "neutral"}
    assert fg["one_month_ago"] == {"score": 71, "rating": "greed"} and fg["one_year_ago"] == {"score": 22, "rating": "extreme fear"}
    assert len(fg["components"]) == 7 and fg["components"][0] == {"name": "Market momentum (S&P 500 vs its 125-day average)", "score": 78, "rating": "extreme greed"}
    assert out["vix"]["value"] == 14.23 and out["vix"]["level"] == "calm / normal" and out["vix"]["change"] == -0.78
    assert out["status"] == "Fear & Greed 62/100 (greed) · VIX 14.2 (calm / normal)"
    assert len(raw) < 3000  # the long history arrays are dropped
    cnn_req = router.hits("dataviz.cnn.io")[0]
    assert "Mozilla/5.0" in cnn_req.headers["user-agent"] and cnn_req.headers["referer"].startswith("https://edition.cnn.com")


def test_market_sentiment_cnn_blocked_falls_back_to_vix():
    out, _ = call("get_market_sentiment", sentiment_router(cnn=httpx.Response(418, text="I'm a teapot")))
    assert out["fear_greed"] is None and out["vix"]["value"] == 14.23
    assert out["status"] == "VIX 14.2 (calm / normal)" and "unofficial" in out["unavailable"][0]


def test_market_sentiment_everything_down():
    router = Router({"production.dataviz.cnn.io": httpx.Response(500), "query1.finance.yahoo.com": httpx.Response(500), "query2.finance.yahoo.com": httpx.Response(500)})
    out, _ = call("get_market_sentiment", router)
    assert out["error"].startswith("Market sentiment data is unavailable right now") and "CNN" in out["error"]


def test_fg_bands_and_vix_levels():
    assert [ds.fg_band(s) for s in (0, 24.4, 25, 44, 45, 55, 56, 75, 76, 100)] == [
        "extreme fear", "extreme fear", "fear", "fear", "neutral", "neutral", "greed", "greed", "extreme greed", "extreme greed"]
    assert [ds.vix_level(v) for v in (10, 15, 25, 40)] == ["very calm", "calm / normal", "elevated (nervous)", "high (fearful)"]


def test_sentiment_pulse_never_raises_and_caches():
    good = call_fn(ds.sentiment_pulse, sentiment_router())
    assert good["fear_greed"] == {"score": 62, "rating": "greed", "previous_close": {"score": 58, "rating": "greed"}, "one_week_ago": {"score": 45, "rating": "neutral"}}
    assert good["vix"]["value"] == 14.23 and call_fn(ds.sentiment_pulse, no_network) == good
    ds._CACHE.clear()
    markets._CACHE.clear()  # the VIX quote is cached by the markets module

    def boom(request):
        raise httpx.ConnectError("no route")

    bad = call_fn(ds.sentiment_pulse, Router({"production.dataviz.cnn.io": boom, "query1.finance.yahoo.com": boom, "query2.finance.yahoo.com": boom}))
    assert bad["fear_greed"] is None and bad["vix"] is None and bad["error"]


# =========================================================================== FRED
def monthly(start_year, start_month, values):
    out, y, mo = [], start_year, start_month
    for v in values:
        out.append((date(y, mo, 1), v))
        mo += 1
        if mo > 12:
            y, mo = y + 1, 1
    return out


CPI_VALUES = [round(300 * (1.0025 ** i) + (0.3 if i % 5 == 0 else 0), 3) for i in range(37)]  # Sep 2023 .. Sep 2026
CPI = monthly(2023, 9, CPI_VALUES)
CPI_WITH_GAP = [(d, v) for d, v in CPI if d != date(2025, 10, 1)]  # Oct 2025 CPI was never published


def fred_csv(series_id, obs, missing=(), header="observation_date"):
    lines = [f"{header},{series_id}"]
    for d, v in obs:
        lines.append(f"{d.isoformat()},{'' if d in missing else v}")
    return "\n".join(lines) + "\n"


def fred_json(obs, limit=40):
    rows = [{"realtime_start": "2026-11-04", "realtime_end": "2026-11-04", "date": d.isoformat(), "value": f"{v:.3f}"} for d, v in sorted(obs, reverse=True)[:limit]]
    rows.insert(3, {"realtime_start": "2026-11-04", "realtime_end": "2026-11-04", "date": "2025-10-01", "value": "."})
    return {"realtime_start": "2026-11-04", "realtime_end": "2026-11-04", "observation_start": "1600-01-01", "observation_end": "9999-12-31",
            "units": "lin", "output_type": 1, "file_type": "json", "order_by": "observation_date", "sort_order": "desc",
            "count": 945, "offset": 0, "limit": limit, "observations": rows}


def fred_router(csv_series: dict, api_series: dict | None = None, api_response=None):
    def csv_handler(request):
        sid = request.url.params["id"]
        body = csv_series.get(sid)
        if body is None:
            return httpx.Response(404, text="<!DOCTYPE html><html><body>Series not found</body></html>")
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, text=body, headers={"content-type": "text/csv"})

    def api_handler(request):
        if api_response is not None:
            return api_response(request) if callable(api_response) else api_response
        sid = request.url.params["series_id"]
        obs = (api_series or {}).get(sid)
        if obs is None:
            return json_response({"error_code": 400, "error_message": "Bad Request.  The series does not exist."}, 400)
        return json_response(fred_json(obs, int(request.url.params["limit"])))

    return Router({"fred.stlouisfed.org/graph/fredgraph.csv": csv_handler, "api.stlouisfed.org/fred/series/observations": api_handler})


def expected_yoy(obs, i=-1, nd=1):
    """Inflation is shown to one decimal, as BLS/BEA publish it."""
    by = dict(obs)
    d, v = obs[i]
    return round((v / by[d.replace(year=d.year - 1)] - 1) * 100, nd)


def test_cpi_yoy_from_public_csv():
    router = fred_router({"CPIAUCSL": fred_csv("CPIAUCSL", CPI)})
    out, raw = call("get_economic_data", router, indicator="inflation")
    assert out["indicator"] == "cpi" and out["series_id"] == "CPIAUCSL" and out["unit"] == "% YoY"
    assert out["value"] == expected_yoy(CPI) and out["period"] == "Sep 2026" and out["date"] == "2026-09-01"
    assert out["previous"] == expected_yoy(CPI, -2) and out["previous_period"] == "Aug 2026"
    assert out["change"] == round(out["value"] - out["previous"], 1)
    assert out["value"] == round(out["value"], 1)
    assert [t["period"] for t in out["trend"]] == ["Apr 2026", "May 2026", "Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026"]
    assert out["status"] == f"CPI inflation: {out['value']}% YoY (Sep 2026)"
    assert out["source"] == "FRED (St. Louis Fed), series CPIAUCSL (FRED public CSV)" and "seasonally adjusted" in out["note"]
    req = router.requests[0]
    assert req.url.params["id"] == "CPIAUCSL" and "cosd" in req.url.params and "api.stlouisfed.org" not in str(req.url)


def test_fred_csv_and_api_give_identical_results(isolated):
    csv_out, _ = call("get_economic_data", fred_router({"CPIAUCSL": fred_csv("CPIAUCSL", CPI_WITH_GAP, header="DATE")}), indicator="cpi")
    ds._CACHE.clear()
    set_env(isolated, FRED_API_KEY="abcdef0123456789abcdef0123456789")
    router = fred_router({}, api_series={"CPIAUCSL": CPI_WITH_GAP})
    api_out, raw = call("get_economic_data", router, indicator="cpi")
    params = router.requests[0].url.params
    assert params["api_key"] == "abcdef0123456789abcdef0123456789" and params["file_type"] == "json" and params["sort_order"] == "desc"
    assert "abcdef0123456789abcdef0123456789" not in raw and api_out["source"].endswith("(FRED API)")
    strip = lambda o: {k: v for k, v in o.items() if k not in ("source", "source_note")}  # noqa: E731
    assert strip(csv_out) == strip(api_out)
    assert csv_out["value"] == expected_yoy(CPI)


def test_fred_bad_key_falls_back_to_csv_without_leaking(isolated):
    key = "deadbeefdeadbeefdeadbeefdeadbeef"
    set_env(isolated, FRED_API_KEY=key)
    bad_key = json_response({"error_code": 400, "error_message": f"Bad Request.  The value for variable api_key is not registered. ({key})"}, 400)
    out, raw = call("get_economic_data", fred_router({"UNRATE": fred_csv("UNRATE", monthly(2024, 1, [3.7, 3.9, 3.9, 3.9, 4.0, 4.1] * 4))}, api_response=bad_key), indicator="unemployment")
    assert out["value"] == 4.1 and out["unit"] == "%" and out["change"] == 0.1
    assert "FRED_API_KEY" in out["source_note"] and "public CSV" in out["source_note"] and key not in raw


def test_fred_network_errors_never_leak_the_key(isolated):
    key = "0123456789abcdef0123456789abcdef"
    set_env(isolated, FRED_API_KEY=key)

    def leaky(request):
        raise httpx.ConnectError(f"[Errno 111] Connection refused for {request.url}")

    router = fred_router({"UNRATE": httpx.Response(503)}, api_response=leaky)
    out, raw = call("get_economic_data", router, indicator="unemployment")
    assert "FRED" in out["error"] and key not in raw and "api_key" not in raw
    assert key in str(router.requests[0].url)  # the key really was in the URL that failed
    assert ds._scrub(f"x?api_key={key}&y=1") == "x?api_key=***&y=1"


def test_payrolls_monthly_change_and_retail_sales_mom():
    payems = monthly(2026, 1, [159_000, 159_120, 159_374, 159_400, 159_380, 159_450, 159_520, 159_542, 159_661])
    retail = monthly(2026, 1, [720_000.0, 723_600.0, 721_000.0, 725_000.0, 730_800.0, 728_000.0, 732_000.0, 735_660.0, 737_131.3])
    router = fred_router({"PAYEMS": fred_csv("PAYEMS", payems), "RSAFS": fred_csv("RSAFS", retail)})
    out, _ = call("get_economic_data", router, indicator="nonfarm payrolls")
    assert out["value"] == 119 and out["previous"] == 22 and out["change"] == 97 and out["unit"] == "thousand jobs"
    assert [t["value"] for t in out["trend"]] == [26, -20, 70, 70, 22, 119]
    assert out["status"] == "Nonfarm payrolls, monthly change: +119K jobs (Sep 2026)"
    out, _ = call("get_economic_data", router, indicator="retail sales")
    assert out["value"] == round((737_131.3 / 735_660.0 - 1) * 100, 1) and out["unit"] == "% MoM"


def test_monthly_change_skips_a_missing_month():
    obs = monthly(2025, 7, [100.0, 101.0, 102.0, 999.0, 104.0, 105.0])
    gap = [(d, v) for d, v in obs if d != date(2025, 10, 1)]
    out = ds.transform_series(gap, "diff", 45)
    assert [(d.month, v) for d, v in out] == [(8, 1.0), (9, 1.0), (12, 1.0)]  # Sep->Nov isn't a one-month change


def test_fed_funds_with_target_range_and_monthly_fallback():
    daily = [(date(2026, 10, 26) + timedelta(days=i), 3.87 if i < 4 else 3.88) for i in range(8)]
    lower = [(date(2026, 10, 29) + timedelta(days=i), 3.75) for i in range(5)]
    upper = [(date(2026, 10, 29) + timedelta(days=i), 4.0) for i in range(5)]
    router = fred_router({"DFF": fred_csv("DFF", daily), "DFEDTARL": fred_csv("DFEDTARL", lower), "DFEDTARU": fred_csv("DFEDTARU", upper)})
    out, _ = call("get_economic_data", router, indicator="fed funds rate")
    assert out["value"] == 3.88 and out["date"] == "2026-11-02" and out["target_range"] == "3.75-4.00%" and out["frequency"] == "daily"
    ds._CACHE.clear()
    router = fred_router({"DFF": httpx.Response(500), "FEDFUNDS": fred_csv("FEDFUNDS", monthly(2026, 4, [4.33, 4.33, 4.33, 4.33, 4.22, 4.09, 3.87]))})
    out, _ = call("get_economic_data", router, indicator="fed_funds")
    assert out["series_id"] == "FEDFUNDS" and out["value"] == 3.87 and out["period"] == "Oct 2026" and out["target_range"] is None


def test_economy_overview_dashboard_with_partial_failure():
    csvs = {
        "CPIAUCSL": fred_csv("CPIAUCSL", CPI),
        "CPILFESL": fred_csv("CPILFESL", monthly(2023, 9, [v * 1.04 for v in CPI_VALUES])),
        "UNRATE": fred_csv("UNRATE", monthly(2026, 1, [4.0, 4.1, 4.2, 4.2, 4.3, 4.3, 4.3, 4.3, 4.4])),
        "PAYEMS": fred_csv("PAYEMS", monthly(2026, 1, [159_000, 159_100, 159_150, 159_200, 159_250, 159_300, 159_310, 159_330, 159_380])),
        "DFF": fred_csv("DFF", [(date(2026, 10, 27) + timedelta(days=i), 3.88) for i in range(6)]),
        "A191RL1Q225SBEA": fred_csv("A191RL1Q225SBEA", [(date(2025, 1, 1), -0.5), (date(2025, 4, 1), 3.8), (date(2025, 7, 1), 3.4), (date(2025, 10, 1), 1.9), (date(2026, 1, 1), 2.2), (date(2026, 4, 1), 2.6)]),
        "DGS10": fred_csv("DGS10", [(date(2026, 10, 26), 4.05), (date(2026, 10, 27), 4.08), (date(2026, 10, 28), ""), (date(2026, 10, 29), 4.11)], missing=(date(2026, 10, 28),)),
        "MORTGAGE30US": fred_csv("MORTGAGE30US", [(date(2026, 10, 15), 6.21), (date(2026, 10, 22), 6.17), (date(2026, 10, 29), 6.12)]),
        "UMCSENT": httpx.Response(500),
        "ICSA": fred_csv("ICSA", [(date(2026, 10, 17), 228_000), (date(2026, 10, 24), 219_000)]),
    }
    out, raw = call("get_economic_data", fred_router(csvs), indicator="overview")
    names = [i["indicator"] for i in out["indicators"]]
    assert names == ["cpi", "core_cpi", "unemployment", "payrolls", "fed_funds", "gdp_growth", "10y_treasury", "mortgage_30y", "jobless_claims"]
    assert out["unavailable"] == [{"indicator": "consumer_sentiment", "error": "FRED (St. Louis Fed) is unavailable right now (HTTP 500). Try again later."}]
    gdp = out["indicators"][5]
    assert gdp == {"indicator": "gdp_growth", "name": "Real GDP growth (annualized, quarter over quarter)", "value": 2.6, "unit": "%",
                   "period": "2026 Q2", "previous": 2.2, "change": 0.4, "trend": [-0.5, 3.8, 3.4, 1.9, 2.2, 2.6]}
    assert out["indicators"][6]["value"] == 4.11 and out["indicators"][6]["previous"] == 4.08  # blank (missing) day skipped
    assert out["indicators"][8]["value"] == 219_000 and out["indicators"][8]["change"] == -9000
    assert out["status"].startswith(f"US economy: CPI {expected_yoy(CPI)}% YoY, core CPI")
    assert "unemployment 4.4%" in out["status"] and len(raw) < 6000


def test_economy_unknown_indicator_and_raw_series():
    router = fred_router({"T10Y2Y": fred_csv("T10Y2Y", [(date(2026, 10, 28), 0.52), (date(2026, 10, 29), 0.55), (date(2026, 10, 30), 0.57)])})
    out, _ = call("get_economic_data", router, indicator="T10Y2Y")
    assert out["series_id"] == "T10Y2Y" and out["value"] == 0.57 and out["change"] == 0.02 and out["link"] == "https://fred.stlouisfed.org/series/T10Y2Y"
    out, _ = call("get_economic_data", router, indicator="NOPE123")
    assert out["error"].startswith("Unknown indicator 'NOPE123'") and "cpi" in out["error"]
    out, _ = call("get_economic_data", no_network, indicator="what's up?!")
    assert out["error"].startswith("Unknown indicator")


def test_fred_csv_parser_handles_legacy_and_missing_values():
    text = "﻿DATE,ICSA\n2026-10-10,231000\n2026-10-17,.\n2026-10-24,219000\n"
    assert ds.parse_fred_csv(text, "ICSA") == [(date(2026, 10, 10), 231000.0), (date(2026, 10, 24), 219000.0)]
    assert ds.parse_fred_csv("", "X") == [] and ds.parse_fred_csv("<html>busy</html>", "X") == []
    out, _ = call("get_economic_data", fred_router({"UNRATE": "<!DOCTYPE html><html><body>Service busy</body></html>"}), indicator="unemployment")
    assert "web page instead of data" in out["error"]


def test_fred_results_cached():
    router = fred_router({"UNRATE": fred_csv("UNRATE", monthly(2026, 1, [4.0, 4.1, 4.2]))})
    call("get_economic_data", router, indicator="unemployment")
    call("get_economic_data", router, indicator="unemployment rate")
    assert len(router.requests) == 1


# =========================================================================== misc helpers
def test_number_parsing_and_formatting():
    assert ds._num("$2,939,676,340,770") == 2_939_676_340_770 and ds._num("($0.12)") == -0.12 and ds._num("-$1.5") == -1.5
    assert ds._num("N/A") is None and ds._num("") is None and ds._num(True) is None and ds._num(float("nan")) is None
    assert ds._compact(1_923_456_789_012) == "$1.92T" and ds._compact(-2_500_000, "€") == "-€2.50M" and ds._compact(950) == "$950"
    assert ds._price(0.00001234567) == 1.23457e-05 and ds._price(97123.456) == 97123.46


# =========================================================================== adversarial-review regressions
def test_crypto_overview_fallback_is_labelled_usd_not_requested_currency():
    router = Router({"api.coingecko.com": json_response({"status": {"error_code": 429}}, 429)})
    for sym in ds.YAHOO_DEFAULT:
        router.add(f"query1.finance.yahoo.com/v8/finance/chart/{sym}-USD", yahoo_chart(f"{sym}-USD", 97000.0, 95000.0))
    out, _ = call("get_crypto", router, top=2, currency="eur")
    assert out["currency"] == "USD" and "€" not in out["status"] and "BTC $97,000" in out["status"]
    assert "00:00 UTC" in out["source"]  # Yahoo's daily change isn't a rolling 24h change


def test_crypto_unsupported_currency_not_blamed_on_key_and_no_usd_fallback(isolated):
    set_env(isolated, COINGECKO_API_KEY="CG-demoKEY123456")
    router = cg_router(markets=json_response({"error": "invalid vs_currency"}, 400))
    out, raw = call("get_crypto", router, query="bitcoin", currency="xyz")
    assert out["error"].startswith("CoinGecko doesn't quote prices in XYZ") and "API key" not in out["error"]
    assert not router.hits("yahoo") and "CG-demoKEY123456" not in raw
    out, _ = call("get_crypto", cg_router(markets=json_response({"error": "invalid vs_currency"}, 400)), currency="xyz")
    assert "doesn't quote prices in XYZ" in out["error"]


def test_crypto_query_wording_models_actually_send():
    router = cg_router()
    out, _ = call("get_crypto", router, query="What's the bitcoin price today?")
    assert [c["symbol"] for c in out["coins"]] == ["BTC"] and not router.hits("/search?")  # no search for "bitcoin price"
    ds._CACHE.clear()  # (that BTC price is cached for a minute)
    router = cg_router()
    out, _ = call("get_crypto", router, query="BTC/USD")
    assert router.hits("/coins/markets")[0].url.params["ids"] == "bitcoin"  # one coin, not BTC and "USD"
    out, _ = call("get_crypto", cg_router(), query="trending coins")
    assert "coins" not in out and out["trending"]
    ds._CACHE.clear()
    router = cg_router()
    out, _ = call("get_crypto", router, query="top 3 coins by market cap")
    assert router.hits("/coins/markets")[0].url.params["per_page"] == "3" and "global" in out
    assert ds._split_terms("usd coin, binance coin") == ["usd coin", "binance coin"]  # names containing "coin" kept
    assert ds._split_terms(["ETH-USDT", "sol"]) == ["ETH", "sol"]


def test_small_prices_never_use_scientific_notation():
    assert [ds._fmt_price(v) for v in (0.00001234567, 0.5, 0.0312, 0.999, 1.5, 97123.0, 0)] == [
        "$0.00001235", "$0.50", "$0.0312", "$0.999", "$1.50", "$97,123", "$0"]


def test_sec_13g_filters_match_old_and_new_form_names():
    subs = submissions([
        ("0001104659-26-000001", "2026-02-10", "", "SCHEDULE 13G", "", "tm2601_sched13g.htm", "SCHEDULE 13G"),
        ("0001140361-24-000002", "2024-02-13", "", "SC 13G/A", "", "ef2024_sc13ga.htm", "SC 13G/A"),
        ("0000320193-26-000077", "2026-08-01", "2026-06-27", "10-Q", "", "aapl-20260627.htm", "10-Q"),
    ])
    for spelling in ("13G", "SC 13G", "Schedule 13G"):
        ds._CACHE.clear()
        out, _ = call("get_sec_filings", sec_router(subs=subs), ticker="AAPL", forms=[spelling])
        assert [f["form"] for f in out["filings"]] == ["SCHEDULE 13G", "SC 13G/A"], spelling
    assert out["filings"][0]["description"] == "Passive 5%+ ownership report"


def test_sec_older_page_failure_keeps_recent_results_with_note():
    out, _ = call("get_sec_filings", sec_router(older=httpx.Response(503)), ticker="AAPL", forms=["10-K"], limit=5)
    assert [f["form"] for f in out["filings"]] == ["10-K", "10-K/A"] and "Older filings couldn't be loaded" in out["note"]
    ds._CACHE.clear()
    router = sec_router()
    call("get_sec_filings", router, ticker="AAPL", limit=3)  # unfiltered: 'recent' is enough, no older page
    assert not router.hits("-submissions-")


def test_sec_filings_long_listing_stays_compact():
    # FWP isn't in FORM_INFO, so its (here very long) primaryDocDescription is used, clipped.
    rows = [(f"0000320193-26-{i:06d}", "2026-10-01", "2026-09-30", "FWP", "", f"{'x' * 300}_{i}.htm", "D" * 300) for i in range(30)]
    out, raw = call("get_sec_filings", sec_router(subs=submissions(rows)), ticker="AAPL", limit=30)
    assert len(raw) < 12_000 and all(len(f["description"]) <= 100 for f in out["filings"])
    assert len(out["filings"]) < 30 and "more filings not shown" in out["trimmed"]


def retail_calendar_facts():
    """A 52-week year of 12/12/12/16-week quarters (Costco-style): 10-Qs carry 12-week and YTD figures, the 10-K only FY."""
    facts, s = [], date(2023, 9, 4)
    for fy, vals in [(2024, [57.8e9, 58.4e9, 58.5e9, 79.7e9]), (2025, [62.2e9, 63.7e9, 63.2e9, 86.2e9])]:
        ends = [s + timedelta(days=83), s + timedelta(days=167), s + timedelta(days=251), s + timedelta(days=363)]
        starts = [s] + [e + timedelta(days=1) for e in ends[:3]]
        for qi in range(3):
            base = {"accn": f"0000909832-{fy}-{qi}", "fy": fy, "fp": f"Q{qi + 1}", "form": "10-Q", "filed": (ends[qi] + timedelta(days=35)).isoformat()}
            facts.append(dict(base, start=starts[qi].isoformat(), end=ends[qi].isoformat(), val=vals[qi]))
            if qi:
                facts.append(dict(base, start=s.isoformat(), end=ends[qi].isoformat(), val=sum(vals[: qi + 1])))
        facts.append({"start": s.isoformat(), "end": ends[3].isoformat(), "val": sum(vals), "accn": f"0000909832-{fy}-K", "fy": fy, "fp": "FY",
                      "form": "10-K", "filed": (ends[3] + timedelta(days=40)).isoformat()})
        s = ends[3] + timedelta(days=1)
    return facts


def test_parse_concept_keeps_16_week_quarters():
    parsed = ds.parse_concept({"tag": "Revenues", "units": {"USD": retail_calendar_facts()}})
    vals = [round(p["val"] / 1e9, 1) for p in parsed["quarters"]]
    assert vals == [57.8, 58.4, 58.5, 79.7, 62.2, 63.7, 63.2, 86.2]  # the 112-day Q4s are kept (derived FY - 36 weeks)
    rows = ds.build_financial_rows({"revenue": parsed}, "quarters", 8)
    assert rows[0]["period"] == "FY2025 Q4" and rows[0]["revenue_yoy_pct"] == round((86.2 - 79.7) / 79.7 * 100, 1)


def test_company_financials_outage_is_not_reported_as_missing():
    concepts = full_concepts()
    concepts["EarningsPerShareDiluted"] = httpx.Response(503)
    concepts["EarningsPerShareBasicAndDiluted"] = httpx.Response(503)
    out, _ = call("get_company_financials", sec_router(concepts=concepts), ticker="AAPL")
    assert "missing" not in out and out["unavailable"][0].startswith("eps_diluted (SEC EDGAR is unavailable right now")
    assert out["quarters"][0]["revenue"] == m(110_700_000_000)
    ds._CACHE.clear()
    down = {c: httpx.Response(503) for c in ("Revenues", "SalesRevenueNet", "ProfitLoss", "EarningsPerShareBasicAndDiluted",
                                             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")}
    out, _ = call("get_company_financials", sec_router(concepts=down), ticker="AAPL")  # primaries 404, fallbacks down
    assert "unavailable right now" in out["error"] and "no US-GAAP" not in out["error"]


def test_earnings_calendar_busy_day_stays_well_under_the_clip():
    rows = [er(f"S{i:03d}", "Arm Holdings plc American Depositary Shares Representing One Ordinary Share", f"${(100 - i) * 10**9:,}",
               "time-after-hours", "($10.87)", "11", "($12.69)") | {"eps": "$1.11", "surprise": "12.34"} for i in range(80)]
    router = Router({"api.nasdaq.com/api/calendar/earnings": json_response(nasdaq_day(rows))})
    out, raw = call("get_earnings_calendar", router, date="2026-11-03", limit=50)
    assert len(raw) < 12_000 and out["count"] == 80
    assert out["status"] == f"80 companies report earnings on Tue Nov 3, 2026; the {len(out['earnings'])} largest shown"
    assert out["earnings"][0]["symbol"] == "S000" and len(out["earnings"][0]["company"]) <= 60


def test_earnings_calendar_this_week_next_week_and_class_shares(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.params["date"])
        rows = [er("BRK/B", "Berkshire Hathaway Inc.", "$1,000,000,000,000", "time-pre-market", "$5.10", "3", "$4.90")] if request.url.params["date"] == "2026-11-06" else []
        return json_response(nasdaq_day(rows))

    monkeypatch.setattr(ds, "_today", lambda: date(2026, 11, 4))  # a Wednesday
    out, _ = call("get_earnings_calendar", Router({"api.nasdaq.com/api/calendar/earnings": handler}), date="this week", symbols=["BRK.B"])
    assert seen == ["2026-11-04", "2026-11-05", "2026-11-06"] and out["status"] == "BRK/B reports 2026-11-06 (before market open)"
    assert "not_found" not in out
    seen.clear()
    monkeypatch.setattr(ds, "_today", lambda: date(2026, 11, 7))  # Saturday: "this week" is the coming one
    call("get_earnings_calendar", Router({"api.nasdaq.com/api/calendar/earnings": handler}), date="this week")
    assert seen == ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]


def test_market_sentiment_errors_carry_no_raw_http_text():
    router = Router({"production.dataviz.cnn.io": httpx.Response(500), "query1.finance.yahoo.com": httpx.Response(500), "query2.finance.yahoo.com": httpx.Response(500)})
    out, raw = call("get_market_sentiment", router)
    assert "Yahoo Finance didn't return the VIX" in out["error"]
    assert "http" not in out["error"].lower() or "HTTP 500" in out["error"]
    assert "finance.yahoo.com" not in raw and "developer.mozilla" not in raw


def test_fred_annual_series_labelled_by_year():
    router = fred_router({"GDPA": "observation_date,GDPA\n2022-01-01,26006.9\n2023-01-01,27720.7\n2024-01-01,29184.9\n"})
    out, _ = call("get_economic_data", router, indicator="GDPA")
    assert out["frequency"] == "annual" and out["period"] == "2024" and [t["period"] for t in out["trend"]] == ["2022", "2023", "2024"]


def test_economic_data_several_indicators_and_lists():
    csvs = {"CPIAUCSL": fred_csv("CPIAUCSL", CPI), "UNRATE": fred_csv("UNRATE", monthly(2026, 1, [4.0, 4.1, 4.2, 4.2, 4.3, 4.3, 4.3, 4.3, 4.4]))}
    router = fred_router(csvs)
    out, _ = call("get_economic_data", router, indicator="inflation rate, unemployment and bogus")
    assert [i["indicator"] for i in out["indicators"]] == ["cpi", "unemployment"] and out["unknown_indicators"] == ["bogus"]
    assert out["status"] == f"US economy: CPI {expected_yoy(CPI)}% YoY (Sep 2026), unemployment 4.4% (Sep 2026)"
    assert len(router.requests) == 2  # just those two, not the 10-indicator dashboard
    out, _ = call("get_economic_data", router, indicator=["cpi", "unemployment"])
    assert [i["indicator"] for i in out["indicators"]] == ["cpi", "unemployment"]
    out, _ = call("get_economic_data", no_network, indicator="foo, bar")
    assert out["error"].startswith("Unknown indicators 'foo', 'bar'")
    assert [ds._indicator_key(k) for k in ("Inflation Rate", "GDP growth rate", "10-year yield", "initial jobless claims", "core inflation rate")] == [
        "cpi", "gdp_growth", "10y_treasury", "jobless_claims", "core_cpi"]


def test_panel_backs_off_while_serving_stale_data():
    good = call_fn(ds.crypto_pulse, cg_router())
    stamp, value = ds._CACHE[("panel_crypto",)]
    ds._CACHE.clear()
    markets._CACHE.clear()
    ds._CACHE[("panel_crypto",)] = (stamp - 600, value)
    router = Router({"api.coingecko.com": httpx.Response(503), "query1.finance.yahoo.com": httpx.Response(503), "query2.finance.yahoo.com": httpx.Response(503)})
    first = call_fn(ds.crypto_pulse, router)
    n = len(router.requests)
    second = call_fn(ds.crypto_pulse, router)
    assert first["stale"] is second["stale"] is True and first["coins"] == good["coins"]
    assert n > 0 and len(router.requests) == n  # the next refresh within 30s doesn't hit CoinGecko/Yahoo again


def test_cache_refresh_is_not_evicted_first(monkeypatch):
    monkeypatch.setattr(ds, "CACHE_MAX", 2)
    ds._cache_put(("a",), 1)
    ds._cache_put(("b",), 2)
    ds._cache_put(("a",), 3)  # refreshed: now the newest entry
    ds._cache_put(("c",), 4)
    assert set(ds._CACHE) == {("a",), ("c",)} and ds._cache_get(("a",), 60) == 3


def test_unexpected_exceptions_become_friendly_errors_without_their_text(monkeypatch):
    async def explode(ctx, day):
        raise RuntimeError("internal detail https://api.stlouisfed.org/x?api_key=SECRETKEY123")

    monkeypatch.setattr(ds, "_nasdaq_day", explode)
    out, raw = call("get_earnings_calendar", no_network, date="2026-11-04")
    assert out["error"] == "Something went wrong reading Nasdaq's earnings calendar (RuntimeError). Try again later."
    assert "SECRETKEY123" not in raw and "internal detail" not in raw
