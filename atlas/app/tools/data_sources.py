"""Live finance & economy data beyond Yahoo quotes.

Sources (all read-only, public data):
- CoinGecko public API v3 (official). Keyless use is limited to a few calls a minute; an optional free "demo"
  key (COINGECKO_API_KEY, sent as the x-cg-demo-api-key header) raises that to ~30/min. When CoinGecko is
  rate-limited, major coins fall back to Yahoo Finance quotes.
- SEC EDGAR (official): company_tickers.json, the submissions API and the XBRL companyconcept API. The SEC's
  fair-access policy allows at most 10 requests/second and requires a User-Agent naming a contact
  (SEC_CONTACT_EMAIL); requests without one may get HTTP 403.
- Nasdaq's calendar API (api.nasdaq.com) -- UNOFFICIAL and undocumented (it's what nasdaq.com's own pages
  call). Best-effort: it needs browser-like headers and may change or block without notice.
- CNN's Fear & Greed graph data (production.dataviz.cnn.io) -- UNOFFICIAL (it feeds cnn.com's own widget).
  Best-effort, browser headers; the VIX from Yahoo is fetched alongside as a companion/fallback.
- FRED (St. Louis Fed): the official API when FRED_API_KEY is set, otherwise the public fredgraph.csv download
  behind FRED's "Download CSV" button (keyless, but not a formal API: best-effort).

Every request goes through a per-host throttle and an in-memory cache. Every failure ends in a fallback
source or a ToolError naming the source. API keys never appear in results or error messages.
"""
from __future__ import annotations

import asyncio
import csv
import functools
import io
import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..config import load_env_file
from ..util import BROWSER_UA
from . import ToolError, markets, tool

ATLAS_UA = "ATLAS-assistant/1.0 (personal assistant)"


def _setting(key: str, default: str = "") -> str:
    return str(os.environ.get(key) or load_env_file().get(key, default) or default).strip()


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return " ".join(str(value).split())


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        n = default
    return max(lo, min(hi, n))


def _list(value) -> list[str]:
    """A list argument that models sometimes send as one comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,;]", value)
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [t for t in (_text(v) for v in value) if t]


# --------------------------------------------------------------------------- secrets
def _secrets() -> list[str]:
    return [s for s in (_setting("FRED_API_KEY"), _setting("COINGECKO_API_KEY")) if len(s) >= 4]


def _scrub(text: str) -> str:
    """Belt and braces: no configured API key may ever leave this module in a message."""
    text = str(text)
    for secret in _secrets():
        text = text.replace(secret, "***")
    return re.sub(r"(api_key=)[^&\s'\"]+", r"\1***", text)


# --------------------------------------------------------------------------- throttling & caching
async def _sleep(delay: float) -> None:  # module-level so tests can replace it
    await asyncio.sleep(delay)


class Throttle:
    """Per-host pacing (GCRA, i.e. a token bucket): `rate` requests/second on average with bursts of up to
    `burst` back-to-back requests. Shared by concurrent callers; a caller whose slot is more than `max_wait`
    seconds away gets a friendly ToolError instead of a long, silent stall."""

    def __init__(self, rate: float, burst: int = 1, max_wait: float = 12.0, name: str = "this service", clock=time.monotonic):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.burst = max(1, int(burst))
        self.max_wait = max_wait
        self.name = name
        self.clock = clock
        self._tat = 0.0  # "theoretical arrival time" of the next request
        self._lock = threading.Lock()

    def reserve(self) -> float:
        """Claim the next slot; return how long the caller must wait for it."""
        with self._lock:
            now = self.clock()
            tat = max(self._tat, now)
            delay = max(0.0, tat - now - (self.burst - 1) * self.interval)
            if delay > self.max_wait:
                raise ToolError(f"Too many requests to {self.name} at once. Try again in a few seconds.")
            self._tat = tat + self.interval
            return delay

    async def wait(self) -> None:
        delay = self.reserve()
        if delay > 0:
            await _sleep(delay)


_SEC_THROTTLE = Throttle(9, 1, max_wait=15, name="SEC EDGAR")  # SEC fair access: <=10 req/s across sec.gov
_LIMITS: dict[str, Throttle] = {
    "api.coingecko.com": Throttle(0.5, 5, name="CoinGecko"),  # keyless/demo tier: ~30 calls/min at best
    "www.sec.gov": _SEC_THROTTLE,
    "data.sec.gov": _SEC_THROTTLE,
    "api.nasdaq.com": Throttle(2, 3, max_wait=15, name="Nasdaq"),
    "production.dataviz.cnn.io": Throttle(1, 2, name="CNN"),
    "api.stlouisfed.org": Throttle(1.5, 8, name="FRED"),  # FRED API: 120 req/min per key
    "fred.stlouisfed.org": Throttle(2, 8, name="FRED"),
}

CACHE_MAX = 512
_CACHE: dict[tuple, tuple[float, Any]] = {}


def _cache_get(key: tuple, ttl: float):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key: tuple, value) -> None:
    _CACHE.pop(key, None)  # re-insert at the end, so eviction (oldest first) never drops a fresh refresh
    _CACHE[key] = (time.time(), value)
    while len(_CACHE) > CACHE_MAX:
        try:
            _CACHE.pop(next(iter(_CACHE)), None)
        except (StopIteration, RuntimeError):  # emptied/changed meanwhile
            break


# Tool results are clipped by the agent at 14,000 characters: list-heavy results are trimmed well below that.
RESULT_BUDGET = 11_000


def _fit_rows(rows: list[dict], budget: int = RESULT_BUDGET) -> tuple[list[dict], int]:
    """Keep the leading rows whose JSON fits in `budget` characters. Returns (kept, number dropped)."""
    kept, used = [], 0
    for row in rows:
        used += len(json.dumps(row, ensure_ascii=False, default=str)) + 2
        if used > budget and kept:
            break
        kept.append(row)
    return kept, len(rows) - len(kept)


class SourceError(ToolError):
    """A friendly ToolError that also remembers the HTTP status (for fallback decisions)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


async def _fetch(ctx, url: str, source: str, *, params=None, headers=None, timeout: float = 15.0) -> httpx.Response:
    limiter = _LIMITS.get(httpx.URL(url).host)
    if limiter is not None:
        await limiter.wait()
    try:
        return await ctx.http.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        raise SourceError(f"{source} didn't answer in time. Try again in a moment.") from None
    except httpx.HTTPError as exc:  # never echo the exception text: it can carry the request URL (and a key)
        raise SourceError(f"Couldn't reach {source} ({exc.__class__.__name__}).") from None


def _check(r: httpx.Response, source: str, hints: dict[int, str] | None = None) -> None:
    if r.status_code < 400:
        return
    msg = (hints or {}).get(r.status_code)
    if msg is None:
        if r.status_code == 429:
            msg = f"{source} is limiting requests right now. Try again in a minute."
        elif r.status_code >= 500:
            msg = f"{source} is unavailable right now (HTTP {r.status_code}). Try again later."
        else:
            msg = f"{source} refused the request (HTTP {r.status_code})."
    raise SourceError(msg, r.status_code)


def _json(r: httpx.Response, source: str):
    try:
        return r.json()
    except ValueError:
        raise SourceError(f"{source} sent an unexpected reply (not JSON).", r.status_code) from None


def _guard(source: str):
    """Tool wrapper: scrub messages and turn surprises (odd payloads) into friendly errors naming the source."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(ctx, **kwargs):
            try:
                return await fn(ctx, **kwargs)
            except ToolError as exc:
                raise ToolError(_scrub(str(exc))) from None
            except httpx.HTTPError as exc:
                raise ToolError(f"Couldn't reach {source} ({exc.__class__.__name__}).") from None
            except (KeyError, TypeError, ValueError, AttributeError, IndexError, ZeroDivisionError, OverflowError, csv.Error) as exc:
                raise ToolError(f"{source} sent data in an unexpected format ({exc.__class__.__name__}). Try again later.") from None
            except Exception as exc:  # anything else: never echo the text (it could carry a URL with a key)
                raise ToolError(f"Something went wrong reading {source} ({exc.__class__.__name__}). Try again later.") from None

        return wrapper

    return deco


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, ToolError):
        return _scrub(str(exc))
    return f"{exc.__class__.__name__}"


# --------------------------------------------------------------------------- number formatting
def _num(value) -> float | None:
    """Numbers from JSON or display strings: 2.83, "$2.83", "($0.12)", "-$1,234.5", "N/A" -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    s = str(value).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    if s.startswith("-"):
        neg, s = True, s[1:].strip()
    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return -v if neg else v


def _pct(v, nd: int = 2):
    v = _num(v)
    return None if v is None else round(v, nd)


def _price(v):
    v = _num(v)
    if v is None:
        return None
    if abs(v) >= 1 or v == 0:
        return round(v, 2)
    return float(f"{v:.6g}")


_CURRENCY_SIGNS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "inr": "₹", "cad": "C$", "aud": "A$"}


def _prefix(currency: str) -> str:
    return _CURRENCY_SIGNS.get((currency or "usd").lower(), f"{(currency or '').upper()} ")


def _compact(v, prefix: str = "$") -> str | None:
    v = _num(v)
    if v is None:
        return None
    sign, a = ("-" if v < 0 else ""), abs(v)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}{prefix}{a / div:.2f}{suffix}"
    return f"{sign}{prefix}{a:,.0f}"


def _fmt_price(v, prefix: str = "$") -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1000:
        return f"{sign}{prefix}{a:,.0f}"
    if a >= 1:
        return f"{sign}{prefix}{a:,.2f}"
    if a == 0:
        return f"{prefix}0"
    # 4 significant digits without scientific notation (status lines may be read aloud): $0.00001235
    places = min(12, max(2, 3 - math.floor(math.log10(a))))
    digits = f"{a:.{places}f}".rstrip("0")
    if len(digits.split(".")[1]) < 2:
        digits = f"{a:.2f}"
    return f"{sign}{prefix}{digits}"


