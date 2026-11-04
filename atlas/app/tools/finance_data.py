"""Deep market data: stock details & fundamentals, market movers, ticker news, technical indicators and the
US Treasury yield curve.

Sources
- Yahoo Finance's JSON API. It is public but UNOFFICIAL (no published contract): the endpoints and field names
  below are the ones finance.yahoo.com itself calls (and yfinance uses), so every call is best-effort and degrades
  gracefully to another source or a friendly error that names the source.
  * v7 quote, v10 quoteSummary and the predefined screeners want a "crumb": a session token tied to Yahoo's A3
    cookie. YahooSession gets one the way yfinance does: GET https://fc.yahoo.com (sets the cookie; a 404 there
    is the normal answer), then GET /v1/test/getcrumb with those cookies. Cookies live in the session's own
    client, and a 401/403/"Invalid Crumb" refreshes the crumb once.
  * EU visitors can be redirected to Yahoo's cookie-consent pages (guce.yahoo.com / consent.yahoo.com). ATLAS
    can't click through those, so the session backs off for a while and tools fall back to the crumb-less
    v8 chart endpoint (markets.fetch_quote), Google News, or the Treasury.
  * v8 chart, v1 search (news) and v1 trending work without a crumb.
- U.S. Department of the Treasury daily par yield curve (official, end of day, no key) for the full curve.

Equity quotes on Yahoo are often delayed ~15 minutes (exchange dependent). Nothing here gives buy/sell advice.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import statistics
import threading
import time
import weakref
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote as urlquote

import httpx

from ..config import load_env_file
from ..util import BROWSER_UA, clip, relative_time
from . import ToolError, markets, tool
from . import news as news_mod

Q1 = "https://query1.finance.yahoo.com"
Q2 = "https://query2.finance.yahoo.com"
COOKIE_URL = "https://fc.yahoo.com"
CRUMB_PATH = "/v1/test/getcrumb"
QUOTE_URL = Q1 + "/v7/finance/quote"
SUMMARY_URL = Q2 + "/v10/finance/quoteSummary/"
SCREENER_URL = Q1 + "/v1/finance/screener/predefined/saved"
TRENDING_URL = Q1 + "/v1/finance/trending/"
SEARCH_URL = Q2 + "/v1/finance/search"
CHART_PATH = "/v8/finance/chart/"
TREASURY_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"

SUMMARY_MODULES = (
    "assetProfile,financialData,defaultKeyStatistics,summaryDetail,calendarEvents,recommendationTrend,earningsTrend"
)
SCREENERS = {"gainers": "day_gainers", "losers": "day_losers", "most_active": "most_actives"}
KIND_LABELS = {"gainers": "day gainers", "losers": "day losers", "most_active": "most active", "trending": "trending tickers"}
_KIND_ALIASES = {
    "gainer": "gainers", "top_gainers": "gainers", "winners": "gainers", "up": "gainers", "day_gainers": "gainers",
    "loser": "losers", "top_losers": "losers", "decliners": "losers", "down": "losers", "day_losers": "losers",
    "active": "most_active", "actives": "most_active", "most_actives": "most_active", "volume": "most_active",
    "trend": "trending", "hot": "trending", "popular": "trending",
}
STATE_LABELS = {
    "PREPRE": "closed", "PRE": "pre-market", "REGULAR": "open", "POST": "after-hours", "POSTPOST": "closed",
    "CLOSED": "closed",
}
TECH_RANGES = ["6mo", "1y", "2y", "5y"]
YIELD_TICKERS = [("3M", "^IRX"), ("5Y", "^FVX"), ("10Y", "^TNX"), ("30Y", "^TYX")]
TREASURY_TENORS = [
    ("1M", "BC_1MONTH"), ("2M", "BC_2MONTH"), ("3M", "BC_3MONTH"), ("4M", "BC_4MONTH"), ("6M", "BC_6MONTH"),
    ("1Y", "BC_1YEAR"), ("2Y", "BC_2YEAR"), ("3Y", "BC_3YEAR"), ("5Y", "BC_5YEAR"), ("7Y", "BC_7YEAR"),
    ("10Y", "BC_10YEAR"), ("20Y", "BC_20YEAR"), ("30Y", "BC_30YEAR"),
]

DELAY_NOTE = "Yahoo Finance data; equity quotes can be delayed ~15 minutes."
CONSENT_MSG = (
    "Yahoo Finance is asking for cookie consent (EU privacy prompt), which ATLAS can't accept for you, "
    "so its detailed quote service is unavailable from here right now."
)

# Cache lifetimes (seconds)
QUOTE_TTL = 30
SUMMARY_TTL = 600
SCREENER_TTL = 60
TRENDING_TTL = 120
NEWS_TTL = 300
BARS_TTL = 300
TREASURY_TTL = 3600
PULSE_TTL = 60
PULSE_PARTIAL_TTL = 20
PULSE_TIMEOUT = 25.0

# Session timings
CRUMB_TTL = 6 * 3600
CONSENT_BACKOFF = 1800.0
FAIL_BACKOFF = 60.0
CRUMB_TIMEOUT = 15.0  # whole cookie + crumb handshake

# Time budgets inside one tool call, so a slow Yahoo still leaves time for the fallback source before the
# tool's own timeout (40-45 s) cuts everything off.
PRIMARY_TIMEOUT = 22.0  # v7 + v10 for get_stock_details
QUOTES_TIMEOUT = 15.0  # one v7 batch for movers/pulse
FALLBACK_TIMEOUT = 15.0  # chart-endpoint fallback
SUGGEST_TIMEOUT = 6.0

# Output budgets (characters of JSON). The agent clips tool results at 14,000 characters, which would cut the
# JSON mid-way, so list results are trimmed well before that.
NEWS_BUDGET = 9000
MAX_LINK = 700

# Rate limiting: spacing between requests to the same host, and a pause after a 429.
HOST_INTERVAL = 0.15
MAX_QUEUE_WAIT = 8.0
COOLDOWN_429 = 45.0

_CRUMB_RE = re.compile(r"[^\s<>{}\"']{4,64}")
_SYMBOL_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^&]{0,19}$")


def _setting(key: str, default: str = "") -> str:
    try:
        env = load_env_file()
    except Exception:  # unreadable .env: behave as if it were empty
        env = {}
    return str(os.environ.get(key) or env.get(key) or default).strip()


def _ua() -> str:
    return _setting("FINANCE_USER_AGENT") or BROWSER_UA


def _region() -> str:
    region = _setting("FINANCE_REGION", "US").upper()
    return region if re.fullmatch(r"[A-Z]{2}", region) else "US"


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())


def _int_arg(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _flag(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)


# --------------------------------------------------------------------------- errors
class YahooError(ToolError):
    """A friendly, user-facing failure talking to a finance data source."""


class YahooConsentRequired(YahooError):
    pass


class YahooAuthError(YahooError):
    pass


class YahooNotFound(YahooError):
    pass


class YahooRateLimited(YahooError):
    pass


def _msg(exc: BaseException) -> str:
    """A short, safe description of a failure (no tracebacks, bodies or URLs with tokens)."""
    if isinstance(exc, ToolError):
        text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return clip(text, 200)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "timed out"
    if isinstance(exc, httpx.HTTPError):
        return f"network error ({exc.__class__.__name__})"
    return exc.__class__.__name__


def _host_label(host: str) -> str:
    if "yahoo" in host:
        return "Yahoo Finance"
    if "treasury" in host:
        return "The US Treasury site"
    return host


# --------------------------------------------------------------------------- rate limiting & caching
class HostLimiter:
    """Spaces requests to each host at least HOST_INTERVAL apart (across concurrent callers) and pauses a host
    for a while after it answers 429. Slots are reserved under a thread lock, so it isn't tied to one event loop."""

    def __init__(self, clock=time.monotonic, sleep=None):
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
                raise YahooRateLimited(
                    f"{_host_label(host)} is limiting requests right now. Try again in about {max(1, math.ceil(until - now))} seconds."
                )
            slot = max(now, self._next.get(host, 0.0))
            delay = slot - now
            if delay > MAX_QUEUE_WAIT:
                raise YahooRateLimited("Too many finance data requests at once. Try again in a few seconds.")
            self._next[host] = slot + HOST_INTERVAL
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

CACHE_MAX = 512
_CACHE: dict[tuple, tuple[float, object]] = {}


def _cache_get(key: tuple, ttl: float):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key: tuple, value) -> None:
    _CACHE.pop(key, None)
    _CACHE[key] = (time.time(), value)
    while len(_CACHE) > CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))


# --------------------------------------------------------------------------- HTTP helpers
def _is_consent(r: httpx.Response) -> bool:
    """True when Yahoo bounced the request to its EU cookie-consent flow."""
    hosts = [h.url.host for h in r.history] + [r.url.host]
    if any(h.startswith(("consent.", "guce.")) and h.endswith("yahoo.com") for h in hosts):
        return True
    location = r.headers.get("location", "")
    return "consent.yahoo.com" in location or "guce.yahoo.com" in location


def _invalid_crumb(r: httpx.Response) -> bool:
    if r.status_code < 400:
        return False
    try:
        return "invalid crumb" in r.text[:800].lower()
    except Exception:
        return False


def _check(r: httpx.Response, what: str) -> None:
    if _is_consent(r):
        raise YahooConsentRequired(CONSENT_MSG)
    code = r.status_code
    if code == 429:
        _LIMITER.cooldown(r.url.host)
        raise YahooRateLimited(f"{what} is limiting requests right now (HTTP 429). Try again in a minute.")
    if code in (401, 403) or _invalid_crumb(r):
        raise YahooAuthError(f"{what} refused the request (HTTP {code}).")
    if code == 404:
        raise YahooNotFound(f"{what} has no data for that (HTTP 404).")
    if code >= 400:
        raise YahooError(f"{what} is unavailable right now (HTTP {code}).")
    if 300 <= code < 400:
        raise YahooError(f"{what} sent an unexpected redirect (HTTP {code}).")


