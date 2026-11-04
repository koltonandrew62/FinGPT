"""Market data via Yahoo Finance's public chart endpoint (stocks, indices, crypto, FX, futures)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import quote

from ..util import BROWSER_UA
from . import ToolError, tool

FRIENDLY = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ",
    "^DJI": "DOW",
    "^RUT": "RUSSELL 2K",
    "^VIX": "VIX",
    "^TNX": "US 10Y",
    "BTC-USD": "BITCOIN",
    "ETH-USD": "ETHER",
    "SOL-USD": "SOLANA",
    "GC=F": "GOLD",
    "SI=F": "SILVER",
    "CL=F": "CRUDE",
    "DX-Y.NYB": "DOLLAR IDX",
    "EURUSD=X": "EUR/USD",
}
RANGES = {"1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1d", "ytd": "1d", "1y": "1wk", "5y": "1mo"}
_CACHE: dict[str, tuple[float, dict]] = {}


async def fetch_quote(http, symbol: str, range_: str = "1d") -> dict:
    symbol = symbol.strip().upper()
    range_ = range_ if range_ in RANGES else "1d"
    key = f"{symbol}:{range_}"
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    last_exc: Exception | None = None
    for host in ("query1", "query2"):
        try:
            r = await http.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol)}",
                params={"range": range_, "interval": RANGES[range_], "includePrePost": "false"},
                headers={"User-Agent": BROWSER_UA},
                timeout=12,
            )
            if r.status_code == 404:
                raise ToolError(f"Unknown ticker '{symbol}'. Try lookup_ticker.")
            r.raise_for_status()
            result = (r.json().get("chart") or {}).get("result")
            if not result:
                raise ToolError(f"No data for '{symbol}'.")
            data = result[0]
            break
        except ToolError:
            raise
        except Exception as exc:
            last_exc = exc
    else:
        raise ToolError(f"Market data unavailable for {symbol}: {last_exc}")

    meta = data.get("meta", {})
    closes = [c for c in (((data.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if range_ == "1d" and meta.get("previousClose"):
        prev = meta["previousClose"]
    change = (price - prev) if price is not None and prev else None
    pct = (change / prev * 100) if change is not None and prev else None
    step = max(1, len(closes) // 40)
    out = {
        "symbol": symbol,
        "name": FRIENDLY.get(symbol) or meta.get("shortName") or meta.get("longName") or symbol,
        "price": _r(price),
        "currency": meta.get("currency"),
        "change": _r(change),
        "change_pct": _r(pct, 2),
        "range": range_,
        "day_high": _r(meta.get("regularMarketDayHigh")),
        "day_low": _r(meta.get("regularMarketDayLow")),
        "52w_high": _r(meta.get("fiftyTwoWeekHigh")),
        "52w_low": _r(meta.get("fiftyTwoWeekLow")),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "as_of": datetime.fromtimestamp(meta["regularMarketTime"], tz=timezone.utc).isoformat(timespec="minutes")
        if meta.get("regularMarketTime")
        else None,
        "spark": [_r(c) for c in closes[::step]],
    }
    _CACHE[key] = (time.time(), out)
    return out


def _r(v, nd: int = 4):
    if v is None:
        return None
    v = float(v)
    return round(v, 2 if abs(v) >= 1 and nd > 2 else nd)


@tool(
    "get_market_data",
    "Live prices and performance for stocks, ETFs, indices, crypto, FX and futures using Yahoo Finance "
    "tickers (AAPL, NVDA, SPY, ^GSPC, ^IXIC, ^DJI, ^VIX, BTC-USD, ETH-USD, EURUSD=X, GC=F gold, CL=F oil, "
    "^TNX 10-year yield). The range sets the period for the change figure.",
    {
        "symbols": {"type": "array", "items": {"type": "string"}, "description": "Up to 12 tickers."},
        "range": {"type": "string", "enum": list(RANGES), "description": "Period for change %. Default '1d'."},
    },
    ["symbols"],
    label="Pulling market data",
)
async def get_market_data(ctx, symbols: list[str], range: str = "1d"):
    if isinstance(symbols, str):
        symbols = [s for s in symbols.replace(" ", ",").split(",") if s]
    symbols = symbols[:12]
    results = await asyncio.gather(*(fetch_quote(ctx.http, s, range) for s in symbols), return_exceptions=True)
    quotes, errors = [], []
    for sym, res in zip(symbols, results):
        if isinstance(res, Exception):
            errors.append({"symbol": sym, "error": str(res)})
        else:
            res = dict(res)
            res.pop("spark", None)
            quotes.append(res)
    if not quotes:
        raise ToolError("; ".join(e["error"] for e in errors) or "No market data.")
    return {"quotes": quotes, "errors": errors}


@tool(
    "lookup_ticker",
    "Find the Yahoo Finance ticker symbol for a company, fund, index or crypto by name.",
    {"query": {"type": "string", "description": "e.g. 'Nvidia', 'Vanguard total market', 'Dogecoin'"}},
    ["query"],
    label="Looking up ticker",
)
async def lookup_ticker(ctx, query: str):
    r = await ctx.http.get(
        "https://query2.finance.yahoo.com/v1/finance/search",
        params={"q": query, "quotesCount": 6, "newsCount": 0},
        headers={"User-Agent": BROWSER_UA},
        timeout=12,
    )
    r.raise_for_status()
    quotes = r.json().get("quotes") or []
    if not quotes:
        raise ToolError(f"No ticker found for '{query}'.")
    return {
        "matches": [
            {"symbol": q.get("symbol"), "name": q.get("shortname") or q.get("longname"), "type": q.get("quoteType"), "exchange": q.get("exchDisp")}
            for q in quotes
        ]
    }


async def watchlist_snapshot(ctx) -> list[dict]:
    results = await asyncio.gather(*(fetch_quote(ctx.http, s) for s in ctx.settings.watchlist), return_exceptions=True)
    return [r for r in results if not isinstance(r, Exception)]