def _signed(v, nd: int = 1) -> str:
    return "n/a" if v is None else f"{v:+.{nd}f}"


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# =========================================================================== CRYPTO (CoinGecko)
CG_BASE = "https://api.coingecko.com/api/v3"
# Long-standing CoinGecko ids for the most-asked coins (saves a /search call each). `yahoo` marks coins whose
# Yahoo Finance ticker is unambiguously SYMBOL-USD, used as a fallback when CoinGecko is rate-limited.
COMMON_COINS = {
    "btc": ("bitcoin", "BTC", "Bitcoin", True),
    "bitcoin": ("bitcoin", "BTC", "Bitcoin", True),
    "eth": ("ethereum", "ETH", "Ethereum", True),
    "ether": ("ethereum", "ETH", "Ethereum", True),
    "ethereum": ("ethereum", "ETH", "Ethereum", True),
    "usdt": ("tether", "USDT", "Tether", True),
    "tether": ("tether", "USDT", "Tether", True),
    "bnb": ("binancecoin", "BNB", "BNB", True),
    "sol": ("solana", "SOL", "Solana", True),
    "solana": ("solana", "SOL", "Solana", True),
    "xrp": ("ripple", "XRP", "XRP", True),
    "ripple": ("ripple", "XRP", "XRP", True),
    "usdc": ("usd-coin", "USDC", "USDC", True),
    "usd coin": ("usd-coin", "USDC", "USDC", True),
    "binance coin": ("binancecoin", "BNB", "BNB", True),
    "doge": ("dogecoin", "DOGE", "Dogecoin", True),
    "dogecoin": ("dogecoin", "DOGE", "Dogecoin", True),
    "ada": ("cardano", "ADA", "Cardano", True),
    "cardano": ("cardano", "ADA", "Cardano", True),
    "trx": ("tron", "TRX", "TRON", True),
    "tron": ("tron", "TRX", "TRON", True),
    "avax": ("avalanche-2", "AVAX", "Avalanche", True),
    "avalanche": ("avalanche-2", "AVAX", "Avalanche", True),
    "link": ("chainlink", "LINK", "Chainlink", True),
    "chainlink": ("chainlink", "LINK", "Chainlink", True),
    "dot": ("polkadot", "DOT", "Polkadot", True),
    "polkadot": ("polkadot", "DOT", "Polkadot", True),
    "ltc": ("litecoin", "LTC", "Litecoin", True),
    "litecoin": ("litecoin", "LTC", "Litecoin", True),
    "shib": ("shiba-inu", "SHIB", "Shiba Inu", True),
    "shiba inu": ("shiba-inu", "SHIB", "Shiba Inu", True),
    "bch": ("bitcoin-cash", "BCH", "Bitcoin Cash", True),
    "bitcoin cash": ("bitcoin-cash", "BCH", "Bitcoin Cash", True),
    "xlm": ("stellar", "XLM", "Stellar", True),
    "stellar": ("stellar", "XLM", "Stellar", True),
    "xmr": ("monero", "XMR", "Monero", True),
    "monero": ("monero", "XMR", "Monero", True),
    "ton": ("the-open-network", "TON", "Toncoin", False),
    "toncoin": ("the-open-network", "TON", "Toncoin", False),
    "sui": ("sui", "SUI", "Sui", False),
    "uni": ("uniswap", "UNI", "Uniswap", False),
    "uniswap": ("uniswap", "UNI", "Uniswap", False),
}
YAHOO_OK = {sym for (_, sym, _, ok) in COMMON_COINS.values() if ok}
YAHOO_DEFAULT = ["BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "ADA", "TRX"]
_OVERVIEW_WORDS = {"", "top", "market", "markets", "overview", "all", "global", "crypto", "cryptocurrency", "cryptocurrencies", "coins"}
_TRENDING_WORDS = {"trending", "trend", "hot", "popular"}


class _BadCurrency(SourceError):
    """CoinGecko doesn't quote in the requested currency: a user error, so never 'fall back' to USD prices."""


def _cg_headers() -> dict:
    headers = {"Accept": "application/json", "User-Agent": ATLAS_UA}
    key = _setting("COINGECKO_API_KEY")
    if key:
        headers["x-cg-demo-api-key"] = key
    return headers


async def _cg_get(ctx, path: str, params: dict | None = None):
    has_key = bool(_setting("COINGECKO_API_KEY"))
    r = await _fetch(ctx, CG_BASE + path, "CoinGecko", params=params, headers=_cg_headers(), timeout=12)
    if r.status_code == 429:
        tail = "." if has_key else ", or add a free CoinGecko demo API key as COINGECKO_API_KEY in Settings."
        raise SourceError(
            "CoinGecko is rate-limiting requests right now (its free API allows only a few calls a minute). "
            "Try again in a minute" + tail,
            429,
        )
    if r.status_code in (400, 422):
        # Real replies: {"error": "invalid vs_currency"} for an unsupported currency; key problems come as
        # {"status": {"error_code": 1001x, "error_message": "... API key ..."}}.
        body = (r.text or "")[:2000].lower()
        if "vs_currency" in body:
            currency = str((params or {}).get("vs_currency") or "").upper()
            raise _BadCurrency(
                f"CoinGecko doesn't quote prices in {currency or 'that currency'}. Try usd, eur, gbp, jpy or btc.", r.status_code
            )
    if r.status_code in (400, 401, 403) and has_key:
        raise SourceError(
            "CoinGecko rejected the API key in COINGECKO_API_KEY (it must be a free 'demo' key). Check it in "
            "Settings, or remove it to use the keyless API.",
            r.status_code,
        )
    _check(r, "CoinGecko")
    return _json(r, "CoinGecko")


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _coin(c: dict, currency: str) -> dict:
    pre = _prefix(currency)
    return {
        "rank": c.get("market_cap_rank"),
        "name": c.get("name"),
        "symbol": str(c.get("symbol") or "").upper(),
        "price": _price(c.get("current_price")),
        "change_1h_pct": _pct(c.get("price_change_percentage_1h_in_currency")),
        "change_24h_pct": _pct(_first(c, "price_change_percentage_24h_in_currency", "price_change_percentage_24h")),
        "change_7d_pct": _pct(c.get("price_change_percentage_7d_in_currency")),
        "market_cap": _compact(c.get("market_cap"), pre),
        "volume_24h": _compact(c.get("total_volume"), pre),
        "from_ath_pct": _pct(c.get("ath_change_percentage"), 1),
        "id": c.get("id"),
    }


async def _cg_markets(ctx, currency: str = "usd", ids: list[str] | None = None, top: int = 10) -> list[dict]:
    key = ("cg_markets", currency, tuple(ids) if ids else None, top)
    hit = _cache_get(key, 60)
    if hit is not None:
        return hit
    params = {
        "vs_currency": currency,
        "order": "market_cap_desc",
        "per_page": len(ids) if ids else top,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d",
    }
    if ids:
        params["ids"] = ",".join(ids)
    data = await _cg_get(ctx, "/coins/markets", params)
    if not isinstance(data, list):
        raise SourceError("CoinGecko sent an unexpected reply for coin prices.")
    coins = [_coin(c, currency) for c in data if isinstance(c, dict) and c.get("id")]
    _cache_put(key, coins)
    return coins


async def _cg_global(ctx, currency: str = "usd") -> dict:
    key = ("cg_global", currency)
    hit = _cache_get(key, 120)
    if hit is not None:
        return hit
    data = await _cg_get(ctx, "/global")
    g = data.get("data") if isinstance(data, dict) else None
    if not isinstance(g, dict):
        raise SourceError("CoinGecko sent an unexpected reply for global market stats.")
    pre = _prefix(currency)
    share = g.get("market_cap_percentage") or {}
    out = {
        "total_market_cap": _compact((g.get("total_market_cap") or {}).get(currency), pre),
        "change_24h_pct": _pct(g.get("market_cap_change_percentage_24h_usd")),
        "volume_24h": _compact((g.get("total_volume") or {}).get(currency), pre),
        "btc_dominance_pct": _pct(share.get("btc"), 1),
        "eth_dominance_pct": _pct(share.get("eth"), 1),
        "active_coins": g.get("active_cryptocurrencies"),
    }
    _cache_put(key, out)
    return out


async def _cg_trending(ctx) -> list[dict]:
    hit = _cache_get(("cg_trending",), 600)
    if hit is not None:
        return hit
    data = await _cg_get(ctx, "/search/trending")
    out = []
    for entry in (data.get("coins") if isinstance(data, dict) else None) or []:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict):
            continue
        extra = item.get("data") if isinstance(item.get("data"), dict) else {}
        change = extra.get("price_change_percentage_24h")
        if isinstance(change, dict):
            change = change.get("usd")
        out.append(
            {
                "name": item.get("name"),
                "symbol": str(item.get("symbol") or "").upper(),
                "rank": item.get("market_cap_rank"),
                "price_usd": _price(extra.get("price")),
                "change_24h_pct": _pct(change),
            }
        )
        if len(out) >= 7:
            break
    _cache_put(("cg_trending",), out)
    return out


def _coin_rank(c: dict) -> int:
    r = c.get("market_cap_rank")
    return r if isinstance(r, int) and not isinstance(r, bool) and r > 0 else 10**9


async def _resolve_coin(ctx, term: str) -> dict | None:
    """'btc' / 'Bitcoin' / 'BTC-USD' / 'pepe' -> {id, symbol, name, yahoo}, or None if CoinGecko knows no such coin."""
    t = re.sub(r"\s*[-/ ](usd|usdt|usdc)$", "", term.strip().lower()).strip()
    if t in COMMON_COINS:
        cid, sym, name, ok = COMMON_COINS[t]
        return {"id": cid, "symbol": sym, "name": name, "yahoo": ok}
    key = ("cg_search", t)
    hit = _cache_get(key, 24 * 3600)
    if hit is None:
        data = await _cg_get(ctx, "/search", {"query": t})
        hit = [c for c in ((data.get("coins") if isinstance(data, dict) else None) or []) if isinstance(c, dict) and c.get("id")]
        _cache_put(key, hit)
    if not hit:
        return None
    by_symbol = [c for c in hit if str(c.get("symbol") or "").lower() == t]
    by_name = [c for c in hit if str(c.get("name") or "").lower() == t or c.get("id") == t]
    best = min(by_symbol, key=_coin_rank) if by_symbol else (min(by_name, key=_coin_rank) if by_name else hit[0])
    sym = str(best.get("symbol") or "").upper()
    return {"id": best["id"], "symbol": sym, "name": best.get("name") or sym, "yahoo": sym in YAHOO_OK}


# Yahoo's 1-day chart compares with the previous daily close, which for crypto is 00:00 UTC, not 24 hours ago.
YAHOO_FALLBACK_SOURCE = "Yahoo Finance (fallback: CoinGecko unavailable; USD; 24h change = change since 00:00 UTC"


async def _yahoo_crypto(ctx, symbols: list[str]) -> list[dict]:
    """Fallback quotes for major coins from Yahoo Finance (SYMBOL-USD)."""
    symbols = [s for s in symbols if s in YAHOO_OK]
    results = await asyncio.gather(*(markets.fetch_quote(ctx.http, f"{s}-USD") for s in symbols), return_exceptions=True)
    names = {sym: name for (_, sym, name, _) in COMMON_COINS.values()}
    out = []
    for sym, q in zip(symbols, results):
        if isinstance(q, BaseException) or not isinstance(q, dict) or q.get("price") is None:
            continue
        out.append({"name": names.get(sym, sym), "symbol": sym, "price": q.get("price"), "change_24h_pct": q.get("change_pct")})
    return out


# Words models add around coin names ("bitcoin price", "price of eth today", "trending coins").
_FILLER = re.compile(
    r"\b(prices?|price\s+of|value|quotes?|rates?|current|currently|live|today|now|right\s+now|latest|the|of|for|"
    r"how\s+much\s+is|what\s+is|what's|coins?|tokens?|crypto(?:currency|currencies|s)?)\b",
    re.I,
)
_QUOTE_PAIR = re.compile(r"(?i)\b([a-z0-9]+)\s*[-/]\s*(?:usd|usdt|usdc)\b")


def _split_terms(query) -> list[str]:
    items = [_text(q) for q in query] if isinstance(query, (list, tuple)) else [_text(query)]
    parts: list[str] = []
    for item in items:
        item = _QUOTE_PAIR.sub(r"\1", item)  # "BTC/USD", "eth-usdt" -> the coin, not two coins
        for p in re.split(r"\s*(?:,|;|&|/|\+|\band\b|\bvs\.?\b|\bversus\b)\s*", item, flags=re.I):
            if " ".join(p.lower().split()) not in COMMON_COINS:  # keep names like "USD Coin" whole
                p = " ".join(_FILLER.sub(" ", p).split()).strip(" ?!.")
            if p:
                parts.append(p)
            elif re.search(r"(?i)\bcrypto|\bcoins?\b", item):
                parts.append("crypto")  # "crypto prices", "coins" -> the overview
    return parts[:8]


def _currency(value) -> str:
    c = _text(value).lower() or "usd"
    return c if re.fullmatch(r"[a-z]{3,5}", c) else "usd"