def _json(r: httpx.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise YahooError(f"{what} sent a reply that isn't data (perhaps a sign-in or consent page).") from None
    if not isinstance(data, dict):
        raise YahooError(f"{what} sent an unexpected reply.")
    return data


class YahooSession:
    """Yahoo cookie + crumb session in a dedicated, lazily created client (cookie jar, browser UA, proxies
    from the environment). Crumb endpoints go through get_json(); call close() on shutdown."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic):
        self._transport = transport
        self.clock = clock
        self._client: httpx.AsyncClient | None = None
        self._loop = None
        self._lock: asyncio.Lock | None = None
        self.crumb_value: str | None = None
        self._crumb_at = 0.0
        self._blocked_until = 0.0
        self._blocked_error: YahooError | None = None

    def _ensure(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._loop is not loop:
            # A client (and lock) belongs to one event loop. On a new loop, carry the cookie jar (and so the
            # crumb, which is tied to it) over to a fresh client; after close() start from scratch.
            old = self._client if self._client is not None and not self._client.is_closed else None
            if old is None:
                self.crumb_value = None
            self._client = httpx.AsyncClient(
                cookies=old.cookies if old is not None else None,
                headers={
                    "User-Agent": _ua(),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(12.0, connect=8.0),
                trust_env=True,
                transport=self._transport,
            )
            self._loop = loop
            self._lock = asyncio.Lock()
        return self._client

    def _check_blocked(self) -> None:
        err = self._blocked_error
        if err is not None and self.clock() < self._blocked_until:
            raise type(err)(str(err))

    def _block(self, err: YahooError, seconds: float) -> None:
        self._blocked_error = err
        self._blocked_until = self.clock() + seconds

    async def crumb(self, stale: str | None = None) -> str:
        """The current crumb, fetching one if there is none, it is old, or it equals `stale` (just rejected)."""
        client = self._ensure()
        self._check_blocked()

        def usable() -> bool:
            return bool(self.crumb_value) and self.crumb_value != stale and self.clock() - self._crumb_at < CRUMB_TTL

        if usable():
            return self.crumb_value  # type: ignore[return-value]
        async with self._lock:  # type: ignore[union-attr]
            self._check_blocked()
            if usable():  # another task refreshed it while we waited
                return self.crumb_value  # type: ignore[return-value]
            if stale is not None and self.crumb_value == stale:
                # A crumb is tied to its A3 cookie: getcrumb with the same cookie hands back the same (rejected)
                # crumb, so start a fresh cookie session instead.
                client.cookies.clear()
            try:
                value = await asyncio.wait_for(self._fetch_crumb(client), CRUMB_TIMEOUT)
            except asyncio.TimeoutError:
                err = YahooError("Yahoo Finance didn't answer in time while starting a session.")
                self._block(err, FAIL_BACKOFF)
                raise err from None
            except YahooConsentRequired as exc:
                self._block(exc, CONSENT_BACKOFF)
                raise
            except YahooError as exc:
                self._block(exc, FAIL_BACKOFF)
                raise
            self.crumb_value, self._crumb_at = value, self.clock()
            self._blocked_error = None
            return value

    async def _fetch_crumb(self, client: httpx.AsyncClient) -> str:
        await _LIMITER.wait("fc.yahoo.com")
        try:
            r = await client.get(COOKIE_URL, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
            if _is_consent(r):
                raise YahooConsentRequired(CONSENT_MSG)
            # A 404 here is normal: the response's Set-Cookie (A3) is what matters.
        except httpx.HTTPError:
            pass  # fc.yahoo.com sometimes drops connections; getcrumb may still work with cookies we already hold
        last = "no crumb in reply"
        for base in (Q1, Q2):
            host = httpx.URL(base).host
            await _LIMITER.wait(host)
            try:
                r = await client.get(base + CRUMB_PATH, headers={"Accept": "text/plain, */*"})
            except httpx.TimeoutException:
                last = "timed out"
                continue
            except httpx.HTTPError as exc:
                last = f"network error ({exc.__class__.__name__})"
                continue
            if _is_consent(r):
                raise YahooConsentRequired(CONSENT_MSG)
            if r.status_code == 429:
                _LIMITER.cooldown(host)
                raise YahooRateLimited("Yahoo Finance is limiting requests right now (HTTP 429). Try again in a minute.")
            text = r.text.strip() if r.status_code == 200 else ""
            if _CRUMB_RE.fullmatch(text):
                return text
            last = f"HTTP {r.status_code}"
        raise YahooAuthError(f"Couldn't start a Yahoo Finance session ({last}).")

    async def get_json(self, url: str, params: dict, what: str) -> dict:
        """GET a crumb-protected Yahoo endpoint, refreshing the crumb once if it is rejected."""
        stale: str | None = None
        host = httpx.URL(url).host
        for attempt in (0, 1):
            crumb = await self.crumb(stale=stale)
            client = self._ensure()
            await _LIMITER.wait(host)
            try:
                r = await client.get(url, params={**params, "crumb": crumb})
            except httpx.TimeoutException:
                raise YahooError(f"{what} didn't answer in time. Try again in a moment.") from None
            except httpx.HTTPError as exc:
                raise YahooError(f"Couldn't reach {what} ({exc.__class__.__name__}).") from None
            try:
                _check(r, what)
            except YahooConsentRequired as exc:
                self._block(exc, CONSENT_BACKOFF)  # don't bounce every call through the consent pages
                raise
            except YahooAuthError:
                if attempt == 0:
                    stale = crumb
                    continue
                # A fresh crumb was refused too: back off for a minute, then start over with new cookies.
                client.cookies.clear()
                self.crumb_value = None
                self._block(
                    YahooAuthError(f"Yahoo Finance refused ATLAS's session (HTTP {r.status_code}); it retries in about a minute."),
                    FAIL_BACKOFF,
                )
                raise
            return _json(r, what)
        raise YahooAuthError(f"{what} refused the request.")  # pragma: no cover - loop always returns or raises

    async def close(self) -> None:
        client, self._client = self._client, None
        self.crumb_value = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                pass


_SESSION = YahooSession()


async def shutdown() -> None:
    """Close the Yahoo session's HTTP client (call from the server's shutdown hook)."""
    await _SESSION.close()


async def _public_json(ctx, url: str, params: dict, what: str, crumb_fallback: bool = True) -> dict:
    """GET a Yahoo endpoint that normally needs no crumb, on the shared client. If Yahoo starts demanding
    one (401/403), retry once through the crumb session."""
    host = httpx.URL(url).host
    await _LIMITER.wait(host)
    try:
        r = await ctx.http.get(
            url, params=params, headers={"User-Agent": _ua(), "Accept": "application/json, text/plain, */*"}, timeout=12
        )
    except httpx.TimeoutException:
        raise YahooError(f"{what} didn't answer in time. Try again in a moment.") from None
    except httpx.HTTPError as exc:
        raise YahooError(f"Couldn't reach {what} ({exc.__class__.__name__}).") from None
    try:
        _check(r, what)
    except YahooAuthError:
        if not crumb_fallback:
            raise
        return await _SESSION.get_json(url, params, what)
    return _json(r, what)


# --------------------------------------------------------------------------- number & date helpers
def _num(v) -> float | None:
    """A finite float from a number, numeric string or Yahoo {raw, fmt} pair; None otherwise."""
    if isinstance(v, dict):
        v = v.get("raw")
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.replace(",", "").strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _fmt(v) -> str | None:
    if isinstance(v, dict):
        f = v.get("fmt")
        if isinstance(f, str) and f.strip():
            return f.strip()
    return None


def _px(v, ref=None) -> float | None:
    """Round a price (or a change in price): 2 decimals for normal prices, 4 significant digits below 1."""
    v = _num(v)
    if v is None:
        return None
    r = _num(ref)
    if abs(v) >= 1 or (r is not None and abs(r) >= 1) or v == 0:
        return round(v, 2)
    return float(f"{v:.4g}")


def _r2(v) -> float | None:
    v = _num(v)
    return None if v is None else round(v, 2)


def _pct(fraction) -> float | None:
    v = _num(fraction)
    return None if v is None else round(v * 100, 2)


def _pct_from_fmt(v) -> float | None:
    f = _fmt(v)
    if f and f.endswith("%"):
        return _num(f[:-1])
    return None


def _int(v) -> int | None:
    v = _num(v)
    return None if v is None else int(round(v))


def _big(v) -> str | None:
    v = _num(v)
    if v is None:
        return None
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.2f}{suffix}"
    return f"{v:.0f}"


def _iso(epoch) -> str | None:
    t = _num(epoch)
    if t is None or t <= 0:
        return None
    try:
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return None


def _local_date(epoch, gmt_offset_s: float = 0) -> str | None:
    t = _num(epoch)
    if t is None or t <= 0:
        return None
    try:
        return datetime.fromtimestamp(t + gmt_offset_s, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _pct_change(new, base) -> float | None:
    new, base = _num(new), _num(base)
    if new is None or not base:
        return None
    return round((new / base - 1) * 100, 2)


def _compact(obj):
    """Drop None, empty strings, empty dicts and empty lists (recursively) so results stay small."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = _compact(v)
            if v is None or v == "" or v == {} or v == []:
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [c for c in (_compact(x) for x in obj) if not (c is None or c == "" or c == {})]
    return obj


def _state_label(raw) -> str | None:
    return STATE_LABELS.get(str(raw or "").upper()) if raw else None


def _majority_state(rows) -> str | None:
    states = [r.get("marketState") for r in rows or [] if isinstance(r, dict) and r.get("marketState")]
    if not states:
        return None
    return _state_label(Counter(states).most_common(1)[0][0])


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))


