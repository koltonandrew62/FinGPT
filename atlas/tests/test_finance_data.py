"""Tests for app/tools/finance_data.py. Offline: every reply is a fixture shaped like the real Yahoo/Treasury JSON/XML."""
import asyncio
import json
import math
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.tools import ToolError, call_tool, finance_data as fd
from conftest import Router, json_response, make_ctx, mock_http, no_network, run

NOW = int(time.time())
A3 = (
    "A3=d=AQABBKx3ZWkCEPZtest&S=AQAAAkLz; Expires=Thu, 04 Nov 2027 15:00:00 GMT; Max-Age=31557600; "
    "Domain=.yahoo.com; Path=/; Secure; HttpOnly; SameSite=None"
)
Q1 = "query1.finance.yahoo.com"
Q2 = "query2.finance.yahoo.com"
CRUMB = f"{Q1}/v1/test/getcrumb"
V7 = f"{Q1}/v7/finance/quote"
V10 = f"{Q2}/v10/finance/quoteSummary/"
SCREENER = f"{Q1}/v1/finance/screener/predefined/saved"
SEARCH = f"{Q2}/v1/finance/search"
TREASURY = "home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    fd._CACHE.clear()
    fd._LIMITER.clear()
    monkeypatch.setattr(fd, "HOST_INTERVAL", 0.0)
    monkeypatch.setattr(fd, "_SESSION", fd.YahooSession(transport=httpx.MockTransport(no_network)))
    for key in ("FINANCE_REGION", "FINANCE_USER_AGENT"):
        monkeypatch.delenv(key, raising=False)
    yield
    asyncio.run(fd._SESSION.close())
    fd._CACHE.clear()
    fd._LIMITER.clear()


def setup(router, monkeypatch):
    """Point the Yahoo session and the shared client at the same mock router; return a ToolContext."""
    monkeypatch.setattr(fd, "_SESSION", fd.YahooSession(transport=httpx.MockTransport(router)))
    return make_ctx(http=mock_http(router))


def fc_cookie(request):
    return httpx.Response(404, headers=[("set-cookie", A3), ("content-type", "text/html")], text="<html>Not found</html>")


def crumb_handler(*values):
    seq = list(values)

    def h(request):
        assert "A3=d=AQABBKx3ZWkCEPZtest" in request.headers.get("cookie", ""), "crumb request must carry the A3 cookie"
        value = seq.pop(0) if len(seq) > 1 else seq[0]
        return httpx.Response(200, text=value, headers={"content-type": "text/plain;charset=utf-8"})

    return h


def with_crumb(router, *crumbs):
    router.add("fc.yahoo.com", fc_cookie)
    router.add(CRUMB, crumb_handler(*(crumbs or ("Xk9/abC.d3F",))))
    return router


def v7_handler(rows: dict, seen: list | None = None):
    def h(request):
        if seen is not None:
            seen.append(request.url.params.get("crumb"))
        syms = request.url.params["symbols"].split(",")
        return json_response({"quoteResponse": {"result": [rows[s] for s in syms if s in rows], "error": None}})

    return h


def invalid_crumb():
    return json_response({"finance": {"result": None, "error": {"code": "Unauthorized", "description": "Invalid Crumb"}}}, 401)


def summary_404(sym):
    return json_response(
        {"quoteSummary": {"result": None, "error": {"code": "Not Found", "description": f"Quote not found for symbol: {sym}"}}}, 404
    )


def raw(v, fmt=None):
    return {"raw": v, "fmt": fmt if fmt is not None else f"{v:.2f}"}


# ----------------------------------------------------------------------------- fixtures shaped like real replies
T_CLOSE = NOW - 3 * 3600
AAPL_ROW = {
    "language": "en-US", "region": "US", "quoteType": "EQUITY", "typeDisp": "Equity",
    "quoteSourceName": "Nasdaq Real Time Price", "triggerable": True, "customPriceAlertConfidence": "HIGH",
    "currency": "USD", "exchange": "NMS", "shortName": "Apple Inc.", "longName": "Apple Inc.",
    "messageBoardId": "finmb_24937", "exchangeTimezoneName": "America/New_York", "exchangeTimezoneShortName": "EST",
    "gmtOffSetMilliseconds": -18000000, "market": "us_market", "esgPopulated": False, "marketState": "POST",
    "regularMarketChangePercent": 1.2287, "regularMarketPrice": 229.87, "regularMarketChange": 2.79,
    "regularMarketTime": T_CLOSE, "regularMarketDayHigh": 230.5, "regularMarketDayRange": "226.1 - 230.5",
    "regularMarketDayLow": 226.1, "regularMarketVolume": 61234567, "regularMarketPreviousClose": 227.08,
    "regularMarketOpen": 227.5,
    "postMarketChangePercent": 0.2741, "postMarketPrice": 230.5, "postMarketChange": 0.63, "postMarketTime": T_CLOSE + 3600,
    # left over from this morning, older than the regular close: must not be shown
    "preMarketPrice": 226.9, "preMarketChange": -0.18, "preMarketChangePercent": -0.0793, "preMarketTime": T_CLOSE - 8 * 3600,
    "averageDailyVolume3Month": 50000000, "averageDailyVolume10Day": 48000000,
    "fiftyTwoWeekLow": 164.08, "fiftyTwoWeekHigh": 260.1, "fiftyTwoWeekRange": "164.08 - 260.1",
    "fiftyDayAverage": 225.4, "twoHundredDayAverage": 215.3,
    "marketCap": 3456789012345, "trailingPE": 35.21, "forwardPE": 28.4, "epsTrailingTwelveMonths": 6.53,
    "epsForward": 8.1, "epsCurrentYear": 7.39, "priceToBook": 52.1, "bookValue": 4.41,
    "dividendRate": 1.04, "dividendYield": 0.45, "trailingAnnualDividendRate": 1.0, "trailingAnnualDividendYield": 0.0044,
    "earningsTimestamp": NOW - 6 * 86400, "earningsTimestampStart": NOW + 85 * 86400,
    "earningsTimestampEnd": NOW + 85 * 86400, "isEarningsDateEstimate": True,
    "averageAnalystRating": "2.0 - Buy", "sharesOutstanding": 15037900000, "fullExchangeName": "NasdaqGS",
    "symbol": "AAPL",
}
LONG_SUMMARY = (
    "Apple Inc. designs, manufactures, and markets smartphones, personal computers, tablets, wearables, and "
    "accessories worldwide. The company offers iPhone, a line of smartphones; Mac, a line of personal computers; "
    "iPad, a line of multi-purpose tablets; and wearables, home, and accessories comprising AirPods, Apple TV, "
    "Apple Watch, Beats products, and HomePod. It also provides AppleCare support and cloud services; and operates "
    "various platforms, including the App Store that allow customers to discover and download applications and "
    "digital content, such as books, music, video, games, and podcasts. The company was founded in 1976 and is "
    "headquartered in Cupertino, California."
)
AAPL_SUMMARY = {
    "quoteSummary": {
        "result": [
            {
                "assetProfile": {
                    "address1": "One Apple Park Way", "city": "Cupertino", "state": "CA", "zip": "95014",
                    "country": "United States", "phone": "(408) 996-1010", "website": "https://www.apple.com",
                    "industry": "Consumer Electronics", "industryKey": "consumer-electronics",
                    "industryDisp": "Consumer Electronics", "sector": "Technology", "sectorKey": "technology",
                    "sectorDisp": "Technology", "longBusinessSummary": LONG_SUMMARY, "fullTimeEmployees": 164000,
                    "companyOfficers": [{"maxAge": 1, "name": "Mr. Timothy D. Cook", "age": 63, "title": "CEO & Director"}],
                    "auditRisk": 7, "boardRisk": 1, "maxAge": 86400,
                },
                "financialData": {
                    "maxAge": 86400, "currentPrice": raw(229.87), "targetHighPrice": raw(300.0),
                    "targetLowPrice": raw(184.0), "targetMeanPrice": raw(252.86), "targetMedianPrice": raw(255.0),
                    "recommendationMean": raw(1.95), "recommendationKey": "buy",
                    "numberOfAnalystOpinions": {"raw": 41, "fmt": "41", "longFmt": "41"},
                    "totalCash": {"raw": 65171001344, "fmt": "65.17B", "longFmt": "65,171,001,344"},
                    "totalCashPerShare": raw(4.31), "ebitda": {"raw": 134660997120, "fmt": "134.66B"},
                    "totalDebt": {"raw": 119058997248, "fmt": "119.06B"}, "quickRatio": raw(0.745),
                    "currentRatio": raw(0.867, "0.87"), "totalRevenue": {"raw": 391034994688, "fmt": "391.03B"},
                    "debtToEquity": raw(209.059, "209.06%"), "revenuePerShare": raw(25.485),
                    "returnOnAssets": raw(0.21464, "21.46%"), "returnOnEquity": raw(1.5741, "157.41%"),
                    "freeCashflow": {"raw": 110846001152, "fmt": "110.85B"},
                    "operatingCashflow": {"raw": 118254002176, "fmt": "118.25B"},
                    "earningsGrowth": raw(-0.341, "-34.10%"), "revenueGrowth": raw(0.061, "6.10%"),
                    "grossMargins": raw(0.46206, "46.21%"), "ebitdaMargins": raw(0.34437, "34.44%"),
                    "operatingMargins": raw(0.31171, "31.17%"), "profitMargins": raw(0.23971, "23.97%"),
                    "financialCurrency": "USD",
                },
                "defaultKeyStatistics": {
                    "maxAge": 1, "priceHint": {"raw": 2, "fmt": "2"},
                    "enterpriseValue": {"raw": 3510000000000, "fmt": "3.51T"}, "forwardPE": raw(28.4),
                    "profitMargins": raw(0.23971, "23.97%"), "beta": raw(1.24), "pegRatio": {},
                    "shortPercentOfFloat": raw(0.0071, "0.71%"), "enterpriseToEbitda": raw(26.9),
                    "trailingEps": raw(6.53), "forwardEps": raw(8.1), "52WeekChange": raw(0.21, "21.00%"),
                },
                "summaryDetail": {
                    "maxAge": 1, "priceHint": {"raw": 2, "fmt": "2"}, "previousClose": raw(227.08),
                    "dividendRate": raw(1.0), "dividendYield": raw(0.0044, "0.44%"),
                    "exDividendDate": {"raw": 1794787200, "fmt": "2026-11-10"}, "payoutRatio": raw(0.1612, "16.12%"),
                    "beta": raw(1.24), "trailingPE": raw(35.21),
                    "marketCap": {"raw": 3456789012345, "fmt": "3.46T", "longFmt": "3,456,789,012,345"},
                    "priceToSalesTrailing12Months": raw(8.84), "currency": "USD", "fromCurrency": None,
                    "toCurrency": None, "lastMarket": None, "coinMarketCapLink": None, "algorithm": None, "tradeable": False,
                },
                "calendarEvents": {
                    "maxAge": 1,
                    "earnings": {
                        "earningsDate": [{"raw": 1801512000, "fmt": "2027-01-28"}],
                        "earningsCallDate": [{"raw": 1801515600, "fmt": "2027-01-28"}],
                        "isEarningsDateEstimate": True, "earningsAverage": raw(2.67), "earningsLow": raw(2.5),
                        "earningsHigh": raw(2.81), "revenueAverage": {"raw": 138470000000, "fmt": "138.47B"},
                        "revenueLow": {"raw": 136000000000, "fmt": "136B"}, "revenueHigh": {"raw": 140200000000, "fmt": "140.2B"},
                    },
                    "exDividendDate": {"raw": 1794787200, "fmt": "2026-11-10"},
                    "dividendDate": {"raw": 1795046400, "fmt": "2026-11-13"},
                },
                "recommendationTrend": {
                    "trend": [
                        {"period": "0m", "strongBuy": 7, "buy": 24, "hold": 15, "sell": 1, "strongSell": 2},
                        {"period": "-1m", "strongBuy": 7, "buy": 23, "hold": 16, "sell": 1, "strongSell": 2},
                        {"period": "-2m", "strongBuy": 8, "buy": 23, "hold": 15, "sell": 1, "strongSell": 2},
                        {"period": "-3m", "strongBuy": 8, "buy": 22, "hold": 14, "sell": 2, "strongSell": 2},
                    ],
                    "maxAge": 86400,
                },
                "earningsTrend": {
                    "trend": [
                        {
                            "maxAge": 1, "period": "0q", "endDate": "2026-12-31", "growth": raw(0.1125, "11.25%"),
                            "earningsEstimate": {
                                "avg": raw(2.67), "low": raw(2.5), "high": raw(2.81), "yearAgoEps": raw(2.4),
                                "numberOfAnalysts": {"raw": 27, "fmt": "27", "longFmt": "27"}, "growth": raw(0.1125, "11.25%"),
                            },
                            "revenueEstimate": {"avg": {"raw": 138470000000, "fmt": "138.47B"}},
                            "epsTrend": {"current": raw(2.67)}, "epsRevisions": {"upLast7days": {"raw": 1, "fmt": "1"}},
                        },
                        {"maxAge": 1, "period": "+1q", "endDate": "2027-03-31", "earningsEstimate": {"avg": raw(1.75)}},
                        {"maxAge": 1, "period": "0y", "endDate": "2027-09-30", "earningsEstimate": {"avg": raw(8.21)}},
                        {"maxAge": 1, "period": "+1y", "endDate": "2028-09-30", "earningsEstimate": {"avg": raw(8.95)}},
                    ],
                    "maxAge": 1,
                },
            }
        ],
        "error": None,
    }
}
SPY_ROW = {
    "language": "en-US", "region": "US", "quoteType": "ETF", "typeDisp": "ETF", "currency": "USD",
    "exchange": "PCX", "shortName": "SPDR S&P 500", "longName": "SPDR S&P 500 ETF Trust", "marketState": "REGULAR",
    "regularMarketPrice": 601.25, "regularMarketChange": -3.1, "regularMarketChangePercent": -0.5129,
    "regularMarketTime": NOW - 600, "regularMarketPreviousClose": 604.35, "regularMarketVolume": 40500000,
    "averageDailyVolume3Month": 45000000, "fiftyTwoWeekLow": 480.0, "fiftyTwoWeekHigh": 612.0,
    "trailingAnnualDividendRate": 7.1, "trailingAnnualDividendYield": 0.01175, "trailingPE": 27.1,
    "netAssets": 571450000000, "netExpenseRatio": 0.0945, "fullExchangeName": "NYSEArca", "symbol": "SPY",
    "gmtOffSetMilliseconds": -18000000,
}
SPY_SUMMARY = {
    "quoteSummary": {
        "result": [
            {
                "assetProfile": {"longBusinessSummary": "The trust seeks to achieve its investment objective by holding a portfolio of the common stocks that are included in the index.", "maxAge": 86400},
                "defaultKeyStatistics": {
                    "maxAge": 1, "category": "Large Blend", "fundFamily": "SPDR State Street Global Advisors",
                    "ytdReturn": raw(0.1664, "16.64%"), "threeYearAverageReturn": raw(0.1101, "11.01%"),
                    "fiveYearAverageReturn": raw(0.1452, "14.52%"), "beta3Year": raw(1.0), "legalType": "Exchange Traded Fund",
                },
                "summaryDetail": {
                    "maxAge": 1, "previousClose": raw(604.35), "yield": raw(0.0121, "1.21%"),
                    "totalAssets": {"raw": 571450000000, "fmt": "571.45B"}, "navPrice": raw(601.1),
                    "currency": "USD", "trailingPE": raw(27.1),
                },
                "calendarEvents": {"maxAge": 1, "earnings": {"earningsDate": [], "earningsCallDate": [], "isEarningsDateEstimate": False}},
                "recommendationTrend": {"trend": [], "maxAge": 86400},
                "earningsTrend": {"trend": [], "maxAge": 1},
            }
        ],
        "error": None,
    }
}
BTC_ROW = {
    "language": "en-US", "region": "US", "quoteType": "CRYPTOCURRENCY", "typeDisp": "Cryptocurrency",
    "currency": "USD", "exchange": "CCC", "shortName": "Bitcoin USD", "marketState": "REGULAR",
    "regularMarketPrice": 98123.45, "regularMarketChange": 1523.2, "regularMarketChangePercent": 1.5768,
    "regularMarketTime": NOW - 60, "regularMarketPreviousClose": 96600.25, "regularMarketVolume": 45678901234,
    "marketCap": 1943210000000, "circulatingSupply": 19800000, "volume24Hr": 45678901234, "fromCurrency": "BTC",
    "toCurrency": "USD=X", "lastMarket": "CoinMarketCap", "fiftyTwoWeekLow": 52000.0, "fiftyTwoWeekHigh": 112000.0,
    "fullExchangeName": "CCC", "symbol": "BTC-USD",
}