async def _crypto_overview(ctx, top: int, currency: str, trending_only: bool = False) -> dict:
    jobs = [_cg_global(ctx, currency), _cg_trending(ctx)]
    if not trending_only:
        jobs.append(_cg_markets(ctx, currency, None, top))
    res = await asyncio.gather(*jobs, return_exceptions=True)
    glob, trending = res[0], res[1]
    coins = res[2] if not trending_only else []
    unavailable, source = [], "CoinGecko"
    if isinstance(glob, BaseException):
        unavailable.append(f"global market stats ({_friendly(glob)})")
        glob = None
    if isinstance(trending, BaseException):
        unavailable.append(f"trending coins ({_friendly(trending)})")
        trending = []
    if isinstance(coins, BaseException):
        if isinstance(coins, _BadCurrency):
            raise coins
        fallback = await _yahoo_crypto(ctx, YAHOO_DEFAULT[:top])
        if not fallback:
            raise coins
        unavailable.append(f"CoinGecko prices ({_friendly(coins)})")
        coins, source = fallback, YAHOO_FALLBACK_SOURCE + "; major coins only)"
        currency = "usd"  # Yahoo's fallback quotes are in USD whatever was asked for
    if trending_only and not trending:
        raise SourceError("CoinGecko's trending list is unavailable right now. " + "; ".join(unavailable))
    pre = _prefix(currency)
    parts = []
    if glob and glob.get("total_market_cap"):
        parts.append(f"Crypto market {glob['total_market_cap']} ({_signed(glob.get('change_24h_pct'))}% 24h)")
    if trending_only:
        parts.append("Trending: " + ", ".join(t["symbol"] for t in trending[:5]))
    for c in coins[:2]:
        parts.append(f"{c['symbol']} {_fmt_price(c.get('price'), pre)} ({_signed(c.get('change_24h_pct'))}%)")
    out: dict[str, Any] = {"status": " · ".join(parts) or "Crypto market data", "source": source, "currency": currency.upper()}
    if glob:
        out["global"] = glob
    if coins:
        out["coins"] = coins
    if trending:
        out["trending"] = trending
    if unavailable:
        out["unavailable"] = unavailable
    out["retrieved_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    out["note"] = "Crypto trades 24/7; CoinGecko prices refresh about every minute."
    return out


async def _crypto_lookup(ctx, terms: list[str], currency: str) -> dict:
    resolved = await asyncio.gather(*(_resolve_coin(ctx, t) for t in terms), return_exceptions=True)
    found: list[dict] = []
    not_found: list[str] = []
    search_error: BaseException | None = None
    for term, res in zip(terms, resolved):
        if isinstance(res, BaseException):
            search_error = search_error or res
            guess = re.sub(r"[-/ ](usd|usdt)$", "", term.strip(), flags=re.I).upper()
            if guess in YAHOO_OK:
                found.append({"id": None, "symbol": guess, "name": guess, "yahoo": True})
            else:
                not_found.append(term)
        elif res is None:
            not_found.append(term)
        elif all(f["id"] != res["id"] for f in found):
            found.append(res)
    if not found:
        if search_error is not None:
            raise search_error
        raise SourceError(f"CoinGecko doesn't list a coin called {', '.join(repr(t) for t in terms)}.")

    ids = [f["id"] for f in found if f["id"]]
    coins: list[dict] = []
    source, problem = "CoinGecko", None
    if ids and len(ids) == len(found):
        try:
            coins = list(await _cg_markets(ctx, currency, ids))  # a copy: it's sorted below, the cache isn't
        except _BadCurrency:
            raise
        except ToolError as exc:
            problem = exc
    else:
        problem = search_error
    if problem is not None or not coins:
        fallback = await _yahoo_crypto(ctx, [f["symbol"] for f in found if f["yahoo"]])
        if not fallback:
            if problem is not None:
                raise problem
            raise SourceError("CoinGecko returned no prices for " + ", ".join(f["name"] for f in found) + ".")
        coins, source = fallback, YAHOO_FALLBACK_SOURCE + ")"
        priced = {c["symbol"] for c in fallback}
        not_found.extend(f"{f['name']} (no fallback price)" for f in found if f["symbol"] not in priced)
    else:
        order = {cid: i for i, cid in enumerate(ids)}
        coins.sort(key=lambda c: order.get(c.get("id"), 99))
        missing = [f["name"] for f in found if f["id"] not in {c.get("id") for c in coins}]
        not_found.extend(missing)
    pre = _prefix(currency) if source == "CoinGecko" else "$"
    parts = [f"{c.get('name')} {_fmt_price(c.get('price'), pre)} ({_signed(c.get('change_24h_pct'))}% 24h)" for c in coins[:3]]
    out: dict[str, Any] = {
        "status": " · ".join(parts),
        "source": source,
        "currency": currency.upper() if source == "CoinGecko" else "USD",
        "coins": coins,
    }
    if not_found:
        out["not_found"] = not_found
    if problem is not None:
        out["unavailable"] = [f"CoinGecko ({_friendly(problem)})"]
    out["retrieved_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    return out


@tool(
    "get_crypto",
    "Live cryptocurrency prices and market overview from CoinGecko (near real-time; crypto trades 24/7). "
    "With no query: the top coins by market cap (price, 1h/24h/7d % change, market cap, 24h volume, rank), total "
    "crypto market cap and its 24h change, BTC/ETH dominance, and trending coins. With a query: specific coins by "
    "name or symbol, e.g. 'bitcoin', 'ETH, SOL, dogecoin', 'pepe'; query 'trending' for what's trending. Use this "
    "for ANY crypto price or crypto-market question in preference to get_market_data.",
    {
        "query": {"type": "string", "description": "Coin name(s) or symbol(s), comma-separated, or 'trending'. Omit for the market overview."},
        "top": {"type": "integer", "description": "How many top coins in the overview, 1-25. Default 10."},
        "currency": {"type": "string", "description": "Quote currency code, e.g. usd, eur, gbp. Default usd."},
    },
    label="Checking crypto markets",
)
@_guard("CoinGecko")
async def get_crypto(ctx, query=None, top: int = 10, currency: str = "usd"):
    currency = _currency(currency)
    top = _int(top, 10, 1, 25)
    m = re.fullmatch(r"(?i)\s*top\s*(\d{1,3})\b.*", _text(query) if not isinstance(query, (list, tuple)) else "")
    if m:  # "top 5", "top 20 coins by market cap"
        return await _crypto_overview(ctx, _int(m.group(1), top, 1, 25), currency)
    terms = _split_terms(query)
    lowered = {t.lower() for t in terms}
    if terms and lowered <= _TRENDING_WORDS:
        return await _crypto_overview(ctx, top, currency, trending_only=True)
    if not terms or lowered <= _OVERVIEW_WORDS:
        return await _crypto_overview(ctx, top, currency)
    return await _crypto_lookup(ctx, terms, currency)


# =========================================================================== SEC EDGAR
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data"

FORM_INFO = {
    "10-K": "Annual report",
    "10-Q": "Quarterly report",
    "8-K": "Current report (material event)",
    "4": "Insider transaction (Form 4)",
    "3": "Initial insider ownership (Form 3)",
    "5": "Annual insider ownership (Form 5)",
    "144": "Notice of proposed insider stock sale",
    "DEF 14A": "Proxy statement (annual meeting)",
    "DEFA14A": "Additional proxy materials",
    "S-1": "Registration statement (IPO)",
    "S-3": "Shelf registration statement",
    "S-4": "Registration statement (merger/exchange)",
    "S-8": "Employee stock plan registration",
    "SC 13G": "Passive 5%+ ownership report",
    "SC 13D": "Active 5%+ ownership report",
    "SCHEDULE 13G": "Passive 5%+ ownership report",
    "SCHEDULE 13D": "Active 5%+ ownership report",
    "13F-HR": "Institutional holdings report",
    "20-F": "Annual report (foreign issuer)",
    "6-K": "Current report (foreign issuer)",
    "40-F": "Annual report (Canadian issuer)",
    "11-K": "Employee benefit plan annual report",
    "SD": "Specialized disclosure (conflict minerals)",
    "ARS": "Annual report to shareholders",
    "PX14A6G": "Shareholder proxy solicitation",
    "CORRESP": "Company letter to the SEC",
    "UPLOAD": "SEC staff letter",
}
ITEMS_8K = {
    "1.01": "material agreement",
    "1.02": "agreement terminated",
    "1.03": "bankruptcy",
    "1.05": "cybersecurity incident",
    "2.01": "acquisition or sale completed",
    "2.02": "results of operations (earnings)",
    "2.03": "new debt obligation",
    "2.05": "restructuring / exit costs",
    "2.06": "impairment",
    "3.01": "listing standards / delisting notice",
    "3.02": "unregistered sale of equity",
    "3.03": "change to shareholder rights",
    "4.01": "auditor change",
    "4.02": "prior financials no longer reliable",
    "5.01": "change in control",
    "5.02": "executive or director change",
    "5.03": "charter or bylaw amendment",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}
# Normalised filter key (upper case, no spaces/dashes) -> the normalised form types it matches. Since Dec 2024
# EDGAR files beneficial-ownership reports as "SCHEDULE 13G/13D" (earlier "SC 13G/13D"): either spelling finds both.
_13G, _13D = ("SC13G", "SCHEDULE13G"), ("SC13D", "SCHEDULE13D")
FORM_ALIASES = {
    "13F": ("13FHR",), "PROXY": ("DEF14A",), "INSIDER": ("4",), "FORM4": ("4",), "INSIDERTRADES": ("4",),
    "ANNUAL": ("10K",), "ANNUALREPORT": ("10K",), "QUARTERLY": ("10Q",), "QUARTERLYREPORT": ("10Q",),
    "13G": _13G, "SC13G": _13G, "SCHEDULE13G": _13G, "13D": _13D, "SC13D": _13D, "SCHEDULE13D": _13D,
}


def _sec_user_agent() -> tuple[str, bool]:
    email = re.sub(r"[^\w.@+\-]", "", _setting("SEC_CONTACT_EMAIL"))[:200]
    if "@" in email:
        return f"ATLAS personal assistant {email}", True
    return "ATLAS personal assistant (contact not configured)", False


async def _sec_get(ctx, url: str):
    """JSON from an SEC host, or None for 404."""
    ua, configured = _sec_user_agent()
    r = await _fetch(ctx, url, "SEC EDGAR", headers={"User-Agent": ua, "Accept": "application/json"}, timeout=20)
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        if configured:
            msg = (
                "SEC EDGAR refused the request (HTTP 403). The SEC briefly blocks clients that exceed 10 requests "
                "a second; try again in a few minutes."
            )
        else:
            msg = (
                "SEC EDGAR refused the request (HTTP 403): the SEC requires a contact email with every request. "
                "Set SEC_CONTACT_EMAIL to your email address in Settings (or the .env file) and try again."
            )
        raise SourceError(msg, 403)
    _check(r, "SEC EDGAR", {429: "SEC EDGAR is rate-limiting requests right now. Try again in a few minutes."})
    return _json(r, "SEC EDGAR")


def _cik10(cik) -> str:
    return f"{int(cik):010d}"


async def _sec_tickers(ctx) -> dict[str, tuple[int, str, str]]:
    hit = _cache_get(("sec_tickers",), 24 * 3600)
    if hit is not None:
        return hit
    data = await _sec_get(ctx, SEC_TICKERS_URL)
    if not isinstance(data, dict) or not data:
        raise SourceError("SEC EDGAR's ticker list is unavailable right now. Try again later.")
    table: dict[str, tuple[int, str, str]] = {}
    for row in data.values():
        if not isinstance(row, dict):
            continue
        try:
            cik, ticker = int(row["cik_str"]), str(row["ticker"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        table.setdefault(ticker, (cik, ticker, str(row.get("title") or ticker)))
    _cache_put(("sec_tickers",), table)
    return table


_NAME_NOISE = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|plc|llc|holdings|group|the)\b\.?", re.I)


async def _resolve_company(ctx, query) -> dict:
    """Ticker ('AAPL', 'BRK.B'), company name ('Apple') or CIK -> {cik, ticker, name}."""
    q = _text(query).strip()
    if not q:
        raise ToolError("Say which company: a ticker like AAPL, or the company's name.")
    m = re.fullmatch(r"(?i)(?:cik\s*)?(\d{1,10})", q)
    if m:
        return {"cik": int(m.group(1)), "ticker": None, "name": None}
    table = await _sec_tickers(ctx)
    ticker = re.sub(r"[./ ]", "-", q.upper().lstrip("$"))
    if ticker in table:
        cik, tk, name = table[ticker]
        return {"cik": cik, "ticker": tk, "name": name}
    needle = " ".join(_NAME_NOISE.sub(" ", q.lower()).split())
    if len(needle) >= 3:  # "ab" would match the first of hundreds of names
        rows = list(table.values())
        starts = [r for r in rows if r[2].lower().startswith(needle)]
        contains = [r for r in rows if needle in r[2].lower()]
        pick = (starts or contains or [None])[0]
        if pick:
            return {"cik": pick[0], "ticker": pick[1], "name": pick[2]}
    raise ToolError(
        f"'{q}' isn't in the SEC's list of company tickers. SEC EDGAR covers companies that file with the SEC; "
        "try the exact US ticker (e.g. AAPL), or lookup_ticker to find it."
    )


def _form_keys(form: str) -> tuple[str, ...]:
    f = re.sub(r"^FORM\s*", "", _text(form).upper())
    f = re.sub(r"[\s\-]", "", f)
    return FORM_ALIASES.get(f, (f,))


def _filing_key(form: str) -> str:
    return form.upper().replace(" ", "").replace("-", "")


def _describe_filing(form: str, items: str, doc_desc: str) -> str:
    base_form, amended = (form[:-2], True) if form.endswith("/A") else (form, False)
    desc = FORM_INFO.get(base_form, "")
    if base_form == "8-K" and items:
        codes = [c.strip() for c in items.split(",") if c.strip()]
        named = [ITEMS_8K[c] for c in codes if c in ITEMS_8K and c != "9.01"] or [ITEMS_8K[c] for c in codes if c in ITEMS_8K]
        if named:
            desc = "Current report: " + ", ".join(named)
    if not desc:
        d = _text(doc_desc)[:100]
        desc = d if d and d.upper() not in {form.upper(), f"FORM {form.upper()}"} else form
    return desc + (" (amendment)" if amended else "")


def _filing_url(cik: int, accession: str, document: str) -> str:
    folder = f"{SEC_ARCHIVE}/{int(cik)}/{accession.replace('-', '')}"
    return f"{folder}/{quote(document, safe='/')}" if document else f"{folder}/{accession}-index.htm"


async def _submissions(ctx, cik: int) -> dict:
    key = ("sec_subs", cik)
    hit = _cache_get(key, 15 * 60)
    if hit is not None:
        return hit
    data = await _sec_get(ctx, SEC_SUBMISSIONS_URL.format(cik=_cik10(cik)))
    if not isinstance(data, dict):
        raise SourceError(f"SEC EDGAR has no filings for CIK {_cik10(cik)}.", 404)
    _cache_put(key, data)
    return data


async def _older_filings(ctx, data: dict) -> dict:
    """The newest supplementary submissions page (filings older than the ~1 year / 1,000 in 'recent').

    Heavy filers (banks issuing thousands of 424B2 notes) can have only a year of filings in 'recent', so a
    filter such as forms=['10-K'] would otherwise find just one. Each page has the same columnar arrays at top
    level. Returns {} when there is no older page."""
    files = [f for f in ((data.get("filings") or {}).get("files") or []) if isinstance(f, dict)]
    files.sort(key=lambda f: str(f.get("filingTo") or ""), reverse=True)
    name = str(files[0].get("name") or "") if files else ""
    if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", name):
        return {}
    key = ("sec_subs_page", name)
    hit = _cache_get(key, 6 * 3600)
    if hit is not None:
        return hit
    page = await _sec_get(ctx, f"https://data.sec.gov/submissions/{name}")
    page = page if isinstance(page, dict) else {}
    _cache_put(key, page)
    return page


def _collect_filings(columns: dict, cik: int, wanted: set[str], limit: int, out: list[dict], seen: set[str]) -> None:
    """Append filings from one columnar submissions block (newest first) to `out`, up to `limit`."""
    accessions = columns.get("accessionNumber") if isinstance(columns.get("accessionNumber"), list) else []

    def col(name_: str, i: int) -> str:
        values = columns.get(name_) if isinstance(columns.get(name_), list) else []
        return str(values[i]) if i < len(values) and values[i] is not None else ""

    for i in range(len(accessions)):
        if len(out) >= limit:
            return
        form = col("form", i)
        key = _filing_key(form)
        if wanted and key not in wanted and key.removesuffix("/A") not in wanted:
            continue
        accession = col("accessionNumber", i)
        if not accession or accession in seen:
            continue
        seen.add(accession)
        out.append(
            {
                "form": form,
                "filed": col("filingDate", i),
                "report_date": col("reportDate", i) or None,
                "description": _describe_filing(form, col("items", i), col("primaryDocDescription", i)),
                "url": _filing_url(cik, accession, col("primaryDocument", i)),
            }
        )


@tool(
    "get_sec_filings",
    "A US public company's recent official SEC EDGAR filings with filing dates and direct document links: "
    "10-K annual reports, 10-Q quarterly reports, 8-K material-event reports (earnings releases, executive changes, "
    "deals), Form 4 insider buys/sells, proxy statements (DEF 14A), S-1 registrations and more. Use when the user "
    "asks about a company's filings, insider trading, recent 8-Ks or wants a link to an annual/quarterly report. "
    "Filter with forms, e.g. ['10-K','10-Q'] or ['4'] for insider trades.",
    {
        "ticker": {"type": "string", "description": "US ticker (AAPL, BRK.B) or company name."},
        "forms": {"type": "array", "items": {"type": "string"}, "description": "Optional form types, e.g. ['10-K','10-Q','8-K','4']. Amendments (/A) are included."},
        "limit": {"type": "integer", "description": "Number of filings, 1-30. Default 10."},
    },
    ["ticker"],
    label="Checking SEC filings",
    timeout=90,  # ticker list + submissions (+ an older page), each up to 20s on a slow SEC day
)
@_guard("SEC EDGAR")
async def get_sec_filings(ctx, ticker: str, forms=None, limit: int = 10):
    limit = _int(limit, 10, 1, 30)
    company = await _resolve_company(ctx, ticker)
    data = await _submissions(ctx, company["cik"])
    cik = company["cik"]
    name = data.get("name") or company["name"] or f"CIK {_cik10(cik)}"
    tk = company["ticker"] or next(iter(data.get("tickers") or []), None)
    wanted = {k for f in _list(forms) for k in _form_keys(f)}
    recent = ((data.get("filings") or {}).get("recent")) or {}
    filings: list[dict] = []
    seen: set[str] = set()
    _collect_filings(recent if isinstance(recent, dict) else {}, cik, wanted, limit, filings, seen)
    older_note = None
    if wanted and len(filings) < limit:
        try:
            _collect_filings(await _older_filings(ctx, data), cik, wanted, limit, filings, seen)
        except ToolError as exc:
            if getattr(exc, "status", None) == 403:
                raise
            older_note = "Older filings couldn't be loaded right now; only the most recent ones were searched."
    filings, dropped = _fit_rows(filings)
    label = f"{name} ({tk})" if tk else name
    kinds = ", ".join(sorted({_text(f).upper() for f in _list(forms)}))
    if filings:
        status = f"{label}: {len(filings)} recent {kinds + ' ' if kinds else ''}filings, latest {filings[0]['form']} on {filings[0]['filed']}"
    else:
        status = f"{label}: no {kinds or 'recent'} filings in EDGAR's recent list"
    out: dict[str, Any] = {
        "status": status,
        "company": name,
        "ticker": tk,
        "cik": _cik10(cik),
        "filings": filings,
        "all_filings_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={_cik10(cik)}&owner=include&count=40",
        "source": "SEC EDGAR",
    }
    if dropped:
        out["trimmed"] = f"{dropped} more filings not shown to keep the answer short; ask for fewer or filter by form."
    if older_note:
        out["note"] = older_note
    return out


# ---------------------------------------------------------------- XBRL fundamentals
FIN_METRICS: list[tuple[str, list[str]]] = [
    ("revenue", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]),
    ("net_income", ["NetIncomeLoss", "ProfitLoss"]),
    ("eps_diluted", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
    ("operating_cash_flow", ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
]
_REPORT_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT", "10-KT/A", "10-QT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
# 13-week quarters are 91 days (98 in a 53-week year); retailers on 12/12/12/16-week calendars (Costco's Q4,
# Kroger's Q1) have 84-day and 112-119-day quarters, so the window must cover 16-17 weeks too.
QUARTER_DAYS = (70, 125)
ANNUAL_DAYS = (340, 390)
STALE_DAYS = 100  # a concept whose latest period trails the company's latest by more than this was retired


def _days(p: dict) -> int:
    return (p["end"] - p["start"]).days


def _form_rank(form: str) -> int:
    return 2 if form.startswith("10-") else 1


def parse_concept(data: dict) -> dict | None:
    """One XBRL companyconcept payload -> discrete quarters and fiscal years, deduplicated.

    Each 10-K/10-Q repeats prior periods as comparatives, and 10-Qs report year-to-date (6- and 9-month)
    figures alongside -- or, for cash flow, instead of -- the 3-month ones. So: keep only 10-K/10-Q-family
    facts, keep one fact per (start, end) period (the latest filed, i.e. as restated), take 3-month facts as
    quarters, derive the quarters that are only implied by cumulative YTD facts sharing a fiscal-year start
    (Q2 = 6M - 3M, Q4 = FY - 9M, ...) and never treat a YTD figure itself as a quarter. Fiscal labels (fy/fp)
    come from each filing's own current period, i.e. facts whose end is the latest end in that accession.
    """
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, dict) or not units:
        return None
    unit = next((u for u in ("USD", "USD/shares") if u in units), None) or next(iter(units))
    facts = []
    for f in units.get(unit) or []:
        if not isinstance(f, dict):
            continue
        form = str(f.get("form") or "").upper()
        start, end, val = _iso(f.get("start")), _iso(f.get("end")), f.get("val")
        if form not in _REPORT_FORMS or start is None or end is None or end < start:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
            continue
        facts.append(
            {
                "start": start,
                "end": end,
                "val": float(val),
                "form": form,
                "filed": str(f.get("filed") or ""),
                "fy": f.get("fy"),
                "fp": str(f.get("fp") or "").upper(),
                "accn": str(f.get("accn") or ""),
            }
        )
    if not facts:
        return None

    # Fiscal labels: a filing's fy/fp describe its own (latest-ending) period, not the comparatives it repeats.
    accn_end: dict[str, date] = {}
    for f in facts:
        accn_end[f["accn"]] = max(accn_end.get(f["accn"], f["end"]), f["end"])
    labels: dict[date, tuple[int, str]] = {}
    for f in sorted(facts, key=lambda f: f["filed"]):
        if f["end"] == accn_end[f["accn"]] and isinstance(f["fy"], int) and f["fp"] in {"Q1", "Q2", "Q3", "Q4", "FY"}:
            labels[f["end"]] = (f["fy"], f["fp"])

    # One fact per period: prefer 10-K/10-Q over foreign forms, then the latest filed (restated figures).
    best: dict[tuple[date, date], dict] = {}
    for f in facts:
        k = (f["start"], f["end"])
        cur = best.get(k)
        if cur is None or (_form_rank(f["form"]), f["filed"]) > (_form_rank(cur["form"]), cur["filed"]):
            best[k] = f
    periods = list(best.values())

    direct = [dict(p, derived=False) for p in periods if QUARTER_DAYS[0] <= _days(p) <= QUARTER_DAYS[1]]
    chains: dict[date, list[dict]] = defaultdict(list)
    for p in periods:
        if QUARTER_DAYS[0] <= _days(p) <= ANNUAL_DAYS[1]:
            chains[p["start"]].append(p)
    derived = []
    for chain in chains.values():
        chain.sort(key=lambda p: p["end"])
        for prev, cur in zip(chain, chain[1:]):
            if QUARTER_DAYS[0] <= (cur["end"] - prev["end"]).days <= QUARTER_DAYS[1]:
                derived.append(
                    {
                        "start": prev["end"] + timedelta(days=1),
                        "end": cur["end"],
                        "val": cur["val"] - prev["val"],
                        "filed": cur["filed"],
                        "derived": True,
                    }
                )
    quarters = _dedupe_periods(direct + derived, 45)
    annuals = _dedupe_periods([dict(p, derived=False) for p in periods if ANNUAL_DAYS[0] <= _days(p) <= ANNUAL_DAYS[1]], 180)
    latest = max(p["end"] for p in periods)
    return {"unit": unit, "concept": data.get("tag"), "entity": data.get("entityName"), "quarters": quarters, "annuals": annuals, "labels": labels, "latest_end": latest}


def _dedupe_periods(candidates: list[dict], min_gap_days: int) -> list[dict]:
    """Keep one entry per period end: reported beats derived, then latest filed. Returns oldest -> newest."""
    ranked = sorted(candidates, key=lambda p: p["filed"], reverse=True)  # ISO dates: latest filed first
    ranked.sort(key=lambda p: p.get("derived", False))  # stable: reported before derived
    kept: list[dict] = []
    for p in ranked:
        if all(abs((p["end"] - k["end"]).days) > min_gap_days for k in kept):
            kept.append(p)
    return sorted(kept, key=lambda p: p["end"])


def _match(periods: list[dict], end: date, tolerance: int = 10) -> dict | None:
    for p in periods:
        if abs((p["end"] - end).days) <= tolerance:
            return p
    return None


def yoy_pct(periods: list[dict], p: dict) -> float | None:
    """Growth vs the same period a year earlier (end 350-380 days before); abs() denominator for losses."""
    for q in periods:
        if 350 <= (p["end"] - q["end"]).days <= 380:
            if q["val"] == 0:
                return None
            return round((p["val"] - q["val"]) / abs(q["val"]) * 100, 1)
    return None


def _scale(v: float | None, unit: str):
    if v is None:
        return None
    return round(v, 2) if "/" in unit else round(v / 1e6, 1)


def _quarter_label(p: dict, labels: dict, annuals: list[dict]) -> str:
    lab = labels.get(p["end"])
    if lab:
        return f"FY{lab[0]} {'Q4' if lab[1] == 'FY' else lab[1]}"
    for a in annuals:
        if a["start"] - timedelta(days=3) <= p["end"] <= a["end"] + timedelta(days=3):
            alab = labels.get(a["end"])
            n = min(4, max(1, round((p["end"] - a["start"]).days / 91.3)))
            return f"FY{alab[0]} Q{n}" if alab else f"Q{n} of FY ending {a['end'].isoformat()}"
    return f"Quarter ended {p['end'].isoformat()}"


def _year_label(p: dict, labels: dict) -> str:
    lab = labels.get(p["end"])
    return f"FY{lab[0]}" if lab and lab[1] == "FY" else f"FY ended {p['end'].isoformat()}"


def build_financial_rows(series: dict[str, dict], kind: str, count: int) -> list[dict]:
    """Rows (newest first) aligning every metric on period end dates, with YoY growth per metric."""
    labels: dict[date, tuple[int, str]] = {}
    annual_ref: list[dict] = []
    ends: list[date] = []
    for s in series.values():
        labels.update(s["labels"])
        if len(s["annuals"]) > len(annual_ref):
            annual_ref = s["annuals"]
        ends.extend(p["end"] for p in s[kind])
    picked: list[date] = []
    for e in sorted(set(ends), reverse=True):
        if all(abs((e - k).days) > 20 for k in picked):
            picked.append(e)
        if len(picked) >= count:
            break
    rows = []
    for end in picked:
        ref = {"end": end}
        row: dict[str, Any] = {"period": _quarter_label(ref, labels, annual_ref) if kind == "quarters" else _year_label(ref, labels), "end": end.isoformat()}
        derived = []
        for metric, s in series.items():
            p = _match(s[kind], end)
            row[metric] = _scale(p["val"], s["unit"]) if p else None
            row[f"{metric}_yoy_pct"] = yoy_pct(s[kind], p) if p else None
            if p and p.get("derived"):
                derived.append(metric)
        if derived:
            row["derived"] = derived
        rows.append(row)
    return rows


async def _concept(ctx, cik: int, concept: str) -> dict | None:
    key = ("sec_concept", cik, concept)
    hit = _cache_get(key, 6 * 3600)
    if hit is not None:
        return hit or None
    data = await _sec_get(ctx, SEC_CONCEPT_URL.format(cik=_cik10(cik), concept=concept))
    parsed = parse_concept(data) if isinstance(data, dict) else None
    _cache_put(key, parsed or {})
    return parsed


async def _metric(ctx, cik: int, candidates: list[str], first: dict | None, reference: date | None) -> tuple[dict | None, BaseException | None]:
    """Pick the concept for a metric: the first candidate unless it's missing or retired (stale), then the freshest.
    Returns (series or None, the first failed request's error -- so 'missing' can be told apart from an outage)."""
    if first is not None and (reference is None or (reference - first["latest_end"]).days <= STALE_DAYS):
        return first, None
    rest = await asyncio.gather(*(_concept(ctx, cik, c) for c in candidates[1:]), return_exceptions=True)
    failed = next((r for r in rest if isinstance(r, BaseException)), None)
    options = [first] + [r if isinstance(r, dict) else None for r in rest]
    options = [o for o in options if o]
    if not options:
        return None, failed
    return max(options, key=lambda o: o["latest_end"]), failed  # max() keeps the earliest (preferred) on ties


@tool(
    "get_company_financials",
    "Official as-filed fundamentals from SEC EDGAR (XBRL data in 10-K/10-Q filings) for a US-listed company: revenue, "
    "net income, diluted EPS and operating cash flow for the last 8 quarters and last 3 fiscal years, each with "
    "year-over-year growth. Use for questions about a company's sales, profits, earnings history, cash generation "
    "or growth trend. Figures are in USD millions (EPS in USD/share) and update when the company files. Not for "
    "prices or valuation (use get_market_data) or upcoming earnings dates/estimates (use get_earnings_calendar).",
    {"ticker": {"type": "string", "description": "US ticker (e.g. AAPL, MSFT, BRK.B) or company name."}},
    ["ticker"],
    label="Reading SEC financials",
    timeout=90,  # ticker list, then up to two rounds of concept requests
)
@_guard("SEC EDGAR")
async def get_company_financials(ctx, ticker: str):
    company = await _resolve_company(ctx, ticker)
    cik = company["cik"]
    raw_firsts = await asyncio.gather(*(_concept(ctx, cik, cands[0]) for _, cands in FIN_METRICS), return_exceptions=True)
    for res in raw_firsts:
        if isinstance(res, SourceError) and res.status == 403:
            raise res
    firsts = [r if isinstance(r, dict) else None for r in raw_firsts]
    found = [f["latest_end"] for f in firsts if f]
    reference = max(found) if found else None
    chosen = await asyncio.gather(
        *(_metric(ctx, cik, cands, first, reference) for (_, cands), first in zip(FIN_METRICS, firsts)),
        return_exceptions=True,
    )
    series: dict[str, dict] = {}
    failures: dict[str, BaseException] = {}  # metric -> why it may be missing (an outage, not "not reported")
    for (metric, _), first_res, res in zip(FIN_METRICS, raw_firsts, chosen):
        picked, err = res if isinstance(res, tuple) else (None, res)
        if isinstance(picked, dict):
            series[metric] = picked
            continue
        err = err or (first_res if isinstance(first_res, BaseException) else None)
        if err is not None:
            failures[metric] = err
    name = company["name"] or next((s.get("entity") for s in series.values() if s.get("entity")), None) or f"CIK {_cik10(cik)}"
    if not series:
        problems = [e for e in failures.values() if isinstance(e, ToolError)]
        if problems:
            raise problems[0]
        if failures:
            raise ToolError(f"SEC EDGAR's financial data for {name} is unavailable right now. Try again later.")
        raise ToolError(
            f"SEC EDGAR has no US-GAAP financial data for {name}. (Companies filing under IFRS, such as many "
            "foreign issuers, and funds aren't covered.)"
        )
    quarters = build_financial_rows(series, "quarters", 8)
    years = build_financial_rows(series, "annuals", 3)
    currency = next((s["unit"] for s in series.values() if "/" not in s["unit"]), "USD")
    sign = _prefix(currency.lower())
    parts = []
    if quarters:
        q = quarters[0]
        parts.append(f"{name} {q['period']}")
        if q.get("revenue") is not None:
            parts.append(f"revenue {_compact(q['revenue'] * 1e6, sign)} ({_signed(q.get('revenue_yoy_pct'))}% YoY)")
        if q.get("net_income") is not None:
            parts.append(f"net income {_compact(q['net_income'] * 1e6, sign)}")
        if q.get("eps_diluted") is not None:
            parts.append(f"EPS {sign}{q['eps_diluted']:.2f}")
    out: dict[str, Any] = {
        "status": ", ".join(parts) if parts else f"{name}: annual figures only",
        "company": name,
        "ticker": company["ticker"],
        "cik": _cik10(cik),
        "units": f"{currency} millions; EPS in {currency} per diluted share; *_yoy_pct = % change vs the same period a year earlier",
        "quarters": quarters,
        "fiscal_years": years,
        "concepts": {m: s["concept"] for m, s in series.items()},
        "source": "SEC EDGAR XBRL (company 10-K/10-Q filings)",
    }
    missing = [m for m, _ in FIN_METRICS if m not in series and m not in failures]
    if missing:
        out["missing"] = missing
    if failures:
        out["unavailable"] = [f"{m} ({_friendly(e)})" for m, e in failures.items()]
    if any(r.get("derived") for r in quarters):
        out["note"] = (
            "Quarters listed under 'derived' weren't filed as 3-month figures; they're computed from year-to-date "
            "totals (e.g. Q4 = full year minus nine months; cash flow is reported YTD). Derived EPS is approximate."
        )
    return out


# =========================================================================== EARNINGS CALENDAR (Nasdaq)
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"  # unofficial; best-effort
NASDAQ_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
NASDAQ_SOURCE = "Nasdaq's earnings calendar"
EARNINGS_TIME = {"time-pre-market": "before market open", "time-after-hours": "after market close", "time-not-supplied": "not specified"}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _today() -> date:  # the user's local date (module-level so tests can pin it)
    return datetime.now().date()


def parse_day(value, today: date | None = None) -> date:
    today = today or _today()
    v = _text(value).lower().strip(" .")
    if v in ("", "today", "now", "tonight", "this morning"):
        return today
    if v == "tomorrow":
        return today + timedelta(days=1)
    if v == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"(next |this )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", v)
    if m:
        ahead = (_WEEKDAYS.index(m.group(2)) - today.weekday()) % 7
        if m.group(1) == "next " and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead)
    d = _iso(v)
    if d:
        return d
    try:
        return datetime.strptime(v, "%m/%d/%Y").date()
    except ValueError:
        pass
    raise ToolError("Give the date as YYYY-MM-DD (or today, tomorrow, or a weekday).")


def _us_date(value) -> str | None:
    try:
        return datetime.strptime(_text(value), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _earning_row(row: dict, day: date) -> dict:
    mcap = _num(row.get("marketCap"))
    out = {
        "symbol": _text(row.get("symbol")).upper(),
        "company": _text(row.get("name"))[:60],
        "date": day.isoformat(),
        "time": EARNINGS_TIME.get(_text(row.get("time")), "not specified"),
        "eps_forecast": _num(row.get("epsForecast")),
        "estimates": int(_num(row.get("noOfEsts"))) if _num(row.get("noOfEsts")) is not None else None,
        "last_year_eps": _num(row.get("lastYearEPS")),
        "last_year_report_date": _us_date(row.get("lastYearRptDt")),
        "fiscal_quarter": _text(row.get("fiscalQuarterEnding")) or None,
        "market_cap": _compact(mcap),
        "_mcap": mcap or 0.0,
    }
    if _num(row.get("eps")) is not None:  # past dates sometimes carry the reported figure
        out["eps_actual"] = _num(row.get("eps"))
    if _num(row.get("surprise")) is not None:
        out["surprise_pct"] = _num(row.get("surprise"))
    return out


async def _nasdaq_day(ctx, day: date) -> list[dict]:
    key = ("nasdaq_earnings", day.isoformat())
    hit = _cache_get(key, 30 * 60)
    if hit is not None:
        return hit
    r = await _fetch(ctx, NASDAQ_EARNINGS_URL, NASDAQ_SOURCE, params={"date": day.isoformat()}, headers=NASDAQ_HEADERS, timeout=15)
    _check(
        r,
        NASDAQ_SOURCE,
        {
            403: f"{NASDAQ_SOURCE} (an unofficial feed) refused the request (HTTP 403). Try web_search for the earnings schedule.",
            429: f"{NASDAQ_SOURCE} is limiting requests right now. Try again in a minute.",
        },
    )
    data = _json(r, NASDAQ_SOURCE)
    if not isinstance(data, dict):
        raise SourceError(f"{NASDAQ_SOURCE} sent an unexpected reply.")
    body = data.get("data")
    if not isinstance(body, dict):
        code = (data.get("status") or {}).get("rCode") if isinstance(data.get("status"), dict) else None
        if code not in (None, 200):
            raise SourceError(f"{NASDAQ_SOURCE} couldn't answer for {day.isoformat()} (code {code}).")
        body = {}
    rows = [_earning_row(r_, day) for r_ in (body.get("rows") or []) if isinstance(r_, dict) and _text(r_.get("symbol"))]
    _cache_put(key, rows)
    return rows


def _sym_key(symbol: str) -> str:
    return re.sub(r"[./\-\s]", "", symbol.upper())


def _scan_days(start: date, days: int) -> list[date]:
    if days <= 1:
        return [start]
    out, d = [], start
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@tool(
    "get_earnings_calendar",
    "Which companies report quarterly earnings on a given date (Nasdaq's earnings calendar): report time (before market "
    "open / after market close), consensus EPS forecast, number of analyst estimates, last year's EPS, and market cap, "
    "biggest companies first. Use for 'who reports earnings today/tomorrow/this week' and, with symbols plus days "
    "(up to 10 weekdays ahead), for 'when does NVDA report?'. Unofficial source: best-effort; if it fails, use web_search.",
    {
        "date": {"type": "string", "description": "YYYY-MM-DD, 'today', 'tomorrow', a weekday, 'this week' or 'next week'. Default today."},
        "symbols": {"type": "array", "items": {"type": "string"}, "description": "Optional tickers to look for, e.g. ['NVDA','AAPL']."},
        "days": {"type": "integer", "description": "Scan this many weekdays starting at date (1-10). Default 1."},
        "limit": {"type": "integer", "description": "Max companies listed, 1-50. Default 25."},
    },
    label="Checking the earnings calendar",
)
@_guard(NASDAQ_SOURCE)
async def get_earnings_calendar(ctx, date: str = "today", symbols=None, days: int = 1, limit: int = 25):
    days = _int(days, 1, 1, 10)
    week = re.fullmatch(r"(this|next)\s+week", _text(date).lower().strip(" ."))
    if week:  # "this week" = today through Friday; "next week" = next Monday through Friday
        today = _today()
        upcoming = week.group(1) == "next" or today.weekday() >= 5  # on a weekend "this week" is the coming one
        start = today + timedelta(days=7 - today.weekday()) if upcoming else today
        days = max(days, 5 - start.weekday())
    else:
        start = parse_day(date)
    limit = _int(limit, 25, 1, 50)
    wanted = [s.upper().lstrip("$") for s in _list(symbols)]
    scan = _scan_days(start, days)
    results = await asyncio.gather(*(_nasdaq_day(ctx, d) for d in scan), return_exceptions=True)
    rows: list[dict] = []
    failed: list[str] = []
    first_error: BaseException | None = None
    for d, res in zip(scan, results):
        if isinstance(res, BaseException):
            first_error = first_error or res
            failed.append(d.isoformat())
        else:
            rows.extend(res)
    if first_error is not None and len(failed) == len(scan):
        raise first_error
    if wanted:
        norm = {_sym_key(w) for w in wanted}  # "BRK.B" finds Nasdaq's "BRK/B"
        rows = [r for r in rows if _sym_key(r["symbol"]) in norm]
        rows.sort(key=lambda r: (r["date"], -r["_mcap"]))
    else:
        rows.sort(key=lambda r: -r["_mcap"])
    total = len(rows)
    # One date scanned: every row has that date, which is already at the top level.
    drop = {"_mcap"} if len(scan) > 1 else {"_mcap", "date"}
    shown, _ = _fit_rows([{k: v for k, v in r.items() if k not in drop} for r in rows[:limit]])
    when = start.strftime("%a %b %d, %Y").replace(" 0", " ") if len(scan) == 1 else f"{scan[0].isoformat()} to {scan[-1].isoformat()}"
    if wanted:
        hits = {_sym_key(r["symbol"]) for r in rows}
        missing = [s for s in wanted if _sym_key(s) not in hits]
        if shown:
            status = "; ".join(f"{r['symbol']} reports {r.get('date', start.isoformat())} ({r['time']})" for r in shown[:3])
        else:
            status = f"No earnings for {', '.join(wanted)} on {when}"
    else:
        missing = []
        if total:
            status = f"{total} companies report earnings on {when}" + (f"; the {len(shown)} largest shown" if total > len(shown) else "")
        else:
            weekend = len(scan) == 1 and start.weekday() >= 5
            status = f"No earnings scheduled on {when}" + (" (weekend)" if weekend else "")
    out: dict[str, Any] = {"status": status, "date": start.isoformat(), "count": total, "earnings": shown}
    if len(scan) > 1:
        out["dates_scanned"] = [d.isoformat() for d in scan]
    if missing:
        out["not_found"] = missing
        out["hint"] = "Not scheduled in this window; try more days or a later date, or web_search for the expected date."
    if failed:
        out["unavailable_dates"] = failed
    out["source"] = "Nasdaq earnings calendar (unofficial; best-effort)"
    out["note"] = "EPS figures are per-share; consensus estimates can change. Times are relative to US market hours (open 9:30, close 16:00 ET)."
    return out


# =========================================================================== MARKET SENTIMENT (CNN + VIX)
CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"  # unofficial; best-effort
CNN_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://edition.cnn.com",
    "Referer": "https://edition.cnn.com/",
}
CNN_SOURCE = "CNN's Fear & Greed index"
FG_COMPONENTS = {
    "market_momentum_sp500": "Market momentum (S&P 500 vs its 125-day average)",
    "stock_price_strength": "Stock price strength (52-week highs vs lows)",
    "stock_price_breadth": "Stock price breadth (advancing vs declining volume)",
    "put_call_options": "Put/call options ratio",
    "market_volatility_vix": "Market volatility (VIX vs its 50-day average)",
    "junk_bond_demand": "Junk bond demand",
    "safe_haven_demand": "Safe-haven demand (stocks vs bonds)",
}
FG_SCALE = "0-24 extreme fear, 25-44 fear, 45-55 neutral, 56-75 greed, 76-100 extreme greed"


def fg_band(score: float) -> str:
    s = round(score)
    if s < 25:
        return "extreme fear"
    if s < 45:
        return "fear"
    if s <= 55:
        return "neutral"
    if s <= 75:
        return "greed"
    return "extreme greed"


def _fg_point(value) -> dict | None:
    v = _num(value)
    return None if v is None else {"score": round(v), "rating": fg_band(v)}


def vix_level(v: float) -> str:
    if v < 12:
        return "very calm"
    if v < 20:
        return "calm / normal"
    if v < 30:
        return "elevated (nervous)"
    return "high (fearful)"


async def _fear_greed(ctx) -> dict:
    hit = _cache_get(("cnn_fg",), 300)
    if hit is not None:
        return hit
    blocked = f"{CNN_SOURCE} (an unofficial feed) refused the request right now."
    r = await _fetch(ctx, CNN_FG_URL, CNN_SOURCE, headers=CNN_HEADERS, timeout=12)
    _check(r, CNN_SOURCE, {403: blocked, 418: blocked})
    data = _json(r, CNN_SOURCE)
    fg = data.get("fear_and_greed") if isinstance(data, dict) else None
    score = _num(fg.get("score")) if isinstance(fg, dict) else None
    if score is None:
        raise SourceError(f"{CNN_SOURCE} sent no score.")
    rating = _text(fg.get("rating")).lower() or fg_band(score)
    components = []
    for key, label in FG_COMPONENTS.items():
        comp = data.get(key)
        if isinstance(comp, dict) and _num(comp.get("score")) is not None:
            components.append({"name": label, "score": round(_num(comp["score"])), "rating": _text(comp.get("rating")).lower() or fg_band(_num(comp["score"]))})
    out = {
        "score": round(score),
        "rating": rating,
        "previous_close": _fg_point(fg.get("previous_close")),
        "one_week_ago": _fg_point(fg.get("previous_1_week")),
        "one_month_ago": _fg_point(fg.get("previous_1_month")),
        "one_year_ago": _fg_point(fg.get("previous_1_year")),
        "as_of": _text(fg.get("timestamp")) or None,
        "components": components,
    }
    _cache_put(("cnn_fg",), out)
    return out


async def _vix_quote(ctx) -> dict:
    """The VIX via markets.fetch_quote, whose errors embed raw httpx text (URLs, docs links): keep it friendly."""
    try:
        return await markets.fetch_quote(ctx.http, "^VIX")
    except Exception:
        raise SourceError("Yahoo Finance didn't return the VIX right now.") from None


def _vix_summary(q: dict) -> dict | None:
    v = _num(q.get("price"))
    if v is None:
        return None
    return {"value": round(v, 2), "change": q.get("change"), "change_pct": q.get("change_pct"), "level": vix_level(v), "as_of": q.get("as_of")}


@tool(
    "get_market_sentiment",
    "The US stock market's current mood: CNN's Fear & Greed Index (0-100 score and rating, versus the previous close, "
    "1 week, 1 month and 1 year ago, plus its seven components) and the VIX volatility index ('fear gauge'). Use for "
    "'how's market sentiment', 'are investors fearful or greedy', 'fear and greed index', 'how volatile is the market'. "
    "CNN's feed is unofficial (best-effort); the VIX comes from Yahoo Finance and may be delayed ~15 minutes.",
    {},
    label="Gauging market sentiment",
)
@_guard("market sentiment sources")
async def get_market_sentiment(ctx):
    fg, vix_q = await asyncio.gather(_fear_greed(ctx), _vix_quote(ctx), return_exceptions=True)
    unavailable = []
    if isinstance(fg, BaseException):
        unavailable.append(f"Fear & Greed ({_friendly(fg)})")
        fg = None
    vix = None
    if isinstance(vix_q, BaseException):
        unavailable.append(f"VIX ({_friendly(vix_q)})")
    elif isinstance(vix_q, dict):
        vix = _vix_summary(vix_q)
    if fg is None and vix is None:
        raise ToolError("Market sentiment data is unavailable right now: " + "; ".join(unavailable or ["no data"]))
    parts = []
    if fg:
        parts.append(f"Fear & Greed {fg['score']}/100 ({fg['rating']})")
    if vix:
        parts.append(f"VIX {vix['value']:.1f} ({vix['level']})")
    out: dict[str, Any] = {"status": " · ".join(parts), "fear_greed": fg, "vix": vix}
    if fg:
        out["scale"] = FG_SCALE
    if unavailable:
        out["unavailable"] = unavailable
    out["source"] = "CNN Fear & Greed (unofficial); VIX via Yahoo Finance (may be delayed ~15 min)"
    return out


# =========================================================================== ECONOMY (FRED)
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"  # public download; not a formal API (best-effort)
FRED_SOURCE = "FRED (St. Louis Fed)"


def _ind(series, name, transform, unit, freq, decimals=2, fallback=None, lookback=None, note=None):
    lookback = lookback or {"daily": 75, "weekly": 400, "monthly": 4 * 366 if transform == "yoy" else 3 * 366, "quarterly": 5 * 366}[freq]
    return {"series": series, "name": name, "transform": transform, "unit": unit, "freq": freq, "decimals": decimals, "fallback": fallback, "lookback": lookback, "note": note}


_SA_NOTE = "Computed from FRED's seasonally adjusted index; the headline BLS figure (not seasonally adjusted) can differ by ~0.1 point."
INDICATORS = {
    # Inflation to one decimal, as BLS and BEA publish it ("3.0%", not a falsely precise "3.02%").
    "cpi": _ind("CPIAUCSL", "CPI inflation", "yoy", "% YoY", "monthly", 1, note=_SA_NOTE),
    "core_cpi": _ind("CPILFESL", "Core CPI inflation (ex food & energy)", "yoy", "% YoY", "monthly", 1, note=_SA_NOTE),
    "pce": _ind("PCEPI", "PCE price inflation", "yoy", "% YoY", "monthly", 1),
    "core_pce": _ind("PCEPILFE", "Core PCE inflation (the Fed's preferred gauge)", "yoy", "% YoY", "monthly", 1),
    "unemployment": _ind("UNRATE", "Unemployment rate", "level", "%", "monthly", 1),
    "payrolls": _ind("PAYEMS", "Nonfarm payrolls, monthly change", "diff", "thousand jobs", "monthly", 0),
    "fed_funds": _ind("DFF", "Effective federal funds rate", "level", "%", "daily", fallback="FEDFUNDS"),
    "gdp_growth": _ind("A191RL1Q225SBEA", "Real GDP growth (annualized, quarter over quarter)", "level", "%", "quarterly", 1),
    "10y_treasury": _ind("DGS10", "10-year Treasury yield", "level", "%", "daily"),
    "mortgage_30y": _ind("MORTGAGE30US", "30-year fixed mortgage rate (Freddie Mac)", "level", "%", "weekly"),
    "consumer_sentiment": _ind("UMCSENT", "University of Michigan consumer sentiment", "level", "index (1966 Q1 = 100)", "monthly", 1),
    "jobless_claims": _ind("ICSA", "Initial jobless claims (weekly, seasonally adjusted)", "level", "claims", "weekly", 0),
    "retail_sales": _ind("RSAFS", "Retail sales, monthly change", "mom", "% MoM", "monthly", 1),
    "housing_starts": _ind("HOUST", "Housing starts", "level", "thousand units (annual rate)", "monthly", 0),
}
OVERVIEW = ["cpi", "core_cpi", "unemployment", "payrolls", "fed_funds", "gdp_growth", "10y_treasury", "mortgage_30y", "consumer_sentiment", "jobless_claims"]
_OVERVIEW_ALIASES = {"all", "overview", "dashboard", "economy", "summary", "macro", "everything", "main"}
INDICATOR_ALIASES = {
    "inflation": "cpi", "cpi_inflation": "cpi", "consumer_price_index": "cpi", "headline_cpi": "cpi",
    "core_inflation": "core_cpi", "corecpi": "core_cpi",
    "pce_inflation": "pce", "pcepi": "pce", "corepce": "core_pce", "core_pce_inflation": "core_pce",
    "unemployment_rate": "unemployment", "jobless_rate": "unemployment", "unrate": "unemployment",
    "jobs": "payrolls", "nonfarm_payrolls": "payrolls", "nfp": "payrolls", "jobs_report": "payrolls", "job_growth": "payrolls", "employment": "payrolls",
    "fed_funds_rate": "fed_funds", "federal_funds": "fed_funds", "federal_funds_rate": "fed_funds", "fed_rate": "fed_funds", "interest_rate": "fed_funds", "interest_rates": "fed_funds", "fedfunds": "fed_funds",
    "gdp": "gdp_growth", "real_gdp": "gdp_growth", "economic_growth": "gdp_growth",
    "10y": "10y_treasury", "10_year": "10y_treasury", "10_year_treasury": "10y_treasury", "treasury": "10y_treasury", "treasury_yield": "10y_treasury", "10y_yield": "10y_treasury", "ten_year": "10y_treasury", "bond_yield": "10y_treasury",
    "mortgage": "mortgage_30y", "mortgage_rate": "mortgage_30y", "mortgage_rates": "mortgage_30y", "30y_mortgage": "mortgage_30y", "30_year_mortgage": "mortgage_30y",
    "sentiment": "consumer_sentiment", "consumer_confidence": "consumer_sentiment", "michigan_sentiment": "consumer_sentiment",
    "claims": "jobless_claims", "initial_claims": "jobless_claims", "unemployment_claims": "jobless_claims",
    "initial_jobless_claims": "jobless_claims", "weekly_claims": "jobless_claims",
    "10_year_yield": "10y_treasury", "10_yr": "10y_treasury", "10yr": "10y_treasury", "ten_year_yield": "10y_treasury",
    "price_inflation": "cpi", "consumer_prices": "cpi", "core": "core_cpi", "pce_price_index": "pce",
    "jobs_added": "payrolls", "payroll": "payrolls", "non_farm_payrolls": "payrolls", "fed": "fed_funds",
    "retail": "retail_sales",
    "housing": "housing_starts", "starts": "housing_starts",
}


def _indicator_key(value) -> str | None:
    """Friendly name -> INDICATORS key, 'overview', or None (maybe a raw FRED series id)."""
    k = re.sub(r"[\s\-./]+", "_", _text(value).lower()).strip("_")
    k = re.sub(r"^(the|us|u_s)_", "", k)
    if k in ("", *_OVERVIEW_ALIASES):
        return "overview"
    for cand in (k, re.sub(r"_(rate|rates|index|data|report|numbers|level|growth_rate)$", "", k)):
        hit = cand if cand in INDICATORS else INDICATOR_ALIASES.get(cand)
        if hit:
            return hit
    return None


def _indicator_terms(value) -> list[str]:
    """One indicator, or several ('cpi, unemployment', ['cpi', 'gdp'], 'inflation and jobs')."""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        out.extend(p for p in re.split(r"\s*(?:,|;|&|\band\b|\bplus\b)\s*", _text(item), flags=re.I) if p)
    return out[:12]


def parse_fred_csv(text: str, series_id: str) -> list[tuple[date, float]]:
    """fredgraph.csv ('observation_date,SERIES' or legacy 'DATE,SERIES'; '.'/blank = missing) -> ascending."""
    rows = csv.reader(io.StringIO((text or "").lstrip("﻿")))
    header = next(rows, None)
    if not header or len(header) < 2:
        return []
    idx = next((i for i, h in enumerate(header) if h.strip().upper() == series_id.upper()), 1)
    out = []
    for row in rows:
        if len(row) <= idx:
            continue
        d, v = _iso(row[0].strip()), _num(row[idx])
        if d is not None and v is not None:
            out.append((d, v))
    out.sort()
    return out


async def _fred_api(ctx, series_id: str, api_key: str, limit: int) -> list[tuple[date, float]]:
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": limit}
    r = await _fetch(ctx, FRED_API_URL, "FRED's API", params=params, headers={"Accept": "application/json", "User-Agent": ATLAS_UA}, timeout=15)
    if r.status_code in (400, 401, 403):
        try:
            detail = str((r.json() or {}).get("error_message") or "").lower()
        except (ValueError, AttributeError):
            detail = ""
        if "api_key" in detail or "api key" in detail or r.status_code in (401, 403):
            raise SourceError("FRED rejected the API key in FRED_API_KEY (check it in Settings).", r.status_code)
        if "series" in detail:
            raise SourceError(f"FRED has no series '{series_id}'.", 404)
        raise SourceError(f"FRED's API refused the request (HTTP {r.status_code}).", r.status_code)
    _check(r, "FRED's API", {429: "FRED's API is rate-limiting requests right now."})
    data = _json(r, "FRED's API")
    obs = []
    for o in (data.get("observations") if isinstance(data, dict) else None) or []:
        if isinstance(o, dict):
            d, v = _iso(o.get("date")), _num(o.get("value"))  # missing values are "."
            if d is not None and v is not None:
                obs.append((d, v))
    obs.sort()
    if not obs:
        raise SourceError(f"FRED's API returned no observations for {series_id}.")
    return obs


async def _fred_csv(ctx, series_id: str, lookback_days: int) -> list[tuple[date, float]]:
    # cosd (observation start) trims the download; harmless if FRED ignores it.
    params = {"id": series_id, "cosd": (datetime.now().date() - timedelta(days=lookback_days)).isoformat()}
    r = await _fetch(ctx, FRED_CSV_URL, FRED_SOURCE, params=params, headers={"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"}, timeout=20)
    if r.status_code in (400, 404):
        raise SourceError(f"FRED has no series '{series_id}'.", 404)
    _check(r, FRED_SOURCE)
    text = r.text or ""
    if "<html" in text[:600].lower():
        raise SourceError(f"FRED's CSV download for {series_id} sent a web page instead of data (unknown series, or FRED is busy).")
    obs = parse_fred_csv(text, series_id)
    if not obs:
        raise SourceError(f"FRED returned no data for {series_id}.")
    return obs


async def fred_series(ctx, series_id: str, lookback_days: int = 3 * 366, limit: int = 60) -> tuple[list[tuple[date, float]], str, str | None]:
    """Observations (ascending) via the official API when FRED_API_KEY is set, else (or on failure) the public CSV.

    Returns (observations, via, note)."""
    key = ("fred", series_id, lookback_days, limit)
    hit = _cache_get(key, 3600)
    if hit is not None:
        return hit
    api_key = _setting("FRED_API_KEY")
    note = None
    if api_key:
        try:
            result = (await _fred_api(ctx, series_id, api_key, limit), "FRED API", None)
            _cache_put(key, result)
            return result
        except ToolError as exc:
            if getattr(exc, "status", None) == 404:
                raise
            note = _scrub(f"{exc} Used FRED's public CSV download instead.")
        except Exception as exc:  # anything odd: fall back, never surface details (they may contain the key)
            note = f"FRED's API failed ({exc.__class__.__name__}); used FRED's public CSV download instead."
    obs = await _fred_csv(ctx, series_id, lookback_days)
    result = (obs, "FRED public CSV", note)
    _cache_put(key, result)
    return result


_MAX_GAP = {"monthly": 45, "quarterly": 100, "weekly": 10}


def transform_series(obs: list[tuple[date, float]], kind: str, max_gap: int | None = None) -> list[tuple[date, float]]:
    """level | yoy (% vs the same date a year earlier) | diff (change vs previous) | mom (% vs previous).
    diff/mom skip pairs further apart than max_gap days, so a missing month is never treated as one month."""
    if kind in ("diff", "mom") and max_gap:
        pairs = [(a, b) for a, b in zip(obs, obs[1:]) if (b[0] - a[0]).days <= max_gap]
    else:
        pairs = list(zip(obs, obs[1:]))
    if kind == "yoy":
        by_date = dict(obs)
        out = []
        for d, v in obs:
            try:
                prior = by_date.get(d.replace(year=d.year - 1))
            except ValueError:  # 29 Feb
                prior = None
            if prior:
                out.append((d, (v / prior - 1) * 100))
        return out
    if kind == "diff":
        return [(d1, v1 - v0) for (_, v0), (d1, v1) in pairs]
    if kind == "mom":
        return [(d1, (v1 / v0 - 1) * 100) for (_, v0), (d1, v1) in pairs if v0]
    return list(obs)


def _period(d: date, freq: str) -> str:
    if freq == "monthly":
        return d.strftime("%b %Y")
    if freq == "quarterly":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    if freq == "annual":
        return str(d.year)
    return d.isoformat()


def _rnd(v: float, nd: int):
    return int(round(v)) if nd == 0 else round(v, nd)


def indicator_summary(key: str, spec: dict, obs: list[tuple[date, float]], series_id: str | None = None) -> dict:
    series = transform_series(obs, spec["transform"], _MAX_GAP.get(spec["freq"]))
    if not series:
        raise SourceError(f"FRED doesn't have enough recent data to compute {spec['name']}.")
    nd = spec["decimals"]
    d1, v1 = series[-1]
    prev = series[-2] if len(series) > 1 else None
    return {
        "indicator": key,
        "name": spec["name"],
        "value": _rnd(v1, nd),
        "unit": spec["unit"],
        "period": _period(d1, spec["freq"]),
        "date": d1.isoformat(),
        "previous": _rnd(prev[1], nd) if prev else None,
        "previous_period": _period(prev[0], spec["freq"]) if prev else None,
        "change": _rnd(_rnd(v1, nd) - _rnd(prev[1], nd), nd) if prev else None,  # consistent with the shown values
        "trend": [{"period": _period(d, spec["freq"]), "value": _rnd(v, nd)} for d, v in series[-6:]],
        "series_id": series_id or spec["series"],
        "frequency": spec["freq"],
    }


def _lookup_limit(spec: dict) -> int:
    return 40 if spec["transform"] == "yoy" else 30


async def _indicator(ctx, key: str, spec: dict) -> tuple[dict, str, str | None]:
    try:
        obs, via, note = await fred_series(ctx, spec["series"], spec["lookback"], _lookup_limit(spec))
        sid = spec["series"]
    except ToolError:
        if not spec.get("fallback"):
            raise
        sid = spec["fallback"]
        fb = dict(spec, freq="monthly", lookback=3 * 366)
        obs, via, note = await fred_series(ctx, sid, fb["lookback"], _lookup_limit(fb))
        spec = fb
    return indicator_summary(key, spec, obs, sid), via, note


def _short_value(s: dict) -> str:
    unit = s["unit"]
    v = s["value"]
    if unit.startswith("%"):
        return f"{v}%" + (" YoY" if "YoY" in unit else " MoM" if "MoM" in unit else "")
    if unit == "thousand jobs":
        return f"{v:+,}K jobs"
    if unit == "claims":
        return f"{v:,}"
    if unit.startswith("thousand units"):
        return f"{v:,}K annualized"
    return f"{v}"


@tool(
    "get_economic_data",
    "Latest official US economic data from FRED (St. Louis Fed). indicator: cpi, core_cpi, pce, core_pce (inflation, "
    "% year over year), unemployment (%), payrolls (monthly job gains, thousands), fed_funds (effective rate + target "
    "range), gdp_growth (real, annualized q/q %), 10y_treasury, mortgage_30y, consumer_sentiment, jobless_claims "
    "(weekly), retail_sales (% month over month), housing_starts -- or 'overview' for a dashboard of the main ones. A "
    "raw FRED series id (e.g. T10Y2Y) also works. Returns the latest value and the period it covers, previous value, "
    "change and a short trend. Use for questions about inflation, jobs, rates, growth or the economy; data is released "
    "monthly/weekly with a lag, so always say which month or date a figure is for.",
    {"indicator": {"type": "string", "description": "Indicator name (see description), several comma-separated (e.g. 'cpi, unemployment, gdp_growth'), a FRED series id, or 'overview'. Default 'overview'."}},
    label="Pulling economic data",
)
@_guard(FRED_SOURCE)
async def get_economic_data(ctx, indicator: str = "overview"):
    terms = _indicator_terms(indicator)
    if len(terms) > 1:  # several named indicators -> a dashboard of just those
        keys = [_indicator_key(t) for t in terms]
        if "overview" in keys:
            return await _economy_overview(ctx)
        known = list(dict.fromkeys(k for k in keys if k))
        unknown = [t for t, k in zip(terms, keys) if not k]
        if not known:
            raise ToolError(f"Unknown indicators {', '.join(map(repr, unknown))}. Try: {', '.join(INDICATORS)}, or 'overview'.")
        return await _economy_overview(ctx, known, unknown)
    indicator = terms[0] if terms else ""
    key = _indicator_key(indicator)
    if key == "overview":
        return await _economy_overview(ctx)
    if key is None:
        raw = _text(indicator).upper().replace(" ", "")
        if not re.fullmatch(r"[A-Z0-9_]{2,30}", raw):
            raise ToolError(
                f"Unknown indicator '{_text(indicator)}'. Try one of: {', '.join(INDICATORS)}, or 'overview'."
            )
        spec = _ind(raw, f"FRED series {raw}", "level", "as published by FRED", "daily", 3, lookback=5 * 366)
        try:
            obs, via, note = await fred_series(ctx, raw, spec["lookback"], 30)
        except SourceError as exc:
            if exc.status != 404:
                raise
            raise ToolError(
                f"Unknown indicator '{_text(indicator)}' (FRED has no series with that id). Try one of: "
                f"{', '.join(INDICATORS)}, or 'overview'."
            ) from None
        spec["freq"] = _guess_freq(obs)
        summary = indicator_summary(raw.lower(), spec, obs, raw)
    else:
        spec = INDICATORS[key]
        summary, via, note = await _indicator(ctx, key, spec)
        if key == "fed_funds":
            summary["target_range"] = await _fed_target(ctx)
    out: dict[str, Any] = {"status": f"{summary['name']}: {_short_value(summary)} ({summary['period']})"}
    out.update(summary)
    if key and INDICATORS.get(key, {}).get("note"):
        out["note"] = INDICATORS[key]["note"]
    if note:
        out["source_note"] = note
    out["source"] = f"{FRED_SOURCE}, series {summary['series_id']} ({via})"
    out["link"] = f"https://fred.stlouisfed.org/series/{summary['series_id']}"
    return out


def _guess_freq(obs: list[tuple[date, float]]) -> str:
    if len(obs) < 2:
        return "daily"
    gap = (obs[-1][0] - obs[-2][0]).days
    if gap <= 4:
        return "daily"
    if gap <= 8:
        return "weekly"
    if gap <= 31:
        return "monthly"
    return "quarterly" if gap <= 100 else "annual"


async def _fed_target(ctx) -> str | None:
    """The FOMC target range (DFEDTARL-DFEDTARU), best-effort."""
    res = await asyncio.gather(*(fred_series(ctx, s, 75, 10) for s in ("DFEDTARL", "DFEDTARU")), return_exceptions=True)
    if any(isinstance(r, BaseException) or not r[0] for r in res):
        return None
    lo, hi = res[0][0][-1][1], res[1][0][-1][1]
    return f"{lo:.2f}-{hi:.2f}%"


async def _economy_overview(ctx, keys: list[str] | None = None, unknown: list[str] | None = None) -> dict:
    keys = keys or OVERVIEW
    results = await asyncio.gather(*(_indicator(ctx, k, INDICATORS[k]) for k in keys), return_exceptions=True)
    items, errors, vias, notes = [], [], set(), set()
    for k, res in zip(keys, results):
        if isinstance(res, BaseException):
            errors.append({"indicator": k, "error": _friendly(res)})
            continue
        s, via, note = res
        vias.add(via)
        if note:
            notes.add(note)
        items.append(
            {
                "indicator": k,
                "name": s["name"],
                "value": s["value"],
                "unit": s["unit"],
                "period": s["period"],
                "previous": s["previous"],
                "change": s["change"],
                "trend": [t["value"] for t in s["trend"]],
            }
        )
    if not items:
        raise ToolError("US economic data is unavailable right now: " + (errors[0]["error"] if errors else "no data"))
    short = {"cpi": "CPI", "core_cpi": "core CPI", "unemployment": "unemployment", "payrolls": "payrolls", "fed_funds": "fed funds", "10y_treasury": "10y"}
    if keys is OVERVIEW:
        bits = [f"{short[i['indicator']]} {_short_value(i)}" for i in items if i["indicator"] in short]
    else:
        bits = [f"{short.get(i['indicator'], i['name'])} {_short_value(i)} ({i['period']})" for i in items]
    out: dict[str, Any] = {"status": "US economy: " + ", ".join(bits[:5]), "indicators": items}
    if errors:
        out["unavailable"] = errors
    if unknown:
        out["unknown_indicators"] = unknown
    if notes:
        out["source_note"] = " ".join(sorted(notes))
    out["source"] = f"{FRED_SOURCE} ({', '.join(sorted(vias))})"
    out["note"] = "Each figure is for the period shown (data is released with a lag). Inflation = % change vs a year earlier."
    return out


# =========================================================================== UI PANELS (never raise)
async def crypto_pulse(ctx) -> dict:
    """Dashboard panel: top coins + total crypto market cap. Cached ~60s; never raises (stale data or an error)."""
    hit = _cache_get(("panel_crypto",), 60)
    if hit is not None:
        return hit
    failed = _cache_get(("panel_crypto_err",), 30)
    if failed is not None:
        return failed
    try:
        coins, glob = await asyncio.gather(_cg_markets(ctx, "usd", None, 6), _cg_global(ctx, "usd"), return_exceptions=True)
        source = "CoinGecko"
        if isinstance(coins, BaseException):
            coins = await _yahoo_crypto(ctx, YAHOO_DEFAULT[:6])
            source = "Yahoo Finance"
            if not coins:
                raise ToolError("Crypto prices are unavailable right now (CoinGecko and Yahoo Finance).")
        out = {
            "coins": [
                {"symbol": c.get("symbol"), "name": c.get("name"), "price": c.get("price"), "change_24h_pct": c.get("change_24h_pct"), "change_7d_pct": c.get("change_7d_pct")}
                for c in coins
            ],
            "global": None if isinstance(glob, BaseException) else {k: glob.get(k) for k in ("total_market_cap", "change_24h_pct", "btc_dominance_pct")},
            "source": source,
            "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        }
        _cache_put(("panel_crypto",), out)
        return out
    except Exception as exc:
        stale = _cache_get(("panel_crypto",), 6 * 3600)
        if stale is not None:
            out = dict(stale, stale=True)
        else:
            out = {"coins": [], "global": None, "source": "CoinGecko", "error": _friendly(exc)}
        _cache_put(("panel_crypto_err",), out)  # back off for 30s either way: a refresh loop mustn't hammer the APIs
        return out


async def sentiment_pulse(ctx) -> dict:
    """Dashboard panel: Fear & Greed score + VIX. Cached ~2 min; never raises."""
    hit = _cache_get(("panel_sentiment",), 120)
    if hit is not None:
        return hit
    failed = _cache_get(("panel_sentiment_err",), 30)
    if failed is not None:
        return failed
    try:
        fg, vix_q = await asyncio.gather(_fear_greed(ctx), _vix_quote(ctx), return_exceptions=True)
        fear_greed = None
        if isinstance(fg, dict):
            fear_greed = {k: fg.get(k) for k in ("score", "rating", "previous_close", "one_week_ago")}
        vix = _vix_summary(vix_q) if isinstance(vix_q, dict) else None
        if fear_greed is None and vix is None:
            reasons = [_friendly(x) for x in (fg, vix_q) if isinstance(x, BaseException)]
            raise ToolError("Sentiment data is unavailable right now" + (f" ({'; '.join(reasons)})" if reasons else "."))
        out = {"fear_greed": fear_greed, "vix": vix, "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes")}
        _cache_put(("panel_sentiment",), out)
        return out
    except Exception as exc:
        stale = _cache_get(("panel_sentiment",), 6 * 3600)
        if stale is not None:
            out = dict(stale, stale=True)
        else:
            out = {"fear_greed": None, "vix": None, "error": _friendly(exc)}
        _cache_put(("panel_sentiment_err",), out)  # back off for 30s either way
        return out