def _eastern_fixed(now_utc: datetime) -> datetime:
    """US Eastern time from the DST rules alone (for Windows installs without tzdata)."""
    y = now_utc.year
    start = datetime.combine(_nth_sunday(y, 3, 2), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_nth_sunday(y, 11, 1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=6)
    offset = -4 if start <= now_utc < end else -5
    return now_utc.astimezone(timezone(timedelta(hours=offset)))


def _eastern(now_utc: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return _eastern_fixed(now_utc)


def clock_market_state(now: datetime | None = None) -> str:
    """Approximate US equity session from the clock (ignores exchange holidays)."""
    et = _eastern(now or datetime.now(timezone.utc))
    if et.weekday() >= 5:
        return "closed"
    minutes = et.hour * 60 + et.minute
    if 570 <= minutes < 960:
        return "open"
    if 240 <= minutes < 570:
        return "pre-market"
    if 960 <= minutes < 1200:
        return "after-hours"
    return "closed"


def _norm_symbol(value) -> str:
    raw = _text(value)
    s = raw.upper().replace(" ", "").lstrip("$")
    if re.fullmatch(r"[A-Z]{1,5}\.[ABC]", s):
        s = s.replace(".", "-")  # BRK.B -> BRK-B: Yahoo writes US share classes with a dash
    if not s or not _SYMBOL_RE.match(s):
        raise ToolError(f"'{clip(raw, 40)}' doesn't look like a ticker symbol. Use lookup_ticker to find it.")
    return s


# --------------------------------------------------------------------------- Yahoo data fetchers
QUOTE_WHAT = "Yahoo Finance's quote service"
SUMMARY_WHAT = "Yahoo Finance's company data service"
SCREENER_WHAT = "Yahoo Finance's market movers screener"
TRENDING_WHAT = "Yahoo Finance's trending tickers"
SEARCH_WHAT = "Yahoo Finance's news search"
CHART_WHAT = "Yahoo Finance's chart service"


async def v7_quotes(symbols: list[str]) -> dict[str, dict]:
    """Batch v7 quote rows by symbol (crumb endpoint; cached per symbol)."""
    out: dict[str, dict] = {}
    missing: list[str] = []
    for s in symbols:
        hit = _cache_get(("v7", s), QUOTE_TTL)
        if hit is not None:
            out[s] = hit
        elif s not in missing:
            missing.append(s)
    for i in range(0, len(missing), 50):
        chunk = missing[i : i + 50]
        data = await _SESSION.get_json(
            QUOTE_URL,
            {"symbols": ",".join(chunk), "formatted": "false", "lang": "en-US", "region": "US"},
            QUOTE_WHAT,
        )
        resp = data.get("quoteResponse") or {}
        rows = resp.get("result")
        if rows is None and resp.get("error"):
            raise YahooError(f"{QUOTE_WHAT} returned an error.")
        for row in rows or []:
            if isinstance(row, dict) and row.get("symbol"):
                sym = str(row["symbol"]).upper()
                _cache_put(("v7", sym), row)
                out[sym] = row
    return out


async def quote_summary(symbol: str) -> dict:
    """v10 quoteSummary modules for one symbol ({raw, fmt} pairs kept)."""
    hit = _cache_get(("v10", symbol), SUMMARY_TTL)
    if hit is not None:
        return hit
    data = await _SESSION.get_json(
        SUMMARY_URL + urlquote(symbol, safe=""),
        {"modules": SUMMARY_MODULES, "lang": "en-US", "region": "US"},
        SUMMARY_WHAT,
    )
    qs = data.get("quoteSummary") or {}
    res = qs.get("result")
    if not res or not isinstance(res[0], dict):
        err = qs.get("error") or {}
        text = f"{err.get('code', '')} {err.get('description', '')}".lower() if isinstance(err, dict) else ""
        if "not found" in text:
            raise YahooNotFound(f"{SUMMARY_WHAT} has no company data for {symbol}.")
        raise YahooError(f"{SUMMARY_WHAT} returned no data for {symbol}.")
    _cache_put(("v10", symbol), res[0])
    return res[0]


async def screener(ctx, kind: str) -> list[dict]:
    """Quote rows from a predefined Yahoo screener (top 25, cached)."""
    scr = SCREENERS[kind]
    hit = _cache_get(("scr", scr), SCREENER_TTL)
    if hit is not None:
        return hit
    params = {"scrIds": scr, "count": 25, "start": 0, "formatted": "false", "lang": "en-US", "region": "US"}
    try:
        data = await _SESSION.get_json(SCREENER_URL, params, SCREENER_WHAT)
    except (YahooAuthError, YahooConsentRequired) as first:
        # Best-effort: the predefined screeners have also been served without a crumb.
        try:
            data = await _public_json(ctx, SCREENER_URL, params, SCREENER_WHAT, crumb_fallback=False)
        except YahooError:
            raise first from None
    rows = parse_screener(data)
    _cache_put(("scr", scr), rows)
    return rows


def parse_screener(data: dict) -> list[dict]:
    fin = data.get("finance") or {}
    results = fin.get("result") or []
    if not results or not isinstance(results[0], dict):
        raise YahooError(f"{SCREENER_WHAT} returned no results.")
    return [q for q in results[0].get("quotes") or [] if isinstance(q, dict) and q.get("symbol")]


async def trending_symbols(ctx, region: str) -> list[str]:
    hit = _cache_get(("trend", region), TRENDING_TTL)
    if hit is not None:
        return hit
    try:
        data = await _public_json(ctx, TRENDING_URL + region, {"count": 25}, TRENDING_WHAT)
    except YahooNotFound:
        raise YahooNotFound(
            f"Yahoo Finance has no trending list for region '{region}'. Try US, GB, CA, AU, IN or DE."
        ) from None
    syms = parse_trending(data)
    _cache_put(("trend", region), syms)
    return syms


def parse_trending(data: dict) -> list[str]:
    fin = data.get("finance") or {}
    results = fin.get("result") or []
    if not results or not isinstance(results[0], dict):
        raise YahooError(f"{TRENDING_WHAT} returned no results.")
    out: list[str] = []
    for q in results[0].get("quotes") or []:
        sym = str(q.get("symbol") or "").strip().upper() if isinstance(q, dict) else ""
        if sym and _SYMBOL_RE.match(sym) and sym not in out:
            out.append(sym)
    return out


def _mover(row: dict, brief: bool = False) -> dict:
    price = row.get("regularMarketPrice")
    item = {
        "symbol": row.get("symbol"),
        "name": clip(_text(row.get("shortName") or row.get("longName") or row.get("displayName")), 60),
        "price": _px(price),
        "change_pct": _r2(row.get("regularMarketChangePercent")),
    }
    if not brief:
        item.update(
            {
                "change": _px(row.get("regularMarketChange"), price),
                "volume": _big(row.get("regularMarketVolume")),
                "avg_volume_3m": _big(row.get("averageDailyVolume3Month")),
                "market_cap": _big(row.get("marketCap")),
                "exchange": row.get("fullExchangeName") or row.get("exchange"),
            }
        )
    return _compact(item)


def _mover_from_chart(c: dict, brief: bool = False) -> dict:
    item = {"symbol": c.get("symbol"), "name": c.get("name"), "price": c.get("price"), "change_pct": c.get("change_pct")}
    if not brief:
        item.update({"change": c.get("change"), "exchange": c.get("exchange")})
    return _compact(item)


async def _quotes_for(ctx, symbols: list[str], brief: bool) -> tuple[list[dict], str | None, list[str]]:
    """Mover-style rows for symbols, in order: v7 batch first, chart endpoint per symbol if that fails."""
    errors: list[str] = []
    try:
        rows = await asyncio.wait_for(v7_quotes(symbols), QUOTES_TIMEOUT)
        items = [_mover(rows[s], brief) for s in symbols if s in rows]
        if items:
            return items, _majority_state(list(rows.values())), errors
    except Exception as exc:
        errors.append(f"quotes: {_msg(exc)}")
    # Fallback: the crumb-less chart endpoint, one request per symbol (capped to stay polite).
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(markets.fetch_quote(ctx.http, s) for s in symbols[:10]), return_exceptions=True),
            FALLBACK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        errors.append("chart quotes: timed out")
        results = []
    items = [_mover_from_chart(r, brief) for r in results if isinstance(r, dict)]
    return items, None, errors


async def daily_bars(ctx, symbol: str, range_: str) -> dict:
    """Daily OHLC bars from the v8 chart endpoint (no crumb), cleaned of null rows."""
    key = ("bars", symbol, range_)
    hit = _cache_get(key, BARS_TTL)
    if hit is not None:
        return hit
    last: Exception | None = None
    data = None
    for base in (Q1, Q2):
        try:
            data = await _public_json(
                ctx,
                base + CHART_PATH + urlquote(symbol, safe=""),
                {"range": range_, "interval": "1d", "includePrePost": "false", "events": "div,split"},
                CHART_WHAT,
                crumb_fallback=False,
            )
            break
        except YahooNotFound:
            raise ToolError(f"Unknown ticker '{symbol}'. Try lookup_ticker.") from None
        except YahooError as exc:  # incl. a 429 on query1: query2 is a separate host, so still worth a try
            last = exc
    if data is None:
        if isinstance(last, YahooRateLimited):
            raise last
        raise ToolError(f"Price history for {symbol} is unavailable right now ({_msg(last) if last else 'no reply'}).")
    chart = data.get("chart") or {}
    results = chart.get("result")
    if not results or not isinstance(results[0], dict):
        raise ToolError(f"No price history for '{symbol}'. Check the ticker with lookup_ticker.")
    bars = parse_chart(results[0])
    _cache_put(key, bars)
    return bars


def parse_chart(res: dict) -> dict:
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    q = (ind.get("quote") or [{}])[0] or {}
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose") or []
    closes, highs, lows = q.get("close") or [], q.get("high") or [], q.get("low") or []
    gmt = _num(meta.get("gmtoffset")) or 0
    out = {"meta": meta, "dates": [], "close": [], "high": [], "low": [], "adjclose": []}
    for i, t in enumerate(ts):
        c = _num(closes[i]) if i < len(closes) else None
        t = _num(t)
        if c is None or c <= 0 or t is None:
            continue
        h = _num(highs[i]) if i < len(highs) else None
        lo = _num(lows[i]) if i < len(lows) else None
        a = _num(adj[i]) if i < len(adj) else None
        try:
            day = datetime.fromtimestamp(t + gmt, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            continue
        if out["dates"] and day <= out["dates"][-1]:
            if day < out["dates"][-1]:
                continue  # out of order: ignore
            # Yahoo often appends the live bar as a second row for the latest session: keep only the newest one
            # so SMA/RSI/returns don't count that day twice.
            for k in ("dates", "close", "high", "low", "adjclose"):
                out[k].pop()
        out["dates"].append(day)
        out["close"].append(c)
        out["high"].append(h if h is not None and h >= c else c)
        out["low"].append(lo if lo is not None and lo <= c else c)
        out["adjclose"].append(a if a is not None and a > 0 else c)
    return out


# --------------------------------------------------------------------------- stock details
def _earnings_from_quote(row: dict, now: float) -> dict:
    gmt = (_num(row.get("gmtOffSetMilliseconds")) or 0) / 1000
    stamps = [
        _num(row.get(k))
        for k in ("earningsTimestampStart", "earningsTimestamp", "earningsTimestampEnd", "earningsCallTimestampStart")
    ]
    stamps = [s for s in stamps if s]
    future = sorted(s for s in stamps if s >= now - 86400)
    if future:
        return {"next_date": _local_date(future[0], gmt), "date_is_estimate": row.get("isEarningsDateEstimate")}
    if stamps:
        return {"last_date": _local_date(max(stamps), gmt)}
    return {}


def _quote_section(row: dict) -> dict:
    price = _num(row.get("regularMarketPrice"))
    prev = _num(row.get("regularMarketPreviousClose"))
    change = _num(row.get("regularMarketChange"))
    pct = _num(row.get("regularMarketChangePercent"))
    if change is None and price is not None and prev:
        change = price - prev
    if pct is None and change is not None and prev:
        pct = change / prev * 100
    vol = _num(row.get("regularMarketVolume"))
    avg = _num(row.get("averageDailyVolume3Month")) or _num(row.get("averageDailyVolume10Day"))
    hi, lo = _num(row.get("fiftyTwoWeekHigh")), _num(row.get("fiftyTwoWeekLow"))
    reg_t = _num(row.get("regularMarketTime"))
    out = {
        "price": _px(price),
        "change": _px(change, price),
        "change_pct": _r2(pct),
        "market_state": _state_label(row.get("marketState")),
        "as_of": _iso(reg_t),
        "open": _px(row.get("regularMarketOpen")),
        "day_low": _px(row.get("regularMarketDayLow")),
        "day_high": _px(row.get("regularMarketDayHigh")),
        "prev_close": _px(prev),
        "volume": _int(vol),
        "avg_volume_3m": _int(avg),
        "volume_to_avg_ratio": round(vol / avg, 2) if vol is not None and avg else None,
        "52w_low": _px(lo),
        "52w_high": _px(hi),
        "pct_from_52w_high": _pct_change(price, hi),
        "pct_above_52w_low": _pct_change(price, lo),
        "50d_avg": _px(row.get("fiftyDayAverage")),
        "200d_avg": _px(row.get("twoHundredDayAverage")),
    }
    for prefix, label in (("preMarket", "pre_market"), ("postMarket", "post_market")):
        p = _num(row.get(prefix + "Price"))
        t = _num(row.get(prefix + "Time"))
        if p is None or (reg_t and t and t < reg_t):
            continue  # missing, or left over from before the latest regular session
        out[label] = {
            "price": _px(p),
            "change": _px(row.get(prefix + "Change"), p),
            "change_pct": _r2(row.get(prefix + "ChangePercent")),
            "as_of": _iso(t),
        }
    return out


def _quote_from_chart(c: dict) -> dict:
    return {
        "price": c.get("price"),
        "change": c.get("change"),
        "change_pct": c.get("change_pct"),
        "as_of": c.get("as_of"),
        "day_low": c.get("day_low"),
        "day_high": c.get("day_high"),
        "52w_low": c.get("52w_low"),
        "52w_high": c.get("52w_high"),
        "pct_from_52w_high": _pct_change(c.get("price"), c.get("52w_high")),
        "pct_above_52w_low": _pct_change(c.get("price"), c.get("52w_low")),
    }


def _ratings(entry: dict) -> dict:
    keys = (("strong_buy", "strongBuy"), ("buy", "buy"), ("hold", "hold"), ("sell", "sell"), ("strong_sell", "strongSell"))
    out = {k: _int(entry.get(src)) for k, src in keys}
    return out if any(v for v in out.values()) else {}


def _date_text(v) -> str | None:
    f = _fmt(v)
    if f and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f):
        return f
    return _local_date(v)


def build_details(symbol: str, row: dict | None, summary: dict | None, chart: dict | None, now: float | None = None) -> dict:
    """Condense a v7 quote row, v10 quoteSummary result and/or a chart fallback into one compact dict."""
    now = time.time() if now is None else now
    row = row or {}
    s = summary or {}
    prof = s.get("assetProfile") or {}
    fin = s.get("financialData") or {}
    ks = s.get("defaultKeyStatistics") or {}
    sd = s.get("summaryDetail") or {}
    cal = s.get("calendarEvents") or {}
    rec = s.get("recommendationTrend") or {}
    trend = s.get("earningsTrend") or {}

    qtype = str(row.get("quoteType") or "").upper() or None
    quote_part = _quote_section(row) if row else (_quote_from_chart(chart) if chart else {})
    if _num(quote_part.get("price")) is None and _num(fin.get("currentPrice")) is not None:
        quote_part = {**quote_part, "price": _px(fin.get("currentPrice"))}  # quoteSummary's (delayed) last price
    price = _num(quote_part.get("price"))

    # Prefer Yahoo's own formatted string ("0.44%"); v7's dividendYield is already in percent, while its
    # trailingAnnualDividendYield is a fraction.
    div_yield = _pct_from_fmt(sd.get("dividendYield"))
    if div_yield is None:
        div_yield = _r2(row.get("dividendYield"))
    if div_yield is None:
        div_yield = _pct(row.get("trailingAnnualDividendYield"))
    valuation = {
        "market_cap": _fmt(sd.get("marketCap")) or _big(row.get("marketCap")),
        "enterprise_value": _fmt(ks.get("enterpriseValue")),
        "pe_trailing": _r2(row.get("trailingPE")) or _r2(sd.get("trailingPE")),
        "pe_forward": _r2(row.get("forwardPE")) or _r2(sd.get("forwardPE")) or _r2(ks.get("forwardPE")),
        "eps_ttm": _r2(row.get("epsTrailingTwelveMonths")) or _r2(ks.get("trailingEps")),
        "eps_forward": _r2(row.get("epsForward")) or _r2(ks.get("forwardEps")),
        "peg": _r2(ks.get("pegRatio")) or _r2(ks.get("trailingPegRatio")),
        "price_to_book": _r2(row.get("priceToBook")) or _r2(ks.get("priceToBook")),
        "price_to_sales": _r2(sd.get("priceToSalesTrailing12Months")),
        "ev_to_ebitda": _r2(ks.get("enterpriseToEbitda")),
        "beta": _r2(sd.get("beta")) or _r2(ks.get("beta")),
        "short_pct_of_float": _pct(ks.get("shortPercentOfFloat")),
    }
    dividend = {
        "annual_rate": _r2(sd.get("dividendRate")) or _r2(row.get("dividendRate")),
        "yield_pct": div_yield,
        "ex_date": _date_text(sd.get("exDividendDate")) or _date_text(cal.get("exDividendDate")),
        "pay_date": _date_text(cal.get("dividendDate")),
        "payout_ratio_pct": _pct(sd.get("payoutRatio")),
    }
    if not dividend["annual_rate"] and not dividend["yield_pct"]:
        dividend = {}

    summary_text = " ".join(str(prof.get("longBusinessSummary") or "").split())
    profile = {
        "sector": prof.get("sectorDisp") or prof.get("sector"),
        "industry": prof.get("industryDisp") or prof.get("industry"),
        "employees": _int(prof.get("fullTimeEmployees")),
        "country": prof.get("country"),
        "website": prof.get("website") if str(prof.get("website") or "").startswith(("http://", "https://")) else None,
        "summary": clip(summary_text, 420),
    }
    d2e = _num(fin.get("debtToEquity"))
    financials = {
        "currency": fin.get("financialCurrency"),
        "revenue_ttm": _fmt(fin.get("totalRevenue")) or _big(fin.get("totalRevenue")),
        "revenue_growth_yoy_pct": _pct(fin.get("revenueGrowth")),
        "earnings_growth_yoy_pct": _pct(fin.get("earningsGrowth")),
        "gross_margin_pct": _pct(fin.get("grossMargins")),
        "operating_margin_pct": _pct(fin.get("operatingMargins")),
        "profit_margin_pct": _pct(fin.get("profitMargins")) if fin.get("profitMargins") else _pct(ks.get("profitMargins")),
        "return_on_equity_pct": _pct(fin.get("returnOnEquity")),
        # Yahoo reports debt/equity as a percentage (209.06 = 2.09x).
        "debt_to_equity": round(d2e / 100, 2) if d2e is not None else None,
        "free_cash_flow": _fmt(fin.get("freeCashflow")) or _big(fin.get("freeCashflow")),
        "operating_cash_flow": _fmt(fin.get("operatingCashflow")) or _big(fin.get("operatingCashflow")),
        "total_cash": _fmt(fin.get("totalCash")) or _big(fin.get("totalCash")),
        "total_debt": _fmt(fin.get("totalDebt")) or _big(fin.get("totalDebt")),
        "current_ratio": _r2(fin.get("currentRatio")),
    }
    if set(_compact(financials)) <= {"currency"}:
        financials = {}

    target = _num(fin.get("targetMeanPrice"))
    consensus = str(fin.get("recommendationKey") or "").strip().lower()
    trend_rows = [t for t in rec.get("trend") or [] if isinstance(t, dict)]
    now_row = next((t for t in trend_rows if t.get("period") == "0m"), trend_rows[0] if trend_rows else {})
    old_row = next((t for t in trend_rows if t.get("period") == "-3m"), {})
    analysts = {
        "consensus": consensus.replace("_", " ") if consensus and consensus != "none" else None,
        "mean_score": _r2(fin.get("recommendationMean")),
        "score_scale": "1 = strong buy ... 5 = strong sell" if _num(fin.get("recommendationMean")) else None,
        "analysts": _int(fin.get("numberOfAnalystOpinions")),
        "target_mean": _px(target),
        "target_median": _px(fin.get("targetMedianPrice")),
        "target_high": _px(fin.get("targetHighPrice")),
        "target_low": _px(fin.get("targetLowPrice")),
        "implied_upside_pct": _pct_change(target, price),
        "ratings_now": _ratings(now_row),
        "ratings_3m_ago": _ratings(old_row),
        "average_rating": row.get("averageAnalystRating"),
    }

    earn = cal.get("earnings") or {}
    dates = [d for d in earn.get("earningsDate") or [] if isinstance(d, dict)]
    earnings = _earnings_from_quote(row, now) if row else {}
    # Right after a report Yahoo can keep the date it just passed in calendarEvents for a while; that one (and
    # the estimates beside it) must not be presented as the *next* report.
    last_raw = _num(dates[-1]) if dates else None
    cal_current = bool(dates) and (last_raw is None or last_raw >= now - 86400)
    if cal_current:
        first = _date_text(dates[0])
        earnings["next_date"] = first or earnings.get("next_date")
        if len(dates) > 1 and _date_text(dates[-1]) != first:
            earnings["window_end"] = _date_text(dates[-1])
        if earn.get("isEarningsDateEstimate") is not None:
            earnings["date_is_estimate"] = bool(earn.get("isEarningsDateEstimate"))
        if earnings.get("next_date"):
            earnings.pop("last_date", None)
    elif dates and not earnings.get("next_date"):
        passed = _date_text(dates[-1])
        if passed and passed > str(earnings.get("last_date") or ""):
            earnings["last_date"] = passed
    cal_ok = cal_current or not dates  # calendar estimates belong to the upcoming report
    trend_by = {t.get("period"): t for t in trend.get("trend") or [] if isinstance(t, dict)}
    q0 = trend_by.get("0q") or {}
    q0e = q0.get("earningsEstimate") or {}
    y0 = (trend_by.get("0y") or {}).get("earningsEstimate") or {}
    earnings.update(
        {
            "eps_estimate": (_r2(earn.get("earningsAverage")) if cal_ok else None) or _r2(q0e.get("avg")),
            "eps_low": (_r2(earn.get("earningsLow")) if cal_ok else None) or _r2(q0e.get("low")),
            "eps_high": (_r2(earn.get("earningsHigh")) if cal_ok else None) or _r2(q0e.get("high")),
            "eps_year_ago": _r2(q0e.get("yearAgoEps")),
            "eps_growth_pct": _pct(q0e.get("growth")) if q0e.get("growth") else _pct(q0.get("growth")),
            "eps_analysts": _int(q0e.get("numberOfAnalysts")),
            "revenue_estimate": (_fmt(earn.get("revenueAverage")) if cal_ok else None)
            or _fmt((q0.get("revenueEstimate") or {}).get("avg")),
            "quarter_ending": q0.get("endDate"),
            "fiscal_year_eps_estimate": _r2(y0.get("avg")),
        }
    )

    fund = {}
    if qtype in ("ETF", "MUTUALFUND") or ks.get("fundFamily") or sd.get("totalAssets"):
        fund = {
            "category": ks.get("category"),
            "family": ks.get("fundFamily"),
            "net_assets": _fmt(sd.get("totalAssets")) or _big(row.get("netAssets")),
            # v7 gives percent (0.0945 = 0.0945%); keep 4 decimals, as expense ratios are tiny
            "expense_ratio_pct": round(_num(row.get("netExpenseRatio")), 4)
            if _num(row.get("netExpenseRatio")) is not None
            else None,
            "yield": _fmt(sd.get("yield")),
            "ytd_return": _fmt(ks.get("ytdReturn")),
            "3y_avg_return": _fmt(ks.get("threeYearAverageReturn")),
            "5y_avg_return": _fmt(ks.get("fiveYearAverageReturn")),
            "nav": _px(sd.get("navPrice")),
        }
    crypto = {}
    if qtype == "CRYPTOCURRENCY":
        crypto = {
            "circulating_supply": _big(row.get("circulatingSupply") or sd.get("circulatingSupply")),
            "volume_24h": _big(row.get("volume24Hr") or sd.get("volume24Hr")),
        }

    return _compact(
        {
            "symbol": symbol,
            "name": clip(_text(row.get("longName") or row.get("shortName") or (chart or {}).get("name")), 120),
            "type": qtype,
            "exchange": row.get("fullExchangeName") or row.get("exchange") or (chart or {}).get("exchange"),
            "currency": row.get("currency") or (chart or {}).get("currency"),
            "quote": quote_part,
            "valuation": valuation,
            "dividend": dividend,
            "profile": profile,
            "financials": financials,
            "analysts": analysts,
            "earnings": earnings,
            "fund": fund,
            "crypto": crypto,
        }
    )


def _details_status(d: dict) -> str:
    q = d.get("quote") or {}
    sym, cur = d.get("symbol", ""), d.get("currency") or ""
    if q.get("price") is None:
        return f"{sym}: price unavailable"
    text = f"{sym} {q['price']} {cur}".strip()
    if q.get("change_pct") is not None:
        text += f", {q['change_pct']:+.2f}%"
    if q.get("market_state"):
        text += f" ({q['market_state']})"
    for key, label in (("pre_market", "pre-market"), ("post_market", "after-hours")):
        ext = q.get(key)
        if ext and ext.get("price") is not None:
            text += f"; {label} {ext['price']}"
            if ext.get("change_pct") is not None:
                text += f" ({ext['change_pct']:+.2f}%)"
            break
    return text


async def _suggest(ctx, query: str) -> str:
    try:
        data = await asyncio.wait_for(
            _public_json(
                ctx, SEARCH_URL, {"q": query, "quotesCount": 3, "newsCount": 0, "enableFuzzyQuery": "true"}, SEARCH_WHAT
            ),
            SUGGEST_TIMEOUT,
        )
    except Exception:
        return ""
    picks = [
        f"{q.get('symbol')} ({clip(_text(q.get('shortname') or q.get('longname')), 40)})"
        for q in data.get("quotes") or []
        if isinstance(q, dict) and q.get("symbol")
    ][:3]
    return f" Did you mean: {', '.join(picks)}?" if picks else ""


@tool(
    "get_stock_details",
    "Deep live snapshot of ONE stock, ETF, index or crypto from Yahoo Finance: price and day change, market "
    "state with pre-market / after-hours price, volume vs 3-month average, market cap, trailing/forward P/E, EPS, "
    "dividend yield and dates, 52-week range, sector/industry/employees and a short business summary, margins, "
    "revenue growth, debt/equity, free cash flow, analyst consensus with mean/high/low price target and implied "
    "upside, buy/hold/sell rating counts, and the next earnings date with the EPS estimate (ETFs get fund facts; "
    "fields that don't apply are left out). Use it when the user asks about a company's valuation, fundamentals, "
    "analyst views, price targets, earnings date, dividend, or 'tell me about <ticker>'. For quick prices of "
    "several tickers use get_market_data; for indicators use get_technicals; for headlines use get_stock_news. "
    "Needs a Yahoo ticker (lookup_ticker finds one from a name). Equity quotes can be delayed ~15 minutes.",
    {"symbol": {"type": "string", "description": "Yahoo ticker, e.g. AAPL, MSFT, BRK-B, SPY, ^GSPC, BTC-USD, RY.TO."}},
    ["symbol"],
    label="Pulling stock details",
    timeout=45,
)
async def get_stock_details(ctx, symbol: str):
    sym = _norm_symbol(symbol)
    errors: list[str] = []

    def note(part: str, exc: BaseException) -> None:
        if isinstance(exc, asyncio.CancelledError):
            raise exc
        text = _msg(exc)
        if not any(e.endswith(text) for e in errors):  # v7 and v10 often fail for the same reason
            errors.append(f"{part}: {text}")

    try:
        qres, sres = await asyncio.wait_for(
            asyncio.gather(v7_quotes([sym]), quote_summary(sym), return_exceptions=True), PRIMARY_TIMEOUT
        )
    except asyncio.TimeoutError:  # leave time for the chart fallback below
        qres = sres = YahooError(f"{QUOTE_WHAT} didn't answer in time.")
    row = None
    if isinstance(qres, BaseException):
        note("quote", qres)
    else:
        row = qres.get(sym)
    summary = None
    if isinstance(sres, BaseException):
        if not isinstance(sres, YahooNotFound):  # no company data is normal for indices, FX and crypto
            note("fundamentals", sres)
    else:
        summary = sres

    chart = None
    source = "Yahoo Finance quote + quoteSummary" if summary is not None else "Yahoo Finance quote"
    if row is None:
        try:
            chart = await asyncio.wait_for(markets.fetch_quote(ctx.http, sym, "1d"), FALLBACK_TIMEOUT)
            source = "Yahoo Finance chart (fallback: detailed quote unavailable)"
        except Exception as exc:
            if summary is None and (not errors or "Unknown ticker" in str(exc)):
                raise ToolError(f"Unknown ticker '{sym}'.{await _suggest(ctx, sym)} Use lookup_ticker to confirm.") from None
            note("chart", exc)
            if summary is None:
                raise ToolError(f"Couldn't get details for {sym}: " + "; ".join(errors)) from None
            source = "Yahoo Finance quoteSummary (live price unavailable)"

    details = build_details(sym, row, summary, chart)
    out = {"status": _details_status(details), **details, "source": source, "note": DELAY_NOTE}
    if errors:
        out["errors"] = errors
    return out


# --------------------------------------------------------------------------- market movers
def _norm_kind(kind) -> str:
    k = re.sub(r"[\s\-]+", "_", _text(kind).lower()) or "gainers"
    k = _KIND_ALIASES.get(k, k)
    if k not in KIND_LABELS:
        raise ToolError("kind must be one of: gainers, losers, most_active, trending.")
    return k


@tool(
    "get_market_movers",
    "Today's biggest US stock movers from Yahoo Finance's screeners: 'gainers' (top % gainers), 'losers' (top % "
    "decliners), 'most_active' (highest volume), or 'trending' (tickers most looked-up on Yahoo right now, which "
    "can include crypto and non-US names; region applies to trending only). Use it for questions like 'what's "
    "moving today', 'top gainers', 'biggest losers', 'most traded stocks', 'what's trending'. Returns symbol, "
    "name, price, % change, volume and market cap. Quotes can be delayed ~15 minutes.",
    {
        "kind": {"type": "string", "enum": list(KIND_LABELS), "description": "Which list. Default 'gainers'."},
        "count": {"type": "integer", "description": "How many, 1-25. Default 10."},
        "region": {"type": "string", "description": "Trending only: 2-letter region, e.g. US, GB, CA, AU, IN, DE. Default US."},
    },
    label="Checking market movers",
    timeout=45,
)
async def get_market_movers(ctx, kind: str = "gainers", count: int = 10, region: str | None = None):
    kind = _norm_kind(kind)
    count = _int_arg(count, 1, 25, 10)
    if kind == "trending":
        reg = (_text(region).upper() or _region())
        if not re.fullmatch(r"[A-Z]{2}", reg):
            reg = "US"
        syms = await trending_symbols(ctx, reg)
        if not syms:
            return {"status": f"No trending tickers listed for {reg} right now.", "kind": kind, "movers": []}
        items, state, errors = await _quotes_for(ctx, syms[:count], brief=False)
        if not items:
            items = [{"symbol": s} for s in syms[:count]]
        out = {
            "status": f"{len(items)} trending tickers on Yahoo Finance ({reg})",
            "kind": kind,
            "region": reg,
            "market_state": state,
            "movers": items,
            "source": "Yahoo Finance trending",
            "errors": errors,
            "note": DELAY_NOTE,
        }
        return _compact(out)
    rows = await screener(ctx, kind)
    items = [_mover(r) for r in rows[:count]]
    state = _majority_state(rows) or clock_market_state()
    label = KIND_LABELS[kind]
    out = _compact(
        {
            "status": f"Top {len(items)} US {label} (market {state})" if items else f"No US {label} listed right now.",
            "kind": kind,
            "market_state": state,
            "movers": items,
            "source": f"Yahoo Finance screener ({SCREENERS[kind]})",
            "note": DELAY_NOTE,
        }
    )
    out.setdefault("movers", [])
    return out


# --------------------------------------------------------------------------- news
def _safe_link(link) -> str | None:
    """An http(s) link short enough to be worth showing (Google News redirect URLs can run to 500+ chars)."""
    link = str(link or "").strip()
    return link if link.startswith(("https://", "http://")) and len(link) <= MAX_LINK else None


def _fit(items: list[dict], budget: int) -> tuple[list[dict], bool]:
    """Keep leading items while their JSON stays within `budget` characters. Returns (kept, trimmed?)."""
    kept: list[dict] = []
    used = 2
    for it in items:
        size = len(json.dumps(it, ensure_ascii=False, default=str)) + 2
        if kept and used + size > budget:
            return kept, True
        kept.append(it)
        used += size
    return kept, False


def parse_news(data: dict, now: datetime | None = None) -> list[dict]:
    """Yahoo search 'news' entries -> [{title, publisher, published, age, link, tickers, type}] newest first."""
    now = now or datetime.now(timezone.utc)
    seen: set[str] = set()
    items: list[tuple[float, dict]] = []
    for n in data.get("news") or []:
        if not isinstance(n, dict):
            continue
        title = clip(_text(n.get("title")), 220)
        if not title:
            continue
        key = str(n.get("uuid") or re.sub(r"\W+", "", title.lower())[:80])
        if key in seen:
            continue
        seen.add(key)
        t = _num(n.get("providerPublishTime")) or 0
        if t > 1e11:  # milliseconds, should Yahoo ever switch
            t /= 1000
        try:
            dt = datetime.fromtimestamp(t, tz=timezone.utc) if t > 0 else None
        except (OverflowError, OSError, ValueError):
            t, dt = 0, None
        link = str(n.get("link") or "")
        kind = str(n.get("type") or "").upper()
        items.append(
            (
                t,
                _compact(
                    {
                        "title": title,
                        "publisher": clip(_text(n.get("publisher")), 60),
                        "published": dt.isoformat(timespec="minutes") if dt else None,
                        "age": relative_time(dt, now) if dt else None,
                        "link": _safe_link(link),
                        "tickers": [str(x) for x in (n.get("relatedTickers") or []) if isinstance(x, str)][:6],
                        "type": kind.lower() if kind and kind != "STORY" else None,
                    }
                ),
            )
        )
    items.sort(key=lambda p: p[0], reverse=True)
    return [i for _, i in items]


@tool(
    "get_stock_news",
    "Latest finance news about a specific ticker or market topic from Yahoo Finance (falls back to Google News): "
    "title, publisher, age, link and related tickers. Use it when the user asks why a stock is moving, for news "
    "on a company/ticker/crypto, or for market topics like 'Fed rate decision' or 'oil prices'. Pass symbol for "
    "a ticker (AAPL, BTC-USD) or query for a free-text topic. For general headlines use get_news instead.",
    {
        "symbol": {"type": "string", "description": "Ticker, e.g. NVDA, TSLA, BTC-USD."},
        "query": {"type": "string", "description": "Free-text finance topic, used when there's no ticker."},
        "count": {"type": "integer", "description": "Number of stories, 1-20. Default 8."},
    },
    label="Reading market news",
    timeout=40,
)
async def get_stock_news(ctx, symbol: str | None = None, query: str | None = None, count: int = 8):
    count = _int_arg(count, 1, 20, 8)
    sym = None
    if _text(symbol):
        try:
            sym = _norm_symbol(symbol)
        except ToolError:  # a topic passed as the symbol: search it as text instead
            query = query or symbol
    q = sym or clip(_text(query), 120)
    if not q:
        raise ToolError("Say which ticker (symbol) or topic (query) to get news for.")
    key = ("news", q.lower(), count)
    hit = _cache_get(key, NEWS_TTL)
    if hit is not None:
        return copy.deepcopy(hit)
    errors: list[str] = []
    articles: list[dict] = []
    source = "Yahoo Finance"
    try:
        data = await asyncio.wait_for(
            _public_json(
                ctx,
                SEARCH_URL,
                {"q": q, "quotesCount": 0, "newsCount": count, "enableFuzzyQuery": "false", "lang": "en-US", "region": _region()},
                SEARCH_WHAT,
            ),
            FALLBACK_TIMEOUT,  # leave time for Google News within the tool's 40 s
        )
        articles = parse_news(data)[:count]
    except Exception as exc:
        errors.append(f"Yahoo Finance: {_msg(exc)}")
    if not articles:
        try:
            # "<ticker> stock" for share tickers (AAPL, BRK-B, VOD.L); crypto, indices, FX and futures as-is.
            is_share = bool(sym) and re.fullmatch(r"[A-Z0-9]{1,6}([.\-][A-Z]{1,2})?", sym or "") is not None
            items = await asyncio.wait_for(
                news_mod.fetch_headlines(ctx, query=f"{sym} stock" if is_share else q, limit=count), FALLBACK_TIMEOUT
            )
            articles = [
                _compact(
                    {
                        "title": clip(_text(i.get("title")), 220),
                        "publisher": clip(_text(i.get("source")), 60),
                        "published": i["published"].isoformat(timespec="minutes") if isinstance(i.get("published"), datetime) else None,
                        "age": relative_time(i["published"]) if isinstance(i.get("published"), datetime) else None,
                        "link": _safe_link(i.get("link")),
                    }
                )
                for i in items
                if isinstance(i, dict) and _text(i.get("title"))
            ]
            source = "Google News (Yahoo Finance news unavailable)" if errors else "Google News"
        except Exception as exc:
            errors.append(f"Google News: {_msg(exc)}")
    if not articles and errors:
        raise ToolError(f"Couldn't load news for {q}: " + "; ".join(errors))
    articles, trimmed = _fit(articles, NEWS_BUDGET)
    out = _compact(
        {
            "query": q,
            "count": len(articles),
            "articles": articles,
            "source": source,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "errors": errors,
            "note": f"Trimmed to {len(articles)} stories to keep the reply short." if trimmed else None,
        }
    )
    out.setdefault("articles", [])
    _cache_put(key, copy.deepcopy(out))
    return out


# --------------------------------------------------------------------------- technical indicators (pure maths)
def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if n > 0 and len(values) >= n else None


def sma_series(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema_series(values: list[float], n: int) -> list[float | None]:
    """Classic EMA: seeded with the SMA of the first n values, then alpha = 2 / (n + 1)."""
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    k = 2 / (n + 1)
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD line (EMA fast - EMA slow), its signal (EMA of the line) and the histogram, plus the aligned series."""
    ef, es = ema_series(values, fast), ema_series(values, slow)
    line = [f - s for f, s in zip(ef, es) if f is not None and s is not None]
    if len(line) < signal:
        return None
    sig = ema_series(line, signal)
    pairs = [(m, s) for m, s in zip(line, sig) if s is not None]
    return {
        "macd": line[-1],
        "signal": sig[-1],
        "histogram": line[-1] - sig[-1],  # type: ignore[operator]
        "diffs": [m - s for m, s in pairs],
    }


def rsi(values: list[float], n: int = 14) -> float | None:
    """Wilder's RSI: simple average of the first n gains/losses, then Wilder smoothing."""
    if n <= 0 or len(values) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(values, values[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, lo in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + lo) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def annualised_volatility(values: list[float], window: int = 30, periods_per_year: int = 252) -> float | None:
    """Sample standard deviation of the last `window` daily log returns, annualised, in percent."""
    if len(values) < window + 1:
        return None
    tail = values[-window - 1 :]
    rets = [math.log(b / a) for a, b in zip(tail, tail[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(periods_per_year) * 100


def last_cross(diffs: list[float | None]) -> tuple[str, int | None] | None:
    """State of a difference series ('above' if the last value > 0) and how many steps ago its sign last flipped."""
    pts = [(i, d) for i, d in enumerate(diffs) if d is not None]
    if not pts:
        return None
    state = "above" if pts[-1][1] > 0 else "below"
    for j in range(len(pts) - 1, 0, -1):
        if (pts[j][1] > 0) != (pts[j - 1][1] > 0):
            return state, pts[-1][0] - pts[j][0]
    return state, None


def swing_levels(highs, lows, price, window: int = 3, lookback: int = 120, tol: float = 0.015, keep: int = 2) -> dict:
    """Simple support/resistance: recent swing lows below the price and swing highs above it, nearest first,
    with levels closer than `tol` merged."""
    n = len(highs)
    start = max(window, n - lookback)
    swing_hi = [highs[i] for i in range(start, n - window) if highs[i] == max(highs[i - window : i + window + 1])]
    swing_lo = [lows[i] for i in range(start, n - window) if lows[i] == min(lows[i - window : i + window + 1])]

    def pick(levels):
        kept: list[float] = []
        for lv in levels:
            if all(abs(lv - k) / k > tol for k in kept):
                kept.append(lv)
            if len(kept) == keep:
                break
        return kept

    resistance = pick(sorted(h for h in swing_hi if h > price * 1.002))
    support = pick(sorted((lo for lo in swing_lo if lo < price * 0.998), reverse=True))
    return {"support": support, "resistance": resistance}


def _months_back(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def period_return(dates: list[date], closes: list[float], target: date, tolerance_days: int = 4) -> float | None:
    """% change from the last close on or before `target` to the latest close."""
    if not dates:
        return None
    idx = None
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] <= target:
            idx = i
            break
    if idx is None:
        if (dates[0] - target).days > tolerance_days:
            return None  # history doesn't reach back that far
        idx = 0
    if idx >= len(closes) - 1:
        return None
    return _pct_change(closes[-1], closes[idx])


def compute_technicals(bars: dict) -> dict:
    """Indicator summary from daily bars (see parse_chart). Pure function: no I/O."""
    closes, highs, lows, dates = bars["close"], bars["high"], bars["low"], bars["dates"]
    adj = bars.get("adjclose") or closes
    meta = bars.get("meta") or {}
    n = len(closes)
    price = closes[-1]
    crypto = str(meta.get("instrumentType") or "").upper() == "CRYPTOCURRENCY"

    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    e12, e26 = ema_series(closes, 12)[-1], ema_series(closes, 26)[-1]
    m = macd(closes)
    r = rsi(closes, 14)
    vol = annualised_volatility(adj, 30, 365 if crypto else 252)
    year = 252 if not crypto else 365
    hi52, lo52 = max(highs[-year:]), min(lows[-year:])
    full_year = n >= (240 if not crypto else 350)

    cross = None
    if n >= 200:
        s50s, s200s = sma_series(closes, 50), sma_series(closes, 200)
        diffs = [a - b if a is not None and b is not None else None for a, b in zip(s50s, s200s)]
        cross = last_cross(diffs)
    macd_cross = last_cross(m["diffs"]) if m else None

    last = dates[-1]
    returns = {
        "1w": period_return(dates, adj, last - timedelta(days=7)),
        "1m": period_return(dates, adj, _months_back(last, 1)),
        "3m": period_return(dates, adj, _months_back(last, 3)),
        "ytd": period_return(dates, adj, date(last.year - 1, 12, 31), tolerance_days=0),
        "1y": period_return(dates, adj, _months_back(last, 12), tolerance_days=7),
    }
    levels = swing_levels(highs, lows, price)

    def p(v):
        return _px(v, price)

    out = {
        "price": p(price),
        "as_of": last.isoformat(),
        "sessions": n,
        "moving_averages": {
            "sma20": p(s20),
            "sma50": p(s50),
            "sma200": p(s200),
            "ema12": p(e12),
            "ema26": p(e26),
            "price_vs_sma50_pct": _pct_change(price, s50),
            "price_vs_sma200_pct": _pct_change(price, s200),
        },
        "macd": {
            "macd": round(m["macd"], 4),
            "signal": round(m["signal"], 4),
            "histogram": round(m["histogram"], 4),
            "state": f"{macd_cross[0]} signal line" if macd_cross else None,
            "crossed_sessions_ago": macd_cross[1] if macd_cross else None,
        }
        if m
        else None,
        "rsi14": round(r, 1) if r is not None else None,
        ("range_52w" if full_year else "range_period"): {
            "high": p(hi52),
            "low": p(lo52),
            "pct_below_high": round((1 - price / hi52) * 100, 2) if hi52 else None,
            "pct_above_low": _pct_change(price, lo52),
        },
        "returns_pct": returns,
        "volatility_30d_annualised_pct": round(vol, 1) if vol is not None else None,
        "trend_cross": {
            "state": "golden cross (50-day above 200-day)" if cross[0] == "above" else "death cross (50-day below 200-day)",
            "last_cross_sessions_ago": cross[1],
            "last_cross_date": dates[-1 - cross[1]].isoformat() if cross[1] is not None else None,
        }
        if cross
        else None,
        "levels": {"support": [p(x) for x in levels["support"]], "resistance": [p(x) for x in levels["resistance"]]},
    }
    out["signals"] = _signals(out, price, s50, s200, r, m, macd_cross, cross, full_year)
    return _compact(out)


def _signals(out, price, s50, s200, r, m, macd_cross, cross, full_year) -> list[str]:
    sig: list[str] = []
    if s50 is not None and s200 is not None:
        a50, a200 = price > s50, price > s200
        if a50 and a200:
            sig.append("Price is above both its 50-day and 200-day moving averages.")
        elif not a50 and not a200:
            sig.append("Price is below both its 50-day and 200-day moving averages.")
        elif a200:
            sig.append("Price is above its 200-day moving average but below its 50-day.")
        else:
            sig.append("Price is above its 50-day moving average but below its 200-day.")
    elif s50 is not None:
        sig.append(f"Price is {'above' if price > s50 else 'below'} its 50-day moving average (not enough history for the 200-day).")
    if cross:
        tc = out.get("trend_cross") or {}
        when = (
            f", last crossing {cross[1]} sessions ago ({tc.get('last_cross_date')})" if cross[1] is not None else ""
        )
        label = "golden-cross" if cross[0] == "above" else "death-cross"
        side = "above" if cross[0] == "above" else "below"
        sig.append(f"The 50-day average is {side} the 200-day ({label} state){when}.")
    if r is not None:
        if r >= 70:
            sig.append(f"RSI(14) is {r:.0f}, above 70: a zone often described as overbought.")
        elif r <= 30:
            sig.append(f"RSI(14) is {r:.0f}, below 30: a zone often described as oversold.")
        else:
            sig.append(f"RSI(14) is {r:.0f}, inside the neutral 30-70 band.")
    if m and macd_cross:
        side = macd_cross[0]
        if macd_cross[1] is not None and macd_cross[1] <= 5:
            ago = "in the latest session" if macd_cross[1] == 0 else f"{macd_cross[1]} sessions ago"
            sig.append(f"MACD crossed {side} its signal line {ago} (momentum turning {'up' if side == 'above' else 'down'}).")
        else:
            sig.append(f"MACD is {side} its signal line (momentum {'positive' if side == 'above' else 'negative'}).")
    rng = out.get("range_52w") or out.get("range_period") or {}
    span = "52-week" if full_year else "period"
    if rng.get("pct_below_high") is not None and rng["pct_below_high"] <= 3:
        sig.append(f"Trading within {max(rng['pct_below_high'], 0):.1f}% of its {span} high.")
    elif rng.get("pct_above_low") is not None and rng["pct_above_low"] <= 5:
        sig.append(f"Trading within {rng['pct_above_low']:.1f}% of its {span} low.")
    vol = out.get("volatility_30d_annualised_pct")
    if vol is not None:
        sig.append(f"30-day annualised volatility is {vol:.0f}%.")
    return sig


@tool(
    "get_technicals",
    "Technical indicators computed locally from Yahoo Finance daily closes for one ticker: SMA 20/50/200, EMA "
    "12/26, MACD with signal and histogram, RSI(14), distance from the 52-week high/low, returns (1w/1m/3m/ytd/1y), "
    "30-day annualised volatility, golden/death-cross state, and nearby support/resistance from recent swing "
    "lows/highs, plus plain-English signal descriptions. Use it when the user asks about a chart, trend, moving "
    "averages, RSI, MACD, momentum, overbought/oversold, support/resistance or volatility. It describes the data "
    "only; never present it as a recommendation to trade. Equity data can be delayed ~15 minutes.",
    {
        "symbol": {"type": "string", "description": "Yahoo ticker, e.g. AAPL, SPY, ^GSPC, BTC-USD."},
        "range": {"type": "string", "enum": TECH_RANGES, "description": "History to analyse. Default '1y' (needed for the 200-day)."},
    },
    ["symbol"],
    label="Crunching technicals",
    timeout=40,
)
async def get_technicals(ctx, symbol: str, range: str = "1y"):  # noqa: A002 - tool argument name
    sym = _norm_symbol(symbol)
    range_ = range if range in TECH_RANGES else "1y"
    bars = await daily_bars(ctx, sym, range_)
    if len(bars["close"]) < 35:
        raise ToolError(f"Not enough daily price history for {sym} to compute technicals ({len(bars['close'])} sessions).")
    t = compute_technicals(bars)
    meta = bars.get("meta") or {}
    ma = t.get("moving_averages") or {}
    bits = [f"{sym} {t.get('price')}"]
    if ma.get("sma50") is not None:
        side50 = "above" if t["price"] > ma["sma50"] else "below"
        if ma.get("sma200") is not None:
            side200 = "above" if t["price"] > ma["sma200"] else "below"
            bits.append(f"{side50} 50d / {side200} 200d SMA" if side50 != side200 else f"{side50} 50d & 200d SMA")
        else:
            bits.append(f"{side50} 50d SMA")
    if t.get("rsi14") is not None:
        bits.append(f"RSI {t['rsi14']:.0f}")
    if (t.get("macd") or {}).get("state"):
        bits.append(f"MACD {t['macd']['state']}")
    return {
        "status": ", ".join(bits),
        "symbol": sym,
        "name": meta.get("longName") or meta.get("shortName") or sym,
        "currency": meta.get("currency"),
        "range": range_,
        **t,
        "note": "Computed by ATLAS from Yahoo daily closes (equities can be delayed ~15 min); descriptive only, not advice.",
    }


# --------------------------------------------------------------------------- yield curve
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_treasury_xml(text: str) -> list[dict]:
    """Rows of the Treasury's daily par yield curve XML feed -> [{date, yields: {tenor: pct}}]."""
    try:
        root = ET.fromstring((text or "").lstrip("﻿ \t\r\n"))
    except ET.ParseError:
        return []
    rows = []
    for el in root.iter():
        if _local(el.tag) != "properties":
            continue
        vals = {_local(c.tag): (c.text or "").strip() for c in el}
        try:
            day = date.fromisoformat(vals.get("NEW_DATE", "")[:10])
        except ValueError:
            continue
        ys = {}
        for tenor, key in TREASURY_TENORS:
            v = _num(vals.get(key))
            if v is not None:
                ys[tenor] = v
        if ys:
            rows.append({"date": day.isoformat(), "yields": ys})
    return rows


async def treasury_curve(ctx, today: date | None = None) -> dict:
    """Latest official daily par yield curve from home.treasury.gov (this month's feed, else last month's)."""
    hit = _cache_get(("treasury",), TREASURY_TTL)
    if hit is not None:
        return hit
    today = today or datetime.now(timezone.utc).date()
    months = [today.strftime("%Y%m"), (today.replace(day=1) - timedelta(days=1)).strftime("%Y%m")]
    host = httpx.URL(TREASURY_URL).host
    last = "no data"
    for ym in months:
        await _LIMITER.wait(host)
        try:
            r = await ctx.http.get(
                TREASURY_URL,
                params={"data": "daily_treasury_yield_curve", "field_tdr_date_value_month": ym},
                headers={"User-Agent": _ua(), "Accept": "application/xml, text/xml, */*"},
                timeout=15,
            )
        except httpx.TimeoutException:
            raise ToolError("The US Treasury site didn't answer in time.") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach the US Treasury site ({exc.__class__.__name__}).") from None
        if r.status_code >= 400:
            if r.status_code == 429:
                _LIMITER.cooldown(host)
            raise ToolError(f"The US Treasury site is unavailable right now (HTTP {r.status_code}).")
        rows = parse_treasury_xml(r.text)
        if rows:
            latest = max(rows, key=lambda x: x["date"])
            _cache_put(("treasury",), latest)
            return latest
        last = f"no rows for {ym}"
    raise ToolError(f"The US Treasury site returned no yield data ({last}).")


def _curve_shape(spread: float | None) -> str:
    if spread is None:
        return "unknown"
    if spread < 0:
        return "inverted"
    if spread < 0.25:
        return "flat"
    return "normal (upward sloping)"


@tool(
    "get_yield_curve",
    "US Treasury yields and the shape of the yield curve: 13-week (^IRX), 5-year (^FVX), 10-year (^TNX) and "
    "30-year (^TYX) yields from Yahoo Finance with today's change in basis points, the 10-year minus 3-month "
    "spread and an inversion flag. Set official=true to also get the U.S. Treasury's official end-of-day par "
    "curve (1M-30Y, adds the 2-year and the 10y-2y spread); it is also used automatically if Yahoo is missing a "
    "tenor. Use it for questions about bond yields, interest rates, the yield curve or recession-signal inversion.",
    {"official": {"type": "boolean", "description": "Also fetch the Treasury's official daily curve. Default false."}},
    label="Reading the yield curve",
    timeout=40,
)
async def get_yield_curve(ctx, official: bool = False):
    official = _flag(official)
    results = await asyncio.gather(*(markets.fetch_quote(ctx.http, s) for _, s in YIELD_TICKERS), return_exceptions=True)
    curve: list[dict] = []
    errors: list[str] = []
    for (tenor, sym), res in zip(YIELD_TICKERS, results):
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res
            errors.append(f"{sym}: {_msg(res)}")
            continue
        y = _num(res.get("price"))
        if y is None:
            errors.append(f"{sym}: no price")
            continue
        scale = 10.0 if y > 25 else 1.0  # the old CBOE convention quoted these indices at 10x the yield
        chg = _num(res.get("change"))
        curve.append(
            {
                "tenor": tenor,
                "symbol": sym,
                "yield_pct": round(y / scale, 3),
                "change_bp": round(chg / scale * 100, 1) if chg is not None else None,
                "as_of": res.get("as_of"),
                "source": "Yahoo Finance",
            }
        )
    treasury = None
    if official or len(curve) < len(YIELD_TICKERS):
        try:
            treasury = await asyncio.wait_for(treasury_curve(ctx), FALLBACK_TIMEOUT)
        except Exception as exc:
            errors.append(f"US Treasury: {_msg(exc)}")
    have = {c["tenor"] for c in curve}
    if treasury:
        for tenor, _sym in YIELD_TICKERS:
            if tenor not in have and tenor in treasury["yields"]:
                curve.append(
                    {"tenor": tenor, "yield_pct": treasury["yields"][tenor], "as_of": treasury["date"], "source": "US Treasury (end of day)"}
                )
    if not curve:
        raise ToolError("Treasury yields are unavailable right now (" + "; ".join(errors[:4]) + ").")
    order = [t for t, _ in YIELD_TICKERS]
    curve.sort(key=lambda c: order.index(c["tenor"]))
    by = {c["tenor"]: c["yield_pct"] for c in curve}
    spread = round(by["10Y"] - by["3M"], 3) if "10Y" in by and "3M" in by else None
    shape = _curve_shape(spread)
    out = {
        "status": (
            f"10Y-3M spread {spread:+.2f} pts ({spread * 100:+.0f} bp): curve {shape}"
            if spread is not None
            else "10Y-3M spread unavailable"
        ),
        "curve": [_compact(c) for c in curve],
        "spread_10y_3m_pct": spread,
        "inverted_10y_3m": spread < 0 if spread is not None else None,
        "shape": shape,
        "spread_30y_5y_pct": round(by["30Y"] - by["5Y"], 3) if "30Y" in by and "5Y" in by else None,
    }
    if official and treasury:
        ys = treasury["yields"]
        s102 = round(ys["10Y"] - ys["2Y"], 3) if "10Y" in ys and "2Y" in ys else None
        out["official"] = {
            "source": "U.S. Department of the Treasury daily par yield curve",
            "date": treasury["date"],
            "yields_pct": ys,
            "spread_10y_2y_pct": s102,
            "inverted_10y_2y": s102 < 0 if s102 is not None else None,
        }
    out["errors"] = errors
    out["note"] = (
        "Yahoo yields are intraday (can lag ~15 min); Treasury figures are end-of-day. ^IRX is the 13-week bill's "
        "discount rate, a little below its bond-equivalent yield."
    )
    return _compact(out)


# --------------------------------------------------------------------------- dashboard panel
def _empty_pulse(errors: list[str]) -> dict:
    return {
        "status": "unavailable",
        "market_state": clock_market_state(),
        "gainers": [],
        "losers": [],
        "most_active": [],
        "trending": [],
        "errors": errors,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    }


async def _pulse(ctx) -> dict:
    errors: list[str] = []

    async def movers(kind: str) -> list[dict]:
        return await screener(ctx, kind)

    async def trending() -> list[dict]:
        syms = await trending_symbols(ctx, _region())
        items, _state, errs = await _quotes_for(ctx, syms[:8], brief=True)
        errors.extend(f"trending {e}" for e in errs)
        return items or [{"symbol": s} for s in syms[:8]]

    kinds = ["gainers", "losers", "most_active"]
    results = await asyncio.gather(*(movers(k) for k in kinds), trending(), return_exceptions=True)
    out = {"gainers": [], "losers": [], "most_active": [], "trending": []}
    rows_seen: list[dict] = []
    for name, res in zip(kinds + ["trending"], results):
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res
            errors.append(f"{name}: {_msg(res)}")
            continue
        if name == "trending":
            out[name] = res[:8]
        else:
            rows_seen.extend(res)
            out[name] = [_mover(r, brief=True) for r in res[:5]]
    filled = sum(1 for k in out if out[k])
    status = "ok" if not errors else ("partial" if filled else "unavailable")
    return {
        "status": status,
        "market_state": _majority_state(rows_seen) or clock_market_state(),
        **out,
        "errors": errors,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    }


# One refresh in flight per event loop, shared by every concurrent caller (several open tabs polling the panel
# shouldn't multiply the requests to Yahoo).
_PULSE_INFLIGHT: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task]" = weakref.WeakKeyDictionary()


async def _pulse_refresh(ctx) -> dict:
    try:
        try:
            result = await asyncio.wait_for(_pulse(ctx), PULSE_TIMEOUT)
        except asyncio.TimeoutError:
            result = _empty_pulse(["market data sources didn't answer in time"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a dashboard panel must never break the page
        result = _empty_pulse([f"market pulse: {_msg(exc)}"])
    _cache_put(("pulse",), copy.deepcopy(result))
    return result


async def market_pulse(ctx) -> dict:
    """Dashboard panel: {status, market_state, gainers[5], losers[5], most_active[5], trending[8], errors, as_of}.
    Each item is {symbol, name, price, change_pct}. Never raises; cached ~60 s."""
    try:
        hit = _cache_get(("pulse",), PULSE_TTL)
        if hit is not None and (hit.get("status") == "ok" or _cache_get(("pulse",), PULSE_PARTIAL_TTL) is not None):
            return copy.deepcopy(hit)
        loop = asyncio.get_running_loop()
        task = _PULSE_INFLIGHT.get(loop)
        if task is None or task.done():
            task = loop.create_task(_pulse_refresh(ctx))
            _PULSE_INFLIGHT[loop] = task
            # drop the entry once done: the task refers to its loop, which would otherwise keep the key alive
            task.add_done_callback(lambda t, lp=loop: _PULSE_INFLIGHT.pop(lp, None) if _PULSE_INFLIGHT.get(lp) is t else None)
        # shield: one caller going away (tab closed) mustn't cancel the refresh the others are waiting on
        return copy.deepcopy(await asyncio.shield(task))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a dashboard panel must never break the page
        return _empty_pulse([f"market pulse: {_msg(exc)}"])