def mover_row(sym, name, price, pct, vol, state="REGULAR"):
    return {
        "language": "en-US", "region": "US", "quoteType": "EQUITY", "typeDisp": "Equity", "quoteSourceName": "Delayed Quote",
        "triggerable": True, "currency": "USD", "marketState": state, "exchange": "NMS", "shortName": name,
        "longName": name, "regularMarketChangePercent": pct, "regularMarketPrice": price,
        "regularMarketChange": round(price - price / (1 + pct / 100), 4), "regularMarketVolume": vol,
        "averageDailyVolume3Month": vol // 2, "marketCap": 24500000000, "fullExchangeName": "NasdaqGS", "symbol": sym,
    }


def screener_payload(scr, rows):
    return {
        "finance": {
            "result": [
                {
                    "id": "8d6f8d46-c4d1-4f5f-9b7e-1c6b9f1a2b3c", "title": scr.replace("_", " ").title(),
                    "description": "Stocks ordered in descending order by price percent change", "canonicalName": scr.upper(),
                    "criteriaMeta": {"size": 25, "offset": 0, "sortField": "percentchange", "sortType": "DESC", "quoteType": "EQUITY"},
                    "rawCriteria": "{}", "start": 0, "count": len(rows), "total": 120, "quotes": rows,
                    "useRecords": False, "predefinedScr": True, "versionId": 7, "creationDate": 1588878000000,
                    "lastUpdated": 1730736000000, "isPremium": False, "iconUrl": "",
                }
            ],
            "error": None,
        }
    }


GAINERS = [
    mover_row("SMCI", "Super Micro Computer, Inc.", 41.3, 18.52, 98765432),
    mover_row("RIVN", "Rivian Automotive, Inc.", 12.05, 11.2, 54321000),
    mover_row("PLUG", "Plug Power Inc.", 2.345, 9.87, 77000000),
    mover_row("SOFI", "SoFi Technologies, Inc.", 15.6, 7.4, 66000000),
    mover_row("U", "Unity Software Inc.", 22.1, 6.9, 12000000),
    mover_row("AFRM", "Affirm Holdings, Inc.", 48.2, 6.1, 9000000),
]
LOSERS = [mover_row(s, f"{s} Corp", 10.0 + i, -9.0 + i, 1000000 * (i + 1)) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])]
ACTIVES = [mover_row(s, f"{s} Inc.", 100.0 + i, 0.5 * i, 90000000 - i) for i, s in enumerate(["NVDA", "TSLA", "F", "AAPL", "INTC", "AMD"])]
TRENDING_SYMS = ["NVDA", "TSLA", "BTC-USD", "PLTR", "^GSPC", "MSTR", "COIN", "AMD", "SMCI", "HOOD"]


def trending_payload(symbols):
    return {
        "finance": {
            "result": [{"count": len(symbols), "quotes": [{"symbol": s} for s in symbols], "jobTimestamp": 1730736000000, "startInterval": 202611041500}],
            "error": None,
        }
    }


def chart_1d(symbol, price, prev, **meta):
    """A v8 chart reply as markets.fetch_quote consumes it."""
    m = {
        "currency": "USD", "symbol": symbol, "exchangeName": "NIM", "fullExchangeName": "Cboe Indices",
        "instrumentType": "INDEX", "regularMarketPrice": price, "previousClose": prev, "chartPreviousClose": prev,
        "regularMarketTime": NOW - 900, "shortName": symbol, "fiftyTwoWeekHigh": price * 1.1, "fiftyTwoWeekLow": price * 0.9,
    }
    m.update(meta)
    return json_response({"chart": {"result": [{"meta": m, "indicators": {"quote": [{"close": [prev, price]}]}}], "error": None}})


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def daily_chart(closes, symbol="MSFT", start=date(2025, 11, 3), instrument="EQUITY", highs=None, lows=None, gap_at=None):
    days = weekdays(start, len(closes))
    ts = [int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp()) for d in days]
    closes = list(closes)
    highs = list(highs) if highs is not None else [round(c * 1.01, 4) for c in closes]
    lows = list(lows) if lows is not None else [round(c * 0.99, 4) for c in closes]
    if gap_at is not None:  # Yahoo leaves null rows for halted days; they must be skipped
        ts.insert(gap_at, ts[gap_at - 1] + 3600)
        for arr in (closes, highs, lows):
            arr.insert(gap_at, None)
    meta = {
        "currency": "USD", "symbol": symbol, "exchangeName": "NMS", "fullExchangeName": "NasdaqGS",
        "instrumentType": instrument, "firstTradeDate": 511108200, "regularMarketTime": ts[-1] + 23400,
        "hasPrePostMarketData": True, "gmtoffset": -18000, "timezone": "EST", "exchangeTimezoneName": "America/New_York",
        "regularMarketPrice": [c for c in closes if c is not None][-1], "longName": "Microsoft Corporation",
        "shortName": "Microsoft Corporation", "chartPreviousClose": closes[0], "priceHint": 2,
        "dataGranularity": "1d", "range": "1y", "validRanges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"],
    }
    return {
        "chart": {
            "result": [
                {
                    "meta": meta,
                    "timestamp": ts,
                    "events": {"dividends": {str(ts[10]): {"amount": 0.83, "date": ts[10]}}},
                    "indicators": {
                        "quote": [{"open": closes, "high": highs, "low": lows, "close": closes, "volume": [20000000] * len(closes)}],
                        "adjclose": [{"adjclose": closes}],
                    },
                }
            ],
            "error": None,
        }
    }


def walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    yield obj


def details_router():
    router = with_crumb(Router())
    seen: list = []
    router.add(V7, v7_handler({"AAPL": AAPL_ROW, "SPY": SPY_ROW, "BTC-USD": BTC_ROW}, seen))
    router.add(V10 + "AAPL", json_response(AAPL_SUMMARY))
    router.add(V10 + "SPY", json_response(SPY_SUMMARY))
    router.add(V10 + "BTC-USD", summary_404("BTC-USD"))
    return router, seen


# ----------------------------------------------------------------------------- crumb session
def test_crumb_flow_sets_cookie_and_sends_crumb(monkeypatch):
    router, seen = details_router()
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_details(ctx, "aapl"))
    assert out["symbol"] == "AAPL" and out["quote"]["price"] == 229.87
    assert seen == ["Xk9/abC.d3F"]
    # v7 and v10 ran concurrently but shared one crumb fetch
    assert len(router.hits("fc.yahoo.com")) == 1 and len(router.hits("/v1/test/getcrumb")) == 1
    v10 = router.hits("quoteSummary")[0]
    assert v10.url.params["crumb"] == "Xk9/abC.d3F"
    assert v10.url.params["modules"].split(",") == [
        "assetProfile", "financialData", "defaultKeyStatistics", "summaryDetail", "calendarEvents",
        "recommendationTrend", "earningsTrend",
    ]
    assert "A3=" in v10.headers["cookie"]
    assert "Mozilla/5.0" in v10.headers["user-agent"]
    # a second lookup reuses the crumb
    run(fd.get_stock_details(ctx, "SPY"))
    assert len(router.hits("/v1/test/getcrumb")) == 1


def test_crumb_refreshes_once_on_invalid_crumb(monkeypatch):
    router = with_crumb(Router(), "oldCrumb123", "newCrumb456")
    seen: list = []
    good = v7_handler({"AAPL": AAPL_ROW}, seen)
    calls = {"n": 0}

    def v7(request):
        calls["n"] += 1
        if request.url.params["crumb"] == "oldCrumb123":
            seen.append("old")
            return invalid_crumb()
        return good(request)

    router.add(V7, v7)
    router.add(V10 + "AAPL", json_response(AAPL_SUMMARY))
    setup(router, monkeypatch)
    rows = run(fd.v7_quotes(["AAPL"]))
    assert rows["AAPL"]["regularMarketPrice"] == 229.87
    assert seen == ["old", "newCrumb456"] and calls["n"] == 2
    assert len(router.hits("/v1/test/getcrumb")) == 2
    assert fd._SESSION.crumb_value == "newCrumb456"


def test_crumb_refresh_on_403_then_gives_up_without_leaking(monkeypatch):
    router = with_crumb(Router(), "crumbOne111", "crumbTwo222")
    router.add(V7, invalid_crumb())
    router.add(V10 + "AAPL", httpx.Response(403, text="<html><body>Forbidden</body></html>"))
    router.add(f"{Q1}/v8/finance/chart/AAPL", httpx.Response(500, text="<html>oops</html>"))
    router.add(f"{Q2}/v8/finance/chart/AAPL", httpx.Response(500, text="<html>oops</html>"))
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_stock_details", {"symbol": "AAPL"}, ctx)))
    err = out["error"]
    assert "Couldn't get details for AAPL" in err and "Yahoo Finance" in err
    for leak in ("crumbOne111", "crumbTwo222", "<html", "Traceback"):
        assert leak not in err
    # first crumb + exactly one refresh; then the session backs off instead of redoing the flow for v10
    assert len(router.hits("/v1/test/getcrumb")) == 2
    assert fd._SESSION.crumb_value is None  # cookies and crumb dropped after a refreshed crumb was refused too
    with pytest.raises(fd.YahooAuthError, match="retries in about a minute"):
        run(fd._SESSION.crumb())
    assert len(router.hits("/v1/test/getcrumb")) == 2


def test_consent_redirect_fails_over_to_chart_and_backs_off(monkeypatch):
    router = Router()
    router.add("fc.yahoo.com", httpx.Response(302, headers={"location": "https://guce.yahoo.com/consent?brandType=nonEu&gcrumb=abc"}))
    router.add("guce.yahoo.com", httpx.Response(302, headers={"location": "https://consent.yahoo.com/v2/collectConsent?sessionId=3_cc-session_xyz"}))
    router.add("consent.yahoo.com", httpx.Response(200, text="<html><form>Accept all</form></html>", headers={"content-type": "text/html"}))
    router.add(f"{Q1}/v8/finance/chart/AAPL", chart_1d("AAPL", 229.87, 227.08, shortName="Apple Inc.", instrumentType="EQUITY", fullExchangeName="NasdaqGS"))
    router.add(SCREENER, json_response({"finance": {"result": None, "error": {"code": "Unauthorized", "description": "Invalid Crumb"}}}, 401))
    ctx = setup(router, monkeypatch)

    out = run(fd.get_stock_details(ctx, "AAPL"))
    assert out["quote"]["price"] == 229.87 and out["quote"]["change"] == 2.79
    assert "chart" in out["source"]
    assert len(out["errors"]) == 1 and "cookie consent" in out["errors"][0]
    assert out["name"] == "Apple Inc."

    err = json.loads(run(call_tool("get_market_movers", {"kind": "gainers"}, ctx)))["error"]
    assert "cookie consent" in err
    assert len(router.hits("fc.yahoo.com")) == 1  # backed off: no new consent round-trips
    assert router.hits("v1/finance/screener")[0].url.params.get("crumb") is None  # tried the crumb-less fallback


def test_getcrumb_garbage_is_an_auth_error(monkeypatch):
    router = Router()
    router.add("fc.yahoo.com", fc_cookie)
    router.add(CRUMB, httpx.Response(200, text="<!doctype html><html>Too many</html>"))
    router.add(f"{Q2}/v1/test/getcrumb", httpx.Response(200, text=""))
    setup(router, monkeypatch)
    with pytest.raises(fd.YahooAuthError, match="Couldn't start a Yahoo Finance session"):
        run(fd._SESSION.crumb())
    # backs off: the next call fails fast without hitting Yahoo
    with pytest.raises(fd.YahooAuthError):
        run(fd._SESSION.crumb())
    assert len(router.hits("fc.yahoo.com")) == 1


def test_fc_connection_error_tolerated(monkeypatch):
    router = Router()
    router.add("fc.yahoo.com", httpx.ConnectError("reset"))
    router.add(CRUMB, httpx.Response(200, text="abcDEF12345"))
    setup(router, monkeypatch)
    assert run(fd._SESSION.crumb()) == "abcDEF12345"


def test_user_agent_override(monkeypatch):
    monkeypatch.setenv("FINANCE_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) Custom/1.0")
    router, _ = details_router()
    ctx = setup(router, monkeypatch)
    run(fd.get_stock_details(ctx, "AAPL"))
    assert router.hits("getcrumb")[0].headers["user-agent"].endswith("Custom/1.0")


def test_shutdown_closes_session_client(monkeypatch):
    router = with_crumb(Router())
    setup(router, monkeypatch)

    async def go():
        await fd._SESSION.crumb()
        client = fd._SESSION._client
        await fd.shutdown()
        return client

    client = run(go())
    assert client.is_closed and fd._SESSION._client is None
    run(fd.shutdown())  # idempotent


# ----------------------------------------------------------------------------- stock details parsing
def test_stock_details_full_equity(monkeypatch):
    router, _ = details_router()
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_details(ctx, "AAPL"))
    assert out["status"] == "AAPL 229.87 USD, +1.23% (after-hours); after-hours 230.5 (+0.27%)"
    assert out["name"] == "Apple Inc." and out["type"] == "EQUITY" and out["exchange"] == "NasdaqGS"
    q = out["quote"]
    assert q["market_state"] == "after-hours" and q["change"] == 2.79 and q["change_pct"] == 1.23
    assert q["post_market"] == {"price": 230.5, "change": 0.63, "change_pct": 0.27, "as_of": q["post_market"]["as_of"]}
    assert "pre_market" not in q  # older than the regular close
    assert q["volume"] == 61234567 and q["avg_volume_3m"] == 50000000 and q["volume_to_avg_ratio"] == 1.22
    assert q["52w_high"] == 260.1 and q["pct_from_52w_high"] == pytest.approx(-11.62, abs=0.01)
    assert q["pct_above_52w_low"] == pytest.approx(40.1, abs=0.01)
    v = out["valuation"]
    assert v["market_cap"] == "3.46T" and v["pe_trailing"] == 35.21 and v["pe_forward"] == 28.4
    assert v["eps_ttm"] == 6.53 and v["beta"] == 1.24 and v["short_pct_of_float"] == 0.71
    assert "peg" not in v  # empty {} in the reply
    assert out["dividend"] == {"annual_rate": 1.0, "yield_pct": 0.44, "ex_date": "2026-11-10", "pay_date": "2026-11-13", "payout_ratio_pct": 16.12}
    p = out["profile"]
    assert p["sector"] == "Technology" and p["industry"] == "Consumer Electronics" and p["employees"] == 164000
    assert len(p["summary"]) <= 420 and p["summary"].endswith("…") and p["website"] == "https://www.apple.com"
    f = out["financials"]
    assert f["revenue_growth_yoy_pct"] == 6.1 and f["gross_margin_pct"] == 46.21 and f["profit_margin_pct"] == 23.97
    assert f["debt_to_equity"] == 2.09 and f["free_cash_flow"] == "110.85B" and f["revenue_ttm"] == "391.03B"
    assert f["earnings_growth_yoy_pct"] == -34.1
    a = out["analysts"]
    assert a["consensus"] == "buy" and a["analysts"] == 41 and a["mean_score"] == 1.95
    assert (a["target_mean"], a["target_high"], a["target_low"]) == (252.86, 300.0, 184.0)
    assert a["implied_upside_pct"] == pytest.approx((252.86 / 229.87 - 1) * 100, abs=0.01)
    assert a["ratings_now"] == {"strong_buy": 7, "buy": 24, "hold": 15, "sell": 1, "strong_sell": 2}
    assert a["ratings_3m_ago"]["buy"] == 22
    e = out["earnings"]
    assert e["next_date"] == "2027-01-28" and e["date_is_estimate"] is True
    assert e["eps_estimate"] == 2.67 and e["eps_low"] == 2.5 and e["eps_year_ago"] == 2.4 and e["eps_analysts"] == 27
    assert e["revenue_estimate"] == "138.47B" and e["eps_growth_pct"] == 11.25 and e["fiscal_year_eps_estimate"] == 8.21
    assert "fund" not in out and "crypto" not in out and "errors" not in out
    assert "15 minutes" in out["note"]
    assert None not in list(walk(out))
    assert len(json.dumps(out)) < 4000


def test_stock_details_etf_omits_company_fields(monkeypatch):
    router, _ = details_router()
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_details(ctx, "spy"))
    assert out["type"] == "ETF" and out["quote"]["market_state"] == "open"
    for missing in ("analysts", "financials", "earnings", "crypto"):
        assert missing not in out, missing
    fund = out["fund"]
    assert fund["category"] == "Large Blend" and fund["family"].startswith("SPDR")
    assert fund["net_assets"] == "571.45B" and fund["expense_ratio_pct"] == 0.0945 and fund["yield"] == "1.21%"
    assert fund["ytd_return"] == "16.64%" and fund["nav"] == 601.1
    assert out["dividend"]["yield_pct"] == 1.18  # trailingAnnualDividendYield fraction -> percent
    assert out["profile"] == {"summary": SPY_SUMMARY["quoteSummary"]["result"][0]["assetProfile"]["longBusinessSummary"]}
    assert None not in list(walk(out)) and {} not in list(walk(out))


def test_stock_details_crypto_without_fundamentals(monkeypatch):
    router, _ = details_router()
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_details(ctx, "BTC-USD"))
    assert out["type"] == "CRYPTOCURRENCY" and out["quote"]["price"] == 98123.45
    assert out["crypto"] == {"circulating_supply": "19.80M", "volume_24h": "45.68B"}
    assert "errors" not in out  # no quoteSummary is normal for crypto
    assert out["source"] == "Yahoo Finance quote"
    assert out["valuation"] == {"market_cap": "1.94T"}


def test_build_details_earnings_from_v7_timestamps():
    now = 1_800_000_000
    row = dict(AAPL_ROW, earningsTimestampStart=now + 10 * 86400, earningsTimestamp=now - 5 * 86400, earningsTimestampEnd=None)
    d = fd.build_details("AAPL", row, None, None, now=now)
    expected = datetime.fromtimestamp(now + 10 * 86400 - 18000, tz=timezone.utc).date().isoformat()
    assert d["earnings"] == {"next_date": expected, "date_is_estimate": True}
    past = dict(AAPL_ROW, earningsTimestampStart=now - 9 * 86400, earningsTimestamp=now - 5 * 86400, earningsTimestampEnd=None)
    d = fd.build_details("AAPL", past, None, None, now=now)
    assert list(d["earnings"]) == ["last_date"]


def test_build_details_handles_junk_values():
    row = {"symbol": "XYZ", "regularMarketPrice": "n/a", "marketCap": float("nan"), "trailingPE": True, "quoteType": "EQUITY"}
    summary = {"financialData": {"targetMeanPrice": {}, "recommendationKey": "none", "debtToEquity": {"raw": None}}, "assetProfile": None}
    d = fd.build_details("XYZ", row, summary, None)
    assert d == {"symbol": "XYZ", "type": "EQUITY"}


def test_unknown_symbol_suggests_matches(monkeypatch):
    router = with_crumb(Router())
    router.add(V7, json_response({"quoteResponse": {"result": [], "error": None}}))
    router.add(V10 + "APPLE", summary_404("APPLE"))
    router.add(f"{Q1}/v8/finance/chart/APPLE", json_response({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}, 404))
    router.add(SEARCH, json_response({"quotes": [{"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY"}, {"symbol": "APLE", "shortname": "Apple Hospitality REIT, Inc."}], "news": []}))
    ctx = setup(router, monkeypatch)
    with pytest.raises(ToolError) as exc:
        run(fd.get_stock_details(ctx, "apple"))
    msg = str(exc.value)
    assert "Unknown ticker 'APPLE'" in msg and "AAPL (Apple Inc.)" in msg and "lookup_ticker" in msg


@pytest.mark.parametrize(
    "given, expected",
    [("aapl", "AAPL"), ("$tsla", "TSLA"), ("brk.b", "BRK-B"), (" ^gspc ", "^GSPC"), ("rci.b.to", "RCI.B.TO"), ("eurusd=x", "EURUSD=X"), ("M&M.NS", "M&M.NS")],
)
def test_symbol_normalisation(given, expected):
    assert fd._norm_symbol(given) == expected


def test_bad_symbols_rejected_without_network():
    ctx = make_ctx(http=mock_http(no_network))
    for bad in ("drop table;", "", "<script>", "A" * 30, None):
        out = json.loads(run(call_tool("get_stock_details", {"symbol": bad}, ctx)))
        assert "doesn't look like a ticker" in out["error"]


# ----------------------------------------------------------------------------- movers
def screener_router(state="REGULAR"):
    router = with_crumb(Router())
    payloads = {
        "day_gainers": [dict(r, marketState=state) for r in GAINERS],
        "day_losers": LOSERS,
        "most_actives": ACTIVES,
    }

    def scr(request):
        assert request.url.params["crumb"]
        assert request.url.params["formatted"] == "false"
        sid = request.url.params["scrIds"]
        return json_response(screener_payload(sid, payloads[sid]))

    router.add(SCREENER, scr)
    return router


def test_market_movers_gainers(monkeypatch):
    router = screener_router(state="POST")
    ctx = setup(router, monkeypatch)
    out = run(fd.get_market_movers(ctx, kind="top gainers", count=3))
    assert out["status"] == "Top 3 US day gainers (market after-hours)"
    assert [m["symbol"] for m in out["movers"]] == ["SMCI", "RIVN", "PLUG"]
    smci = out["movers"][0]
    assert smci == {
        "symbol": "SMCI", "name": "Super Micro Computer, Inc.", "price": 41.3, "change_pct": 18.52, "change": 6.45,
        "volume": "98.77M", "avg_volume_3m": "49.38M", "market_cap": "24.50B", "exchange": "NasdaqGS",
    }
    assert out["movers"][2]["price"] == 2.35
    assert router.hits("screener")[0].url.params["scrIds"] == "day_gainers"
    # cached: a second call (other count) doesn't hit Yahoo again
    run(fd.get_market_movers(ctx, kind="gainers", count=6))
    assert len(router.hits("screener")) == 1


def test_market_movers_losers_and_active(monkeypatch):
    router = screener_router()
    ctx = setup(router, monkeypatch)
    losers = run(fd.get_market_movers(ctx, kind="losers", count=2))
    assert [m["change_pct"] for m in losers["movers"]] == [-9.0, -8.0] and losers["market_state"] == "open"
    active = json.loads(run(call_tool("get_market_movers", {"kind": "most_active", "count": "4"}, ctx)))
    assert [m["symbol"] for m in active["movers"]] == ["NVDA", "TSLA", "F", "AAPL"]
    bad = json.loads(run(call_tool("get_market_movers", {"kind": "sideways"}, ctx)))
    assert "kind must be one of" in bad["error"]


def test_market_movers_trending_enriched_via_v7(monkeypatch):
    router = with_crumb(Router())
    router.add(f"{Q1}/v1/finance/trending/US", json_response(trending_payload(TRENDING_SYMS)))
    rows = {s: mover_row(s, f"{s} name", 50.0 + i, 1.0 + i, 1000000) for i, s in enumerate(TRENDING_SYMS)}
    router.add(V7, v7_handler(rows))
    ctx = setup(router, monkeypatch)
    out = run(fd.get_market_movers(ctx, kind="trending", count=5))
    assert out["status"] == "5 trending tickers on Yahoo Finance (US)"
    assert [m["symbol"] for m in out["movers"]] == TRENDING_SYMS[:5]
    assert out["movers"][2] == {
        "symbol": "BTC-USD", "name": "BTC-USD name", "price": 52.0, "change_pct": 3.0, "change": 1.51,
        "volume": "1.00M", "avg_volume_3m": "500.00K", "market_cap": "24.50B", "exchange": "NasdaqGS",
    }
    assert router.hits("trending")[0].url.params.get("crumb") is None  # trending needs no crumb
    assert len(router.hits("v7/finance/quote")) == 1
    assert router.hits("v7/finance/quote")[0].url.params["symbols"] == "NVDA,TSLA,BTC-USD,PLTR,^GSPC"


def test_trending_region_setting_and_chart_fallback(monkeypatch):
    monkeypatch.setenv("FINANCE_REGION", "gb")
    router = with_crumb(Router())
    router.add(f"{Q1}/v1/finance/trending/GB", json_response(trending_payload(["BARC.L", "VOD.L"])))
    router.add(V7, httpx.Response(503, text="<html>busy</html>"))
    router.add(f"{Q1}/v8/finance/chart/BARC.L", chart_1d("BARC.L", 250.5, 245.0, shortName="Barclays", currency="GBp"))
    router.add(f"{Q1}/v8/finance/chart/VOD.L", chart_1d("VOD.L", 70.1, 71.0, shortName="Vodafone"))
    ctx = setup(router, monkeypatch)
    out = run(fd.get_market_movers(ctx, kind="trending"))
    assert out["region"] == "GB"
    assert [m["symbol"] for m in out["movers"]] == ["BARC.L", "VOD.L"]
    assert out["movers"][0]["price"] == 250.5 and out["movers"][0]["change_pct"] == 2.24
    assert "HTTP 503" in out["errors"][0]


def test_screener_rate_limited_cools_down(monkeypatch):
    router = with_crumb(Router())
    router.add(SCREENER, httpx.Response(429, text="Too Many Requests"))
    ctx = setup(router, monkeypatch)
    err = json.loads(run(call_tool("get_market_movers", {"kind": "losers"}, ctx)))["error"]
    assert "limiting requests" in err
    err2 = json.loads(run(call_tool("get_market_movers", {"kind": "gainers"}, ctx)))["error"]
    assert "limiting requests" in err2
    assert len(router.hits("screener")) == 1  # host paused after the 429


def test_host_limiter_spacing_and_cooldown(monkeypatch):
    monkeypatch.setattr(fd, "HOST_INTERVAL", 0.5)
    clock = {"t": 100.0}
    slept: list = []

    async def fake_sleep(s):
        slept.append(s)

    lim = fd.HostLimiter(clock=lambda: clock["t"], sleep=fake_sleep)
    assert lim.reserve("query1.finance.yahoo.com") == 0
    assert lim.reserve("query1.finance.yahoo.com") == 0.5
    assert lim.reserve("query2.finance.yahoo.com") == 0  # per host
    run(lim.wait("query1.finance.yahoo.com"))
    assert slept == [1.0]
    monkeypatch.setattr(fd, "MAX_QUEUE_WAIT", 1.2)
    with pytest.raises(fd.YahooRateLimited, match="Too many"):
        lim.reserve("query1.finance.yahoo.com")
    lim.cooldown("query2.finance.yahoo.com", 30)
    with pytest.raises(fd.YahooRateLimited, match="about 30 seconds"):
        lim.reserve("query2.finance.yahoo.com")
    clock["t"] += 31
    assert lim.reserve("query2.finance.yahoo.com") == 0


# ----------------------------------------------------------------------------- news
def news_payload():
    return {
        "explains": [], "count": 5, "quotes": [],
        "news": [
            {
                "uuid": "a1", "title": "Apple shares rise after record iPhone sales", "publisher": "Reuters",
                "link": "https://finance.yahoo.com/news/apple-shares-rise-120000123.html",
                "providerPublishTime": NOW - 7200, "type": "STORY",
                "thumbnail": {"resolutions": [{"url": "https://s.yimg.com/x.jpg", "width": 140, "height": 140, "tag": "140x140"}]},
                "relatedTickers": ["AAPL", "MSFT"],
            },
            {
                "uuid": "a2", "title": "Why Apple stock is moving today", "publisher": "Yahoo Finance Video",
                "link": "https://finance.yahoo.com/video/why-apple-stock-moving-130000456.html",
                "providerPublishTime": NOW - 600, "type": "VIDEO", "relatedTickers": ["AAPL"],
            },
            {"uuid": "a1", "title": "Apple shares rise after record iPhone sales", "publisher": "Reuters", "providerPublishTime": NOW - 7200},
            {"uuid": "a3", "title": "", "publisher": "Nobody", "providerPublishTime": NOW},
            {"uuid": "a4", "title": "Odd link item", "publisher": "Spam", "link": "javascript:alert(1)", "providerPublishTime": NOW - 3 * 86400},
        ],
        "nav": [], "lists": [], "researchReports": [], "screenerFieldResults": [], "totalTime": 26, "timeTakes": {"total": 26},
    }


def test_stock_news_parsing(monkeypatch):
    router = Router({SEARCH: json_response(news_payload())})
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_news(ctx, symbol="aapl", count=5))
    assert out["query"] == "AAPL" and out["source"] == "Yahoo Finance" and out["count"] == 3
    titles = [a["title"] for a in out["articles"]]
    assert titles == ["Why Apple stock is moving today", "Apple shares rise after record iPhone sales", "Odd link item"]
    first, second, third = out["articles"]
    assert first["age"] == "10m ago" and first["type"] == "video" and first["tickers"] == ["AAPL"]
    assert second["publisher"] == "Reuters" and second["age"] == "2h ago" and "type" not in second
    assert second["link"].startswith("https://finance.yahoo.com/news/")
    assert "link" not in third  # unsafe scheme dropped
    params = router.requests[0].url.params
    assert params["q"] == "AAPL" and params["newsCount"] == "5" and params["quotesCount"] == "0"
    assert "crumb" not in params
    # cached
    run(fd.get_stock_news(ctx, symbol="AAPL", count=5))
    assert len(router.requests) == 1


GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>"NVDA stock" - Google News</title>
<item><title>Nvidia rallies on data-center demand - CNBC</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>{pub}</pubDate><source url="https://www.cnbc.com">CNBC</source><description>Nvidia rallies</description></item>
</channel></rss>"""


def test_stock_news_falls_back_to_google_news(monkeypatch):
    pub = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    router = Router(
        {
            SEARCH: httpx.Response(500, text="<html>Internal error</html>"),
            "news.google.com/rss/search": httpx.Response(200, text=GOOGLE_RSS.format(pub=pub)),
        }
    )
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_stock_news", {"symbol": "NVDA", "count": 3}, ctx)))
    assert out["source"].startswith("Google News") and out["articles"][0]["title"] == "Nvidia rallies on data-center demand"
    assert out["articles"][0]["publisher"] == "CNBC" and out["articles"][0]["age"] == "3h ago"
    assert "HTTP 500" in out["errors"][0] and "<html" not in json.dumps(out)
    assert "NVDA stock" in router.hits("news.google.com")[0].url.params["q"]


def test_stock_news_topic_passed_as_symbol_is_searched_as_text(monkeypatch):
    router = Router({SEARCH: json_response(news_payload())})
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_news(ctx, symbol="Federal Reserve rate decision", count=2))
    assert out["query"] == "Federal Reserve rate decision" and out["count"] == 2
    assert router.requests[0].url.params["q"] == "Federal Reserve rate decision"


def test_stock_news_requires_topic_and_reports_total_failure(monkeypatch):
    ctx = make_ctx(http=mock_http(no_network))
    out = json.loads(run(call_tool("get_stock_news", {}, ctx)))
    assert "Say which ticker" in out["error"]
    router = Router({SEARCH: httpx.ConnectError("down"), "news.google.com": httpx.ConnectError("down")})
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_stock_news", {"query": "fed rate decision"}, ctx)))
    assert "Couldn't load news for fed rate decision" in out["error"] and "Yahoo Finance" in out["error"] and "Google News" in out["error"]


# ----------------------------------------------------------------------------- indicator maths
STOCKCHARTS = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]


def test_rsi_matches_stockcharts_reference():
    # StockCharts' published RSI(14) table for this series (their intermediate averages are rounded).
    for upto, published in ((15, 70.53), (16, 66.32), (20, 57.97), (33, 37.77)):
        assert fd.rsi(STOCKCHARTS[:upto], 14) == pytest.approx(published, abs=0.1)


def test_rsi_hand_computed_and_edges():
    # changes +1, -0.5, +1, -0.5; seed avg gain 0.5 / loss 0.25; Wilder: (0.75, 0.125) then (0.375, 0.3125)
    assert fd.rsi([10, 11, 10.5, 11.5, 11], 2) == pytest.approx(100 - 100 / (1 + 0.375 / 0.3125))
    assert fd.rsi(list(range(1, 30)), 14) == 100.0
    assert fd.rsi(list(range(30, 1, -1)), 14) == 0.0
    assert fd.rsi([5.0] * 20, 14) == 50.0
    assert fd.rsi([1, 2, 3], 14) is None


def test_sma_ema_macd_on_known_series():
    lin = [float(i) for i in range(1, 61)]
    assert fd.sma(lin, 20) == pytest.approx(statistics.mean(lin[-20:])) == 50.5
    assert fd.sma(lin, 100) is None
    assert fd.ema_series([float(i) for i in range(1, 11)], 3) == [None, None, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    # hand-computed EMA(3), alpha 0.5, seeded with SMA(10, 12, 11) = 11
    assert fd.ema_series([10, 12, 11, 15, 13], 3)[-2:] == [13.0, 13.0]
    # on a straight line each EMA lags by (n-1)/2, so MACD = 5.5 * ... = (12.5 - 5.5) * slope = 7, signal 7, histogram 0
    m = fd.macd(lin)
    assert m["macd"] == pytest.approx(7.0) and m["signal"] == pytest.approx(7.0) and m["histogram"] == pytest.approx(0.0, abs=1e-9)
    m2 = fd.macd([2 * x for x in lin])
    assert m2["macd"] == pytest.approx(14.0)
    assert fd.macd(lin[:30]) is None  # needs 26 + 9 - 1 points
    assert fd.sma_series([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_volatility_hand_computed():
    a = math.log(1.01)
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    expected = a * math.sqrt(30 / 29) * math.sqrt(252) * 100  # 15 x (+a), 15 x (-a), mean 0, sample stdev
    assert fd.annualised_volatility(closes) == pytest.approx(expected)
    assert fd.annualised_volatility(closes, periods_per_year=365) == pytest.approx(expected * math.sqrt(365 / 252))
    assert fd.annualised_volatility([100 * 1.01**i for i in range(40)]) == pytest.approx(0.0, abs=1e-9)
    assert fd.annualised_volatility([1.0] * 10) is None


def test_cross_detection_and_levels():
    assert fd.last_cross([None, -2, -1, 1, 2, 3]) == ("above", 2)
    assert fd.last_cross([1, 2, -1]) == ("below", 0)
    assert fd.last_cross([1, 2, 3]) == ("above", None)
    assert fd.last_cross([None, None]) is None
    zig = [100, 102, 104, 110, 104, 102, 100, 96, 95, 96, 98, 100, 106, 115, 106, 100, 94, 90, 94, 97, 100, 100, 100, 100]
    lv = fd.swing_levels(zig, zig, 100.0)
    assert lv == {"support": [95, 90], "resistance": [110, 115]}
    # levels within 1.5% merge into the nearer one
    zig2 = [100, 103, 110, 103, 100, 104, 111, 104, 100, 100, 100, 100]
    assert fd.swing_levels(zig2, zig2, 100.0, window=2)["resistance"] == [110]


def test_period_returns():
    days = [date(2025, 12, 29) + timedelta(days=i) for i in range(10)]
    closes = [100.0 + i for i in range(10)]  # last close 109 on 2026-01-07
    assert fd.period_return(days, closes, date(2025, 12, 31), tolerance_days=0) == pytest.approx((109 / 102 - 1) * 100, abs=0.01)
    assert fd.period_return(days, closes, date(2025, 12, 31) - timedelta(days=7), tolerance_days=0) is None
    assert fd.period_return(days, closes, date(2025, 12, 27)) == pytest.approx(9.0)  # within tolerance: first bar
    assert fd._months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert fd._months_back(date(2026, 1, 15), 12) == date(2025, 1, 15)


def wave(n=260):
    return [round(100 + 10 * math.sin(i / 9) + 0.08 * i, 4) for i in range(n)]


def test_get_technicals_end_to_end(monkeypatch):
    closes = wave()
    router = Router({f"{Q1}/v8/finance/chart/MSFT": json_response(daily_chart(closes, gap_at=100))})
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_technicals", {"symbol": "msft"}, ctx)))
    params = router.requests[0].url.params
    assert params["range"] == "1y" and params["interval"] == "1d" and "crumb" not in params
    assert out["symbol"] == "MSFT" and out["name"] == "Microsoft Corporation" and out["sessions"] == 260
    assert out["price"] == round(closes[-1], 2)
    ma = out["moving_averages"]
    assert ma["sma20"] == round(statistics.mean(closes[-20:]), 2)
    assert ma["sma50"] == round(statistics.mean(closes[-50:]), 2)
    assert ma["sma200"] == round(statistics.mean(closes[-200:]), 2)
    assert ma["ema12"] == round(fd.ema_series(closes, 12)[-1], 2)
    m = fd.macd(closes)
    assert out["macd"]["macd"] == round(m["macd"], 4) and out["macd"]["histogram"] == round(m["histogram"], 4)
    assert out["rsi14"] == round(fd.rsi(closes), 1)
    rng = out["range_52w"]
    assert rng["high"] == round(max(round(c * 1.01, 4) for c in closes[-252:]), 2)  # fixture highs are close * 1.01
    assert rng["pct_below_high"] >= 0 and rng["pct_above_low"] >= 0
    assert set(out["returns_pct"]) == {"1w", "1m", "3m", "ytd", "1y"}
    assert out["volatility_30d_annualised_pct"] == round(fd.annualised_volatility(closes), 1)
    assert out["trend_cross"]["state"].startswith(("golden", "death"))
    assert out["levels"]["support"] or out["levels"]["resistance"]
    assert out["status"].startswith(f"MSFT {out['price']}") and "RSI" in out["status"]
    assert out["signals"] and len(json.dumps(out)) < 4000


@pytest.mark.parametrize("series", [wave(), [100 + i for i in range(260)], [400 - i for i in range(260)], [100 + (i % 7) for i in range(260)]])
def test_technical_signals_never_give_advice(series):
    bars = fd.parse_chart(daily_chart(series)["chart"]["result"][0])
    t = fd.compute_technicals(bars)
    text = " ".join(t["signals"]).lower()
    assert t["signals"]
    assert not re.search(r"\b(buy|sell|buying|selling|should|recommend)\b", text), text


def test_technicals_rising_series_reads_as_uptrend():
    bars = fd.parse_chart(daily_chart([100 + i * 0.5 for i in range(260)])["chart"]["result"][0])
    t = fd.compute_technicals(bars)
    assert t["rsi14"] == 100.0
    assert "Price is above both its 50-day and 200-day moving averages." in t["signals"]
    assert t["trend_cross"]["state"].startswith("golden") and "last_cross_sessions_ago" not in t["trend_cross"]
    assert "levels" not in t  # a straight line has no swing highs/lows
    assert t["returns_pct"]["1w"] == pytest.approx((229.5 / 227.0 - 1) * 100, abs=0.01)


def test_technicals_short_history_and_crypto(monkeypatch):
    router = Router(
        {
            f"{Q1}/v8/finance/chart/NEWCO": json_response(daily_chart([10 + i * 0.1 for i in range(20)], symbol="NEWCO")),
            f"{Q1}/v8/finance/chart/BTC-USD": json_response(daily_chart(wave(120), symbol="BTC-USD", instrument="CRYPTOCURRENCY")),
        }
    )
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_technicals", {"symbol": "NEWCO"}, ctx)))
    assert "Not enough daily price history for NEWCO" in out["error"]
    out = json.loads(run(call_tool("get_technicals", {"symbol": "BTC-USD", "range": "6mo"}, ctx)))
    assert "sma200" not in out["moving_averages"] and "trend_cross" not in out
    assert "range_period" in out and "range_52w" not in out
    bars = fd.parse_chart(daily_chart(wave(120), instrument="CRYPTOCURRENCY")["chart"]["result"][0])
    assert out["volatility_30d_annualised_pct"] == round(fd.annualised_volatility(bars["adjclose"], 30, 365), 1)


def test_technicals_unknown_symbol(monkeypatch):
    router = Router({f"{Q1}/v8/finance/chart/ZZZZ": json_response({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}, 404)})
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_technicals", {"symbol": "ZZZZ"}, ctx)))
    assert out["error"] == "Unknown ticker 'ZZZZ'. Try lookup_ticker."


# ----------------------------------------------------------------------------- yield curve
TREASURY_XML = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<feed xml:base="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml" xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <title type="text">DailyTreasuryYieldCurveRateData</title>
  <id>https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml</id>
  <updated>2026-11-04T09:00:00Z</updated>
  <link rel="self" title="DailyTreasuryYieldCurveRateData" href="DailyTreasuryYieldCurveRateData" />
  <entry>
    <id>https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?$skip=0&amp;$top=1</id>
    <title type="text"></title><updated>2026-11-04T09:00:00Z</updated><author><name /></author>
    <link rel="edit" title="DailyTreasuryYieldCurveRateDatum" href="DailyTreasuryYieldCurveRateData(9001)" />
    <category term="TreasuryDataWarehouseModel.DailyTreasuryYieldCurveRateDatum" scheme="http://schemas.microsoft.com/ado/2007/08/dataservices/scheme" />
    <content type="application/xml">
      <m:properties>
        <d:Id m:type="Edm.Int32">9001</d:Id>
        <d:NEW_DATE m:type="Edm.DateTime">2026-11-02T00:00:00</d:NEW_DATE>
        <d:BC_1MONTH m:type="Edm.Double">4.01</d:BC_1MONTH><d:BC_2MONTH m:type="Edm.Double">3.98</d:BC_2MONTH>
        <d:BC_3MONTH m:type="Edm.Double">3.95</d:BC_3MONTH><d:BC_4MONTH m:type="Edm.Double">3.92</d:BC_4MONTH>
        <d:BC_6MONTH m:type="Edm.Double">3.85</d:BC_6MONTH><d:BC_1YEAR m:type="Edm.Double">3.70</d:BC_1YEAR>
        <d:BC_2YEAR m:type="Edm.Double">3.55</d:BC_2YEAR><d:BC_3YEAR m:type="Edm.Double">3.58</d:BC_3YEAR>
        <d:BC_5YEAR m:type="Edm.Double">3.70</d:BC_5YEAR><d:BC_7YEAR m:type="Edm.Double">3.88</d:BC_7YEAR>
        <d:BC_10YEAR m:type="Edm.Double">4.05</d:BC_10YEAR><d:BC_20YEAR m:type="Edm.Double">4.55</d:BC_20YEAR>
        <d:BC_30YEAR m:type="Edm.Double">4.62</d:BC_30YEAR><d:BC_30YEARDISPLAY m:type="Edm.Double">4.62</d:BC_30YEARDISPLAY>
      </m:properties>
    </content>
  </entry>
  <entry>
    <content type="application/xml">
      <m:properties>
        <d:NEW_DATE m:type="Edm.DateTime">2026-11-03T00:00:00</d:NEW_DATE>
        <d:BC_1MONTH m:type="Edm.Double">4.02</d:BC_1MONTH><d:BC_3MONTH m:type="Edm.Double">3.96</d:BC_3MONTH>
        <d:BC_2YEAR m:type="Edm.Double">3.60</d:BC_2YEAR><d:BC_5YEAR m:type="Edm.Double">3.72</d:BC_5YEAR>
        <d:BC_10YEAR m:type="Edm.Double">4.08</d:BC_10YEAR><d:BC_30YEAR m:type="Edm.Double">4.66</d:BC_30YEAR>
        <d:BC_4MONTH m:type="Edm.Double" m:null="true" />
      </m:properties>
    </content>
  </entry>
</feed>"""


def yields_router(irx=5.25, fvx=4.10, tnx=4.30, tyx=4.50, prev_delta=0.05):
    r = Router()
    for sym, val in (("^IRX", irx), ("^FVX", fvx), ("^TNX", tnx), ("^TYX", tyx)):
        if val is None:
            r.add(f"{Q1}/v8/finance/chart/{sym}", httpx.Response(500))
            r.add(f"{Q2}/v8/finance/chart/{sym}", httpx.Response(500))
        else:
            r.add(f"{Q1}/v8/finance/chart/{sym}", chart_1d(sym, val, round(val - prev_delta * (10 if val > 25 else 1), 4)))
    return r


def test_yield_curve_inverted(monkeypatch):
    ctx = setup(yields_router(), monkeypatch)
    out = run(fd.get_yield_curve(ctx))
    assert [c["tenor"] for c in out["curve"]] == ["3M", "5Y", "10Y", "30Y"]
    assert out["spread_10y_3m_pct"] == pytest.approx(-0.95) and out["inverted_10y_3m"] is True
    assert out["shape"] == "inverted" and out["status"] == "10Y-3M spread -0.95 pts (-95 bp): curve inverted"
    assert out["curve"][2]["change_bp"] == pytest.approx(5.0)
    assert out["spread_30y_5y_pct"] == pytest.approx(0.4)
    assert "official" not in out and "errors" not in out


def test_yield_curve_normal_and_tenfold_quotes(monkeypatch):
    ctx = setup(yields_router(irx=3.80, fvx=37.5, tnx=43.0, tyx=46.2), monkeypatch)
    out = run(fd.get_yield_curve(ctx))
    by = {c["tenor"]: c for c in out["curve"]}
    assert by["10Y"]["yield_pct"] == 4.3 and by["10Y"]["change_bp"] == pytest.approx(5.0)
    assert out["spread_10y_3m_pct"] == pytest.approx(0.5) and out["inverted_10y_3m"] is False
    assert out["shape"] == "normal (upward sloping)"


def test_yield_curve_fills_gaps_from_treasury_and_official(monkeypatch):
    router = yields_router(irx=None)
    router.add(TREASURY, httpx.Response(200, text=TREASURY_XML, headers={"content-type": "application/xml"}))
    ctx = setup(router, monkeypatch)
    out = run(fd.get_yield_curve(ctx, official="true"))
    three = out["curve"][0]
    assert three == {"tenor": "3M", "yield_pct": 3.96, "as_of": "2026-11-03", "source": "US Treasury (end of day)"}
    assert out["spread_10y_3m_pct"] == pytest.approx(4.30 - 3.96)
    assert "^IRX" in out["errors"][0]
    off = out["official"]
    assert off["date"] == "2026-11-03" and off["yields_pct"]["2Y"] == 3.6 and "4M" not in off["yields_pct"]
    assert off["spread_10y_2y_pct"] == pytest.approx(0.48) and off["inverted_10y_2y"] is False
    req = router.hits("home.treasury.gov")[0]
    assert req.url.params["data"] == "daily_treasury_yield_curve" and re.fullmatch(r"\d{6}", req.url.params["field_tdr_date_value_month"])
    assert "Mozilla/5.0" in req.headers["user-agent"]


def test_treasury_previous_month_fallback_and_parse_edges(monkeypatch):
    empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    months = []

    def handler(request):
        months.append(request.url.params["field_tdr_date_value_month"])
        return httpx.Response(200, text=empty if len(months) == 1 else TREASURY_XML)

    ctx = setup(Router({TREASURY: handler}), monkeypatch)
    latest = run(fd.treasury_curve(ctx, today=date(2026, 12, 1)))
    assert months == ["202612", "202611"] and latest["date"] == "2026-11-03"
    assert fd.parse_treasury_xml("<<not xml") == []
    assert [r["date"] for r in fd.parse_treasury_xml(TREASURY_XML)] == ["2026-11-02", "2026-11-03"]


def test_yield_curve_total_failure_is_friendly(monkeypatch):
    router = yields_router(irx=None, fvx=None, tnx=None, tyx=None)
    router.add(TREASURY, httpx.Response(503, text="<html>maintenance</html>"))
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_yield_curve", {}, ctx)))
    assert out["error"].startswith("Treasury yields are unavailable right now") and "<html" not in out["error"]


# ----------------------------------------------------------------------------- market pulse panel
def pulse_router():
    router = screener_router()
    router.add(f"{Q1}/v1/finance/trending/US", json_response(trending_payload(TRENDING_SYMS)))
    rows = {s: mover_row(s, f"{s} name", 50.0 + i, 1.0 + i, 1000000) for i, s in enumerate(TRENDING_SYMS)}
    router.add(V7, v7_handler(rows))
    return router


def test_market_pulse_success_and_cache(monkeypatch):
    router = pulse_router()
    ctx = setup(router, monkeypatch)
    out = run(fd.market_pulse(ctx))
    assert out["status"] == "ok" and out["market_state"] == "open" and out["errors"] == []
    assert [len(out[k]) for k in ("gainers", "losers", "most_active", "trending")] == [5, 5, 5, 8]
    for key in ("gainers", "losers", "most_active", "trending"):
        for item in out[key]:
            assert set(item) <= {"symbol", "name", "price", "change_pct"} and item["symbol"]
    assert out["gainers"][0] == {"symbol": "SMCI", "name": "Super Micro Computer, Inc.", "price": 41.3, "change_pct": 18.52}
    n = len(router.requests)
    again = run(fd.market_pulse(ctx))
    assert again == out and len(router.requests) == n
    again["gainers"].clear()  # callers can't corrupt the cache
    assert len(run(fd.market_pulse(ctx))["gainers"]) == 5


def test_market_pulse_never_raises_when_everything_fails(monkeypatch):
    router = Router()
    for host in ("fc.yahoo.com", Q1, Q2):
        router.add(host, httpx.ConnectError("network down"))
    ctx = setup(router, monkeypatch)
    out = run(fd.market_pulse(ctx))
    assert out["status"] == "unavailable" and out["market_state"] in {"open", "closed", "pre-market", "after-hours"}
    assert all(out[k] == [] for k in ("gainers", "losers", "most_active", "trending"))
    assert out["errors"] and all("Traceback" not in e for e in out["errors"])


def test_market_pulse_never_raises_on_unexpected_errors(monkeypatch):
    ctx = setup(Router(), monkeypatch)  # every request trips an AssertionError inside the transport
    out = run(fd.market_pulse(ctx))
    assert out["status"] == "unavailable" and out["errors"]

    async def boom(ctx):
        raise RuntimeError("kaboom")

    fd._CACHE.clear()
    monkeypatch.setattr(fd, "_pulse", boom)
    out = run(fd.market_pulse(make_ctx(http=None)))
    assert out["status"] == "unavailable" and out["errors"] == ["market pulse: RuntimeError"]

    async def slow(ctx):
        await asyncio.sleep(5)

    fd._CACHE.clear()  # failed pulses are cached briefly (PULSE_PARTIAL_TTL) so Yahoo isn't hammered
    monkeypatch.setattr(fd, "_pulse", slow)
    monkeypatch.setattr(fd, "PULSE_TIMEOUT", 0.05)
    out = run(fd.market_pulse(make_ctx(http=None)))
    assert out["status"] == "unavailable" and "didn't answer in time" in out["errors"][0]


def test_market_pulse_partial(monkeypatch):
    router = pulse_router()
    router.add(f"{Q1}/v1/finance/trending/US", httpx.Response(502, text="bad gateway"))
    ctx = setup(router, monkeypatch)
    out = run(fd.market_pulse(ctx))
    assert out["status"] == "partial" and out["trending"] == [] and len(out["gainers"]) == 5
    assert out["errors"] == ["trending: Yahoo Finance's trending tickers is unavailable right now (HTTP 502)."]


# ----------------------------------------------------------------------------- adversarial review regressions
def test_crumb_refresh_starts_a_fresh_cookie_session(monkeypatch):
    # Like the real thing: fc.yahoo.com only sets A3 for a browser that has none, and getcrumb hands back the same
    # crumb for the same cookie. A refresh must drop the cookie first, or it just gets the rejected crumb again.
    issued: list = []

    def fc(request):
        if "A3=" in request.headers.get("cookie", ""):
            return httpx.Response(404, text="<html>Not found</html>")
        issued.append(len(issued) + 1)
        cookie = f"A3=d=sess{issued[-1]}; Domain=.yahoo.com; Path=/; Secure; HttpOnly"
        return httpx.Response(404, headers=[("set-cookie", cookie)], text="<html>Not found</html>")

    def getcrumb(request):
        sess = re.search(r"A3=d=(sess\d+)", request.headers.get("cookie", ""))
        return httpx.Response(200, text=f"crumb-{sess.group(1)}" if sess else "")

    good = v7_handler({"AAPL": AAPL_ROW})

    def v7(request):
        return invalid_crumb() if request.url.params["crumb"] == "crumb-sess1" else good(request)

    router = Router().add("fc.yahoo.com", fc).add(CRUMB, getcrumb).add(V7, v7)
    setup(router, monkeypatch)
    assert run(fd.v7_quotes(["AAPL"]))["AAPL"]["regularMarketPrice"] == 229.87
    assert issued == [1, 2] and fd._SESSION.crumb_value == "crumb-sess2"


def test_slow_crumb_handshake_falls_back_to_chart(monkeypatch):
    monkeypatch.setattr(fd, "CRUMB_TIMEOUT", 0.05)

    async def slow_fc(request):
        await asyncio.sleep(3)
        return fc_cookie(request)

    router = Router().add("fc.yahoo.com", slow_fc)
    router.add(f"{Q1}/v8/finance/chart/AAPL", chart_1d("AAPL", 229.87, 227.08, shortName="Apple Inc.", instrumentType="EQUITY"))
    ctx = setup(router, monkeypatch)
    t0 = time.monotonic()
    out = run(fd.get_stock_details(ctx, "AAPL"))
    assert time.monotonic() - t0 < 2
    assert out["quote"]["price"] == 229.87 and "chart" in out["source"]
    assert out["errors"] == ["quote: Yahoo Finance didn't answer in time while starting a session."]
    with pytest.raises(fd.YahooError, match="didn't answer in time"):  # backs off instead of waiting again
        run(fd._SESSION.crumb())
    assert len(router.hits("fc.yahoo.com")) == 1


def test_slow_quote_services_leave_time_for_chart_fallback(monkeypatch):
    monkeypatch.setattr(fd, "PRIMARY_TIMEOUT", 0.1)

    async def slow(request):
        await asyncio.sleep(3)
        return json_response({})

    router = with_crumb(Router()).add(V7, slow).add(V10 + "AAPL", slow)
    router.add(f"{Q1}/v8/finance/chart/AAPL", chart_1d("AAPL", 229.87, 227.08, instrumentType="EQUITY"))
    ctx = setup(router, monkeypatch)
    t0 = time.monotonic()
    out = json.loads(run(call_tool("get_stock_details", {"symbol": "AAPL"}, ctx)))
    assert time.monotonic() - t0 < 2
    assert out["quote"]["price"] == 229.87 and out["errors"] == ["quote: Yahoo Finance's quote service didn't answer in time."]


def test_consent_redirect_on_a_data_call_blocks_the_session(monkeypatch):
    router = with_crumb(Router())
    router.add(V7, httpx.Response(302, headers={"location": "https://consent.yahoo.com/v2/collectConsent?sessionId=3_cc"}))
    router.add("consent.yahoo.com", httpx.Response(200, text="<html><form>Accept all</form></html>"))
    setup(router, monkeypatch)
    for _ in range(2):
        with pytest.raises(fd.YahooConsentRequired):
            run(fd.v7_quotes(["AAPL"]))
    assert len(router.hits("v7/finance/quote")) == 1  # the second call didn't go round the consent pages again


def test_html_reply_from_v7_falls_back_to_chart_but_keeps_fundamentals(monkeypatch):
    router = with_crumb(Router())
    router.add(V7, httpx.Response(200, text="<!doctype html><html><body>Will be right back</body></html>", headers={"content-type": "text/html"}))
    router.add(V10 + "AAPL", json_response(AAPL_SUMMARY))
    router.add(f"{Q1}/v8/finance/chart/AAPL", chart_1d("AAPL", 229.87, 227.08, shortName="Apple Inc.", instrumentType="EQUITY"))
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_stock_details", {"symbol": "AAPL"}, ctx)))
    assert out["quote"]["price"] == 229.87 and "chart" in out["source"]
    assert out["valuation"]["market_cap"] == "3.46T" and out["analysts"]["consensus"] == "buy"
    assert len(out["errors"]) == 1 and "isn't data" in out["errors"][0]
    assert "<html" not in json.dumps(out) and "Xk9/abC.d3F" not in json.dumps(out)


def test_stock_details_uses_quote_summary_price_when_quotes_fail(monkeypatch):
    router = with_crumb(Router())
    router.add(V7, httpx.Response(503, text="busy"))
    router.add(V10 + "AAPL", json_response(AAPL_SUMMARY))
    for host in (Q1, Q2):
        router.add(f"{host}/v8/finance/chart/AAPL", httpx.Response(500, text="<html>oops</html>"))
    ctx = setup(router, monkeypatch)
    out = run(fd.get_stock_details(ctx, "AAPL"))
    assert out["status"].startswith("AAPL 229.87") and "live price unavailable" in out["source"]
    assert out["analysts"]["implied_upside_pct"] == pytest.approx((252.86 / 229.87 - 1) * 100, abs=0.01)
    assert "<html" not in json.dumps(out)


def test_build_details_stale_calendar_date_is_not_reported_as_next():
    now = 1_800_000_000
    summary = json.loads(json.dumps(AAPL_SUMMARY))["quoteSummary"]["result"][0]
    earn = summary["calendarEvents"]["earnings"]
    passed = datetime.fromtimestamp(now - 3 * 86400, tz=timezone.utc).date().isoformat()
    earn["earningsDate"] = [{"raw": now - 3 * 86400, "fmt": passed}]
    earn["earningsAverage"] = raw(9.99)  # the estimate for the quarter just reported
    earn["revenueAverage"] = {"raw": 999e9, "fmt": "999B"}
    row = dict(AAPL_ROW, earningsTimestampStart=None, earningsTimestampEnd=None, earningsTimestamp=now - 40 * 86400)
    e = fd.build_details("AAPL", row, summary, None, now=now)["earnings"]
    assert "next_date" not in e and e["last_date"] == passed
    assert e["eps_estimate"] == 2.67 and e["revenue_estimate"] == "138.47B"  # from earningsTrend's upcoming quarter
    # still upcoming: calendarEvents wins
    earn["earningsDate"] = [{"raw": now + 20 * 86400, "fmt": "2027-02-03"}]
    e = fd.build_details("AAPL", row, summary, None, now=now)["earnings"]
    assert e["next_date"] == "2027-02-03" and e["eps_estimate"] == 9.99 and "last_date" not in e


def test_parse_chart_keeps_one_bar_per_session():
    res = daily_chart([100.0 + i for i in range(60)])["chart"]["result"][0]
    ts, q = res["timestamp"], res["indicators"]["quote"][0]
    ts.append(ts[-1] + 5 * 3600)  # Yahoo's live bar: same session, later timestamp
    ts.append(ts[0] - 86400)  # and an out-of-order row
    for key in ("open", "high", "low", "close"):
        q[key] += [161.0, 50.0]
    res["indicators"]["adjclose"][0]["adjclose"] += [161.0, 50.0]
    bars = fd.parse_chart(res)
    assert len(bars["dates"]) == 60 and len(set(bars["dates"])) == 60 and bars["dates"] == sorted(bars["dates"])
    assert bars["close"][-1] == 161.0 and bars["close"][-2] == 158.0
    assert all(len(bars[k]) == 60 for k in ("close", "high", "low", "adjclose"))


def test_daily_bars_429_on_query1_still_tries_query2(monkeypatch):
    router = Router()
    router.add(f"{Q1}/v8/finance/chart/MSFT", httpx.Response(429, text="Too Many Requests"))
    router.add(f"{Q2}/v8/finance/chart/MSFT", json_response(daily_chart(wave())))
    router.add(f"{Q1}/v8/finance/chart/NVDA", httpx.Response(429, text="Too Many Requests"))
    router.add(f"{Q2}/v8/finance/chart/NVDA", httpx.Response(429, text="Too Many Requests"))
    ctx = setup(router, monkeypatch)
    out = json.loads(run(call_tool("get_technicals", {"symbol": "MSFT"}, ctx)))
    assert out["sessions"] == 260
    err = json.loads(run(call_tool("get_technicals", {"symbol": "NVDA"}, ctx)))["error"]
    assert "limiting requests" in err
    assert len(router.hits("query1.finance.yahoo.com/v8/finance/chart/NVDA")) == 0  # query1 still cooling down


def google_rss(n, link_len=520):
    items = []
    for i in range(n):
        link = "https://news.google.com/rss/articles/CBMi" + "x" * (link_len - 45) + f"{i:04d}?oc=5"
        if i == 1:
            link = "javascript:alert(1)"
        if i == 2:
            link = "https://news.google.com/rss/articles/" + "y" * 900
        title = ("Nvidia results recap and analysis " * (12 if i == 0 else 2)).strip()
        items.append(
            f"<item><title>{title} {i} - Pub {i}</title><link>{link}</link>"
            f"<pubDate>Wed, 04 Nov 2026 10:{59 - i:02d}:00 GMT</pubDate><source url='https://pub.example'>Pub {i}</source></item>"
        )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{''.join(items)}</channel></rss>"


def test_stock_news_google_fallback_is_sanitised_and_fits_the_budget(monkeypatch):
    router = Router({SEARCH: httpx.Response(500, text="x"), "news.google.com/rss/search": httpx.Response(200, text=google_rss(25))})
    ctx = setup(router, monkeypatch)
    raw_out = run(call_tool("get_stock_news", {"symbol": "NVDA", "count": 20}, ctx))
    out = json.loads(raw_out)
    assert len(raw_out) < 11000  # the agent clips at 14,000, which would break the JSON
    assert 5 <= out["count"] == len(out["articles"]) < 20 and "Trimmed to" in out["note"]
    assert all(len(a["title"]) <= 220 for a in out["articles"])
    assert all(a["link"].startswith("https://") and len(a["link"]) <= fd.MAX_LINK for a in out["articles"] if "link" in a)
    assert out["articles"][0]["title"].endswith("…")
    assert "link" not in out["articles"][1] and "link" not in out["articles"][2]  # javascript: / over-long


def test_yahoo_news_long_list_fits_the_budget(monkeypatch):
    news = [
        {
            "uuid": f"u{i}", "title": "Long headline " * 30, "publisher": "Publisher " * 10,
            "link": "https://finance.yahoo.com/news/" + "z" * 600, "providerPublishTime": NOW - i * 60,
            "type": "STORY", "relatedTickers": [f"T{j}" for j in range(12)],
        }
        for i in range(20)
    ]
    router = Router({SEARCH: json_response({"news": news, "quotes": []})})
    ctx = setup(router, monkeypatch)
    raw_out = run(call_tool("get_stock_news", {"symbol": "AAPL", "count": 20}, ctx))
    assert len(raw_out) < 11000 and json.loads(raw_out)["articles"][0]["tickers"] == [f"T{j}" for j in range(6)]


def test_crypto_news_fallback_query_has_no_stock_suffix(monkeypatch):
    router = Router({SEARCH: httpx.Response(500, text="x"), "news.google.com/rss/search": httpx.Response(200, text=google_rss(3))})
    ctx = setup(router, monkeypatch)
    run(fd.get_stock_news(ctx, symbol="BTC-USD", count=3))
    q = router.hits("news.google.com")[0].url.params["q"]
    assert q.startswith("BTC-USD") and "stock" not in q


def test_market_pulse_concurrent_callers_share_one_refresh(monkeypatch):
    router = pulse_router()
    inner = router.routes[SCREENER]

    async def slow_screener(request):  # yield to the loop so the callers really overlap
        await asyncio.sleep(0.02)
        return inner(request)

    router.add(SCREENER, slow_screener)
    ctx = setup(router, monkeypatch)

    async def together():
        return await asyncio.gather(*(fd.market_pulse(ctx) for _ in range(4)))

    outs = run(together())
    assert outs[0]["status"] == "ok" and all(o == outs[0] for o in outs)
    assert len(router.hits("v1/finance/screener")) == 3 and len(router.hits("finance/trending")) == 1
    assert len(router.hits("getcrumb")) == 1
    outs[0]["gainers"].clear()
    assert len(outs[1]["gainers"]) == 5  # every caller gets its own copy

    async def one_caller_leaves():
        fd._CACHE.clear()
        first = asyncio.ensure_future(fd.market_pulse(ctx))
        second = asyncio.ensure_future(fd.market_pulse(ctx))
        await asyncio.sleep(0)
        first.cancel()
        return await second

    assert run(one_caller_leaves())["status"] == "ok"
    assert len(fd._PULSE_INFLIGHT) == 0  # finished refreshes don't pin their event loop


def test_parse_news_odd_timestamps():
    now = datetime.fromtimestamp(NOW, tz=timezone.utc)
    items = fd.parse_news(
        {"news": [
            {"uuid": "ms", "title": "Milliseconds", "providerPublishTime": (NOW - 120) * 1000},
            {"uuid": "huge", "title": "Overflow", "providerPublishTime": 1e300},
            {"uuid": "neg", "title": "Negative", "providerPublishTime": -5},
        ]},
        now=now,
    )
    by = {i["title"]: i for i in items}
    assert by["Milliseconds"]["age"] == "2m ago" and set(by) == {"Milliseconds", "Overflow", "Negative"}
    assert "published" not in by["Overflow"] and "published" not in by["Negative"]


def test_trending_unknown_region_is_friendly(monkeypatch):
    router = Router()
    router.add(f"{Q1}/v1/finance/trending/ZZ", json_response({"finance": {"result": None, "error": {"code": "Not Found", "description": "Not Found"}}}, 404))
    ctx = setup(router, monkeypatch)
    err = json.loads(run(call_tool("get_market_movers", {"kind": "trending", "region": "zz"}, ctx)))["error"]
    assert "no trending list for region 'ZZ'" in err


# ----------------------------------------------------------------------------- misc
def test_clock_market_state_and_fixed_dst():
    from zoneinfo import ZoneInfo

    utc = timezone.utc
    assert fd.clock_market_state(datetime(2026, 11, 4, 15, 0, tzinfo=utc)) == "open"  # 10:00 EST
    assert fd.clock_market_state(datetime(2026, 7, 1, 13, 0, tzinfo=utc)) == "pre-market"  # 09:00 EDT
    assert fd.clock_market_state(datetime(2026, 7, 1, 21, 0, tzinfo=utc)) == "after-hours"  # 17:00 EDT
    assert fd.clock_market_state(datetime(2026, 11, 7, 16, 0, tzinfo=utc)) == "closed"  # Saturday
    for dt in (datetime(2026, 3, 8, 6, 59, tzinfo=utc), datetime(2026, 3, 8, 7, 0, tzinfo=utc), datetime(2026, 11, 1, 5, 59, tzinfo=utc),
               datetime(2026, 11, 1, 6, 0, tzinfo=utc), datetime(2027, 1, 15, 12, 0, tzinfo=utc), datetime(2027, 6, 15, 12, 0, tzinfo=utc)):
        assert fd._eastern_fixed(dt).utcoffset() == dt.astimezone(ZoneInfo("America/New_York")).utcoffset(), dt


def test_tools_registered_with_clear_descriptions():
    from app.tools import TOOLS

    for name in ("get_stock_details", "get_market_movers", "get_technicals"):
        assert "15 minutes" in TOOLS[name].description, name
    for name in ("get_stock_details", "get_market_movers", "get_stock_news", "get_technicals", "get_yield_curve"):
        t = TOOLS[name]
        assert "Use it" in t.description and t.gate is None and t.preview is None
        assert set(t.parameters["required"]) <= set(t.parameters["properties"])
    assert TOOLS["get_market_movers"].parameters["properties"]["kind"]["enum"] == ["gainers", "losers", "most_active", "trending"]
