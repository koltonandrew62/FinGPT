"""Steps for the scheduled email runs, so a run never copies file paths by hand.

Each `plan` step records the start time and prints the exact Robinhood calls (all
read-only) the run must make. The run makes them; large results are saved to files
automatically, and any result shown inline is saved by the run into
/tmp/fingpt_routine/inline/ (one file per result, exact text). The next step then finds
every result written since the plan by itself (the session's tool-results folders and
the inline folder), tells them apart by their content, and carries on. If a needed
result is missing it says which call to repeat and exits with code 2.

Daily plan email (Sunday-Friday, ~8:30 AM ET):
    python3 -m schwab_trader.routine plan
    python3 -m schwab_trader.routine daily [--holdings FILE]    # -> /tmp/fingpt_routine/daily.txt
The first `daily` run may answer MISSING with get_option_chains calls (the listed expiry
dates of the stocks that get a put spread); make them and run `daily` again.

Open check (Monday-Friday, ~9:40 AM ET): the 0DTE QQQ condor decision on the real
open, with real option bid/ask for the condor and for the morning's put spreads:
    python3 -m schwab_trader.routine open-plan
    python3 -m schwab_trader.routine open-legs
    python3 -m schwab_trader.routine open-quotes
    python3 -m schwab_trader.routine open-build                 # -> /tmp/fingpt_routine/open.txt (if there's a trade)
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_DIR

WORK = Path(os.environ.get("FINGPT_ROUTINE_DIR", "/tmp/fingpt_routine"))
INLINE = WORK / "inline"
VIX_ID = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"
ET = "America/New_York"
BANNED = ("Dad", "1.5M", "1,500,000", "1.5 million")
RESULT_DIRS = [str(Path.home() / ".claude" / "projects" / "*" / "*" / "tool-results"),
               str(Path.home() / ".claude" / "projects" / "*" / "*" / "*" / "tool-results")]


# ------------------------------------------------------------------ state
def start(step: str) -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    INLINE.mkdir(exist_ok=True)
    for f in INLINE.glob("*"):
        f.unlink()
    st = {"step": step, "start": time.time()}
    (WORK / f"{step}.json").write_text(json.dumps(st))
    if step == "plan":
        (WORK / "chains_asked.json").unlink(missing_ok=True)
    return st


def listed_expiries(paths) -> dict[str, list[str]]:
    """symbol -> listed option expiry dates, from get_option_chains results (standard
    100-share chains only; adjusted chains after a split are left out)."""
    out: dict[str, set] = {}
    for p in paths:
        t = Path(p).read_text()
        for c in json.loads(t[t.index("{"):])["data"]["chains"]:
            if float(c.get("trade_value_multiplier") or 100) == 100 and c.get("can_open_position", True):
                out.setdefault(c["symbol"], set()).update(c.get("expiration_dates") or [])
    return {k: sorted(v) for k, v in out.items()}


def since(step: str) -> float:
    p = WORK / f"{step}.json"
    if not p.exists():
        raise SystemExit(f"run `python3 -m schwab_trader.routine {step}` first")
    return json.loads(p.read_text())["start"] - 5


def results_since(t0: float) -> list[tuple[str, dict]]:
    """(path, payload) for every saved or inline result written after t0."""
    paths = [p for d in RESULT_DIRS for p in glob.glob(os.path.join(d, "*.txt"))]
    paths += [str(p) for p in INLINE.glob("*")]
    out = []
    for p in sorted(set(paths)):
        if os.path.getmtime(p) < t0:
            continue
        try:
            text = Path(p).read_text()
            payload = json.loads(text[text.index("{"):])
        except Exception:
            continue
        out.append((p, payload))
    return out


def kind_of(payload: dict) -> str:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if isinstance(data, dict) and "instruments" in data:
        return "option_instruments"
    if isinstance(data, dict) and "chains" in data:
        return "option_chains"
    res = data.get("results") if isinstance(data, dict) else None
    if not res or not isinstance(res, list) or not isinstance(res[0], dict):
        return "other"
    r = res[0]
    if "bars" in r:
        if "instrument_id" in r:
            return "index_daily" if r.get("interval") == "day" else "index_intraday"
        if r.get("interval") == "day":
            return "daily"
        return "extended" if r.get("bounds") == "24_5" else "intraday"
    if "report" in r or "eps" in r:
        return "earnings"
    q = r.get("quote") or {}
    if "instrument_id" in q:
        return "option_quotes"
    if "symbol" in q:
        return "equity_quotes"
    return "other"


def collected(t0: float) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p, payload in results_since(t0):
        out.setdefault(kind_of(payload), []).append(p)
    return out


def call(tool: str, **args) -> str:
    return f"{tool} {json.dumps(args)}"


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ daily email
def symbols() -> list[str]:
    from .patterns import ASIA, ASIA_ADRS, SECTORS
    from .pullback import POOLS
    syms = list(POOLS["index+etfs"]) + ["SPY", "QQQ"] + list(SECTORS) + list(ASIA) + list(ASIA_ADRS)
    return list(dict.fromkeys(syms))


def groups(syms, n: int = 10) -> list[list[str]]:
    return [syms[i:i + n] for i in range(0, len(syms), n)]


def needs_daily_refresh(now: pd.Timestamp) -> str:
    """'full', 'recent' or '' (30-minute bars are enough)."""
    spy = DATA_DIR / "SPY.csv"
    if not spy.exists() or sum(1 for _ in open(spy)) < 3000:
        return "full"
    missing = [s for s in symbols() if not (DATA_DIR / f"{s}.csv").exists()]
    if missing:
        return "full"
    last = pd.read_csv(spy, usecols=["date"]).date.iloc[-1]
    stale = np.busday_count(pd.Timestamp(last).date(), now.date()) > 3
    return "recent" if stale or now.weekday() == 6 else ""


def plan(now: pd.Timestamp | None = None) -> list[str]:
    now = now or pd.Timestamp.now(tz="UTC")
    start("plan")
    mode = needs_daily_refresh(now.tz_convert(ET))
    calls = []
    syms = symbols()
    if mode:
        st = "2010-01-01T00:00:00Z" if mode == "full" else iso(now - pd.Timedelta(days=150))
        calls += [call("get_equity_historicals", symbols=g, interval="day", start_time=st) for g in groups(syms)]
        vst = "2005-01-01T00:00:00Z" if mode == "full" else iso(now - pd.Timedelta(days=3 * 365))
        calls.append(call("get_index_historicals", instrument_ids=[VIX_ID], interval="day", start_time=vst))
    calls += [call("get_equity_historicals", symbols=g, interval="30minute", bounds="24_5",
                   start_time=iso(now - pd.Timedelta(days=7))) for g in groups(syms)]
    calls.append(call("get_index_historicals", instrument_ids=[VIX_ID], interval="30minute",   # 30 days: big enough
                      start_time=iso(now - pd.Timedelta(days=30))))                          # to be saved to a file
    calls += [call("get_earnings_calendar", filter="high_market_cap", days=-31),
              call("get_earnings_calendar", filter="high_market_cap", days=31)]
    (WORK / "plan_calls.json").write_text(json.dumps({"mode": mode, "calls": calls, "symbols": syms}))
    return calls


def daily(holdings: str | None = None, text_out: Path | None = None) -> int:
    from . import import_robinhood
    from .daily_report import build
    from .journal import record
    t0 = since("plan")
    info = json.loads((WORK / "plan_calls.json").read_text())
    found = collected(t0)
    # daily bars (and VIX) first, so the email reads them
    for p in found.get("daily", []) + found.get("index_daily", []):
        import_robinhood.merge(import_robinhood.parse(Path(p)))
    ext_paths = found.get("extended", [])
    persist_built(ext_paths, found.get("index_intraday", []))
    have = set()
    for p in ext_paths:
        payload = json.loads(Path(p).read_text()[Path(p).read_text().index("{"):])
        have |= {r["symbol"] for r in payload["data"]["results"] if r}
    missing = [g for g in groups(info["symbols"]) if not set(g) & have]
    cal = merged_calendar(found.get("earnings", []))
    problems = []
    if missing:
        problems.append("repeat get_equity_historicals interval=30minute bounds=24_5 for: " + "; ".join(",".join(g) for g in missing))
    if info["mode"] and not found.get("daily"):
        problems.append("the daily-bar calls (interval=day) are missing")
    if cal is None:
        problems.append("the get_earnings_calendar results are missing")
    if problems:
        print("MISSING:\n  " + "\n  ".join(problems))
        return 2
    asked = set(json.loads((WORK / "chains_asked.json").read_text())) if (WORK / "chains_asked.json").exists() else set()
    chains = {s: [] for s in asked} | listed_expiries(found.get("option_chains", []))
    wanted: set = set()
    subject, text, rows = build(ext_paths, cal, cal, holdings_path=holdings, chains=chains, wanted=wanted)
    need = sorted(wanted - asked)
    if need:
        (WORK / "chains_asked.json").write_text(json.dumps(sorted(asked | set(need))))
        print("MISSING: the listed option expiries of the stocks that get a put spread (each result is small and shown "
              f"inline: save it as its own file in {INLINE}/), then run this step again:")
        print("\n".join("  " + call("get_option_chains", underlying_symbol=s) for s in need))
        return 2
    bad = [w for w in BANNED if w in text or w in subject]
    if bad:
        print(f"FAILED: the email text contains {bad}")
        return 1
    n = record(rows)
    out = text_out or WORK / "daily.txt"
    Path(out).write_text(f"Subject: {subject}\n\n{text}")
    print(f"OK: {out} written ({len(text):,} characters); {n} new recommendation(s) in the journal; "
          f"{len(ext_paths)} 30-minute files, {len(found.get('daily', []))} daily files")
    print(f"Subject: {subject}")
    return 0


def _save(df: pd.DataFrame, path: Path) -> None:
    df = df.sort_index()
    df.index.name = "date"
    df.to_csv(path)


def persist_built(ext_paths, index_paths, now: pd.Timestamp | None = None) -> int:
    """Robinhood's daily bar for a session shows up a day late: add finished sessions
    missing from data/*.csv, built from their regular-hours 30-minute bars (the next
    daily-bar refresh replaces them with the official bars)."""
    from .daily_report import complete_daily, load_extended
    from .data import COLUMNS, load_prices
    now = now or pd.Timestamp.now(tz=ET)
    ext = load_extended(ext_paths)
    syms = [s for s in ext if (DATA_DIR / f"{s}.csv").exists()]
    added = 0
    for s in syms:
        px = load_prices([s])
        done = complete_daily(px, {s: ext[s]}, now)
        new = done["close"].index.difference(px["close"].index)
        if len(new):
            df = pd.DataFrame({c: done[c][s] for c in COLUMNS}).loc[new].dropna(subset=["close"])
            if len(df):
                old = pd.read_csv(DATA_DIR / f"{s}.csv", parse_dates=["date"], index_col="date")
                _save(pd.concat([old, df]), DATA_DIR / f"{s}.csv")
                added += 1
    for p in index_paths:                                    # VIX from 5/30-minute index bars
        payload = json.loads(Path(p).read_text()[Path(p).read_text().index("{"):])
        for r in payload["data"]["results"]:
            path = DATA_DIR / f"{r.get('symbol', 'VIX')}.csv"
            if not path.exists() or not r.get("bars"):
                continue
            old = pd.read_csv(path, parse_dates=["date"], index_col="date")
            b = pd.DataFrame(r["bars"])
            t = pd.to_datetime(b.begins_at, utc=True).dt.tz_convert(ET)
            b = b[(t.dt.time >= dt.time(9, 30)) & (t.dt.time < dt.time(16, 15))].assign(day=t.dt.normalize().dt.tz_localize(None))
            rows = {}
            for day, g in b.groupby("day"):
                if day <= old.index[-1] or pd.Timestamp(f"{day.date()} 16:15", tz=ET) > now or len(g) < 6:
                    continue
                rows[day] = {"open": float(g.open_value.iloc[0]), "high": float(g.high_value.astype(float).max()),
                             "low": float(g.low_value.astype(float).min()), "close": float(g.close_value.iloc[-1]), "volume": 0}
            if rows:
                _save(pd.concat([old, pd.DataFrame(rows).T[COLUMNS]]), path)
                added += 1
    return added


def merged_calendar(paths) -> str | None:
    if not paths:
        return None
    rows = []
    for p in paths:
        t = Path(p).read_text()
        rows += json.loads(t[t.index("{"):])["data"]["results"]
    out = WORK / "earnings_merged.json"
    out.write_text(json.dumps({"data": {"results": rows}}))
    return str(out)


# ------------------------------------------------------------ 9:40 check
def open_plan(now: pd.Timestamp | None = None) -> list[str]:
    now = now or pd.Timestamp.now(tz="UTC")
    start("open-plan")
    # windows long enough that both results are saved to files (today's first bars are what's used)
    return [call("get_equity_historicals", symbols=["QQQ", "SPY"], interval="5minute", bounds="regular",
                 start_time=iso(now - pd.Timedelta(days=10))),
            call("get_index_historicals", instrument_ids=[VIX_ID], interval="30minute",
                 start_time=iso(now - pd.Timedelta(days=30)))]


def _today_opens(found) -> dict:
    today = pd.Timestamp.now(tz=ET).date()
    out = {}
    for p in found.get("intraday", []) + found.get("index_intraday", []):              # 5-minute QQQ/SPY, 30-minute VIX
        payload = json.loads(Path(p).read_text()[Path(p).read_text().index("{"):])
        for r in payload["data"]["results"]:
            name = "VIX" if r.get("instrument_id") == VIX_ID else r.get("symbol", "index")
            day = [b for b in r.get("bars", []) if pd.Timestamp(b["begins_at"]).tz_convert(ET).date() == today]
            bars = [b for b in day if pd.Timestamp(b["begins_at"]).tz_convert(ET).time() >= dt.time(9, 30)]
            if bars:
                key = "open_price" if "open_price" in bars[0] else "open_value"
                out[name] = float(bars[0][key])
            elif name == "VIX" and len(day) >= 2:
                # a bar shows up only once it has finished, so at 9:40 the 9:30-10:00 VIX bar isn't
                # there yet; the bar that ends at 9:30 closes at the 9:30 value
                last, prev = (pd.Timestamp(b["begins_at"]) for b in (day[-1], day[-2]))
                if (last + (last - prev)).tz_convert(ET).time() == dt.time(9, 30):
                    out[name] = float(day[-1]["close_value"])
    return out


def condor_credit(spot: float, vix_open: float, mult: float) -> tuple[tuple, float, float]:
    """(strikes, credit per share after the spread, max loss per set) at a volatility
    multiplier on VIX (research: fair 0.81, mid 1.06)."""
    from .condor_research import condor_strikes
    from .options_backtest import bs_price, half_spread, implied_vol
    from .vol_alert import CONDOR_K, CONDOR_W
    sc, lc, sp, lp = condor_strikes(spot, CONDOR_K, CONDOR_W)
    t, atm = 1 / 252, vix_open / 100 * mult
    mid = {k: bs_price(spot, K, t, implied_vol(atm, spot, K, t), 0.04, c)
           for k, K, c in (("sc", sc, True), ("lc", lc, True), ("sp", sp, False), ("lp", lp, False))}
    credit = sum((-1 if k[0] == "l" else 1) * (v + (half_spread(v) if k[0] == "l" else -half_spread(v))) for k, v in mid.items())
    width = max(lc - sc, sp - lp)
    return (sc, lc, sp, lp), credit, (width - credit) * 100 + 6 * 0.65


def open_legs() -> int:
    from .journal import load
    from .vol_alert import evaluate_today, history_local
    t0 = since("open-plan")
    opens = _today_opens(collected(t0))
    if not {"QQQ", "SPY", "VIX"} <= set(opens):
        print(f"MISSING: today's open for {sorted({'QQQ', 'SPY', 'VIX'} - set(opens))} - repeat the open-plan calls "
              "(after 9:31 AM ET) and save any inline result into /tmp/fingpt_routine/inline/")
        return 2
    qqq, spy, vix = history_local()
    today = pd.Timestamp(pd.Timestamp.now(tz=ET).date())
    for df, s in ((qqq, "QQQ"), (spy, "SPY")):
        df.loc[today] = {"open": opens[s], "high": np.nan, "low": np.nan, "close": np.nan, "volume": np.nan}
    vix.loc[today] = {"open": opens["VIX"], "high": np.nan, "low": np.nan, "close": np.nan, "volume": 0}
    verdict = evaluate_today(qqq, spy, vix)
    legs = []
    state = {"opens": opens, "verdict": verdict, "legs": []}
    if verdict["calm"]:
        (sc, lc, sp, lp), credit, max_loss = condor_credit(opens["QQQ"], opens["VIX"], 1.06)
        _, fair, _ = condor_credit(opens["QQQ"], opens["VIX"], 0.81)
        state["condor"] = {"strikes": [sc, lc, sp, lp], "model_credit": credit, "fair_credit": fair, "model_max_loss": max_loss}
        exp = today.date().isoformat()
        legs += [("QQQ", exp, "call", sc, "sell"), ("QQQ", exp, "call", lc, "buy"),
                 ("QQQ", exp, "put", sp, "sell"), ("QQQ", exp, "put", lp, "buy")]
    j = load()
    for r in j[(j.plan_date == today.date().isoformat()) & j.kind.str.endswith("put_spread") & (j.status == "open")].itertuples():
        lg = json.loads(r.legs)
        for ks, kl in [(lg["short_put"], lg["long_put"])] + ([tuple(lg["alt"])] if lg.get("alt") else []):
            for k, side in ((ks, "sell"), (kl, "buy")):         # the wider strike steps too, in case these aren't listed
                if (r.symbol, lg["expiry"], "put", k, side) not in legs:
                    legs.append((r.symbol, lg["expiry"], "put", k, side))
    state["legs"] = legs
    (WORK / "open_state.json").write_text(json.dumps(state, default=str))
    print(f"QQQ opened {opens['QQQ']:.2f}, VIX {opens['VIX']:.2f}: big-move probability {verdict['prob']:.0%} "
          f"(calm if <= {verdict['threshold']:.0%}) -> {'SELL CONDOR' if verdict['calm'] else 'no condor'}")
    if not legs:
        print("No option legs to price: run open-build next (no calls needed).")
        return 0
    print("CALLS (one per leg):")
    for sym, exp, typ, k, _ in legs:
        print("  " + call("get_option_instruments", chain_symbol=sym, expiration_dates=exp, type=typ,
                          strike_price=f"{float(k):.4f}"))
    return 0


def _instruments(found) -> dict:
    out = {}
    for p in found.get("option_instruments", []):
        payload = json.loads(Path(p).read_text()[Path(p).read_text().index("{"):])
        for i in payload["data"]["instruments"]:
            out[(i["chain_symbol"], i["expiration_date"], i["type"], round(float(i["strike_price"]), 2))] = i["id"]
    return out


def open_quotes() -> int:
    t0 = since("open-plan")
    st = json.loads((WORK / "open_state.json").read_text())
    ids = _instruments(collected(t0))
    want, missing = [], []
    for sym, exp, typ, k, _ in st["legs"]:
        i = ids.get((sym, exp, typ, round(float(k), 2)))
        (want if i else missing).append(i or f"{sym} {exp} {typ} {k}")
    if missing:
        print("NOT LISTED (these strikes/expiries don't exist; the email will say so): " + "; ".join(missing))
    if not want:
        print("No listed legs: run open-build next.")
        return 0
    print("CALLS:")
    for g in groups(want, 20):
        print("  " + call("get_option_quotes", instrument_ids=g))
    return 0


def spread_check(j: pd.DataFrame, i, r, leg_quote) -> tuple[str, bool]:
    """One of the morning's put spreads against the real quotes: the morning's strikes, or the
    wider strike steps when those aren't listed. A spread not listed, or whose credit is under
    the backtest's floor, is marked skipped in the journal (it never counts as open)."""
    from .daily_report import put_spread_credit
    from .options_backtest import COMMISSION, MULTIPLIER
    from .sizing import money
    lg = json.loads(r.legs)
    exp = lg["expiry"]
    morning = (lg["short_put"], lg["long_put"])
    head = f"  {r.symbol} {exp} ${morning[0]:g}/${morning[1]:g} put spread"
    for ks, kl in [morning] + ([tuple(lg["alt"])] if lg.get("alt") else []):
        qs, ql = leg_quote(r.symbol, exp, "put", ks), leg_quote(r.symbol, exp, "put", kl)
        if qs and ql and qs[1] > 0 and ql[1] > 0:
            break
    else:
        j.at[i, "status"], j.at[i, "reason"] = "skipped", "not listed"
        return f"{head}: not listed - skip it (no listed strikes near these for {exp})", False
    mid = (qs[0] + qs[1]) / 2 - (ql[0] + ql[1]) / 2
    floor, n, note = lg.get("min_credit"), int(lg.get("contracts") or 0), ""
    if (ks, kl) != morning:                       # re-price the listed strikes the way the morning email did
        m = lg.get("model") or {}
        if m:
            credit = put_spread_credit(m["price"], ks, kl, m["t"], m["iv"])
            max_loss = (ks - kl - credit) * MULTIPLIER + 4 * COMMISSION
            n = int(m["risk"] // max_loss) if max_loss > 0 else 0
            floor = round(max(put_spread_credit(m["price"], ks, kl, m["t"], m["iv_fair"]), 0.01), 2) if m.get("iv_fair") else None
            j.at[i, "model_credit"], j.at[i, "max_loss"] = credit, max_loss
        lg.update({"short_put": ks, "long_put": kl, "contracts": n, "min_credit": floor, "morning": list(morning)})
        j.at[i, "legs"] = json.dumps(lg, sort_keys=True)
        note = f" (the morning's ${morning[0]:g}/${morning[1]:g} strikes aren't listed; these are)"
    j.at[i, "real_credit"] = mid
    j.at[i, "real_quotes"] = json.dumps({"short": qs, "long": ql})
    if floor is not None and mid < floor:
        j.at[i, "status"], j.at[i, "reason"] = "skipped", "credit below floor"
        return (f"  {r.symbol} {exp} ${ks:g}/${kl:g} put spread{note}: real mid credit ${mid:.2f} vs estimate "
                f"${float(j.at[i, 'model_credit']):.2f}; below ${floor:.2f} (what the backtest needed) - skip"), True
    risk = (ks - kl - mid) * MULTIPLIER + 4 * COMMISSION
    return (f"  {r.symbol} {exp} ${ks:g}/${kl:g} put spread{note}: real mid credit ${mid:.2f} vs estimate "
            f"${float(j.at[i, 'model_credit']):.2f}; take it: SELL the ${ks:g} put / BUY the ${kl:g} put, {n} spreads at "
            f"${mid:.2f} or better (max loss {money(n * risk)})"), True


def open_build(text_out: Path | None = None) -> int:
    from .journal import load, record, save
    from .sizing import money
    t0 = since("open-plan")
    st = json.loads((WORK / "open_state.json").read_text())
    found = collected(t0)
    ids = _instruments(found)
    quotes = {}
    for p in found.get("option_quotes", []):
        payload = json.loads(Path(p).read_text()[Path(p).read_text().index("{"):])
        for r in payload["data"]["results"]:
            q = r["quote"]
            quotes[q["instrument_id"]] = (float(q.get("bid_price") or 0), float(q.get("ask_price") or 0))
    def leg_quote(sym, exp, typ, k):
        i = ids.get((sym, exp, typ, round(float(k), 2)))
        return quotes.get(i) if i else None
    today = pd.Timestamp.now(tz=ET).date()
    v, opens = st["verdict"], st["opens"]
    lines = [f"OPEN CHECK {today:%a %b %d} ({pd.Timestamp.now(tz=ET):%-I:%M %p} ET) - QQQ opened ${opens['QQQ']:,.2f}, "
             f"VIX {opens['VIX']:.2f}"]
    actionable = False
    if "condor" in st:
        c = st["condor"]
        sc, lc, sp, lp = c["strikes"]
        q = [leg_quote("QQQ", today.isoformat(), t, k) for t, k in (("call", sc), ("call", lc), ("put", sp), ("put", lp))]
        lines.append(f"0DTE QQQ: big-move probability {v['prob']:.0%} (calm if <= {v['threshold']:.0%}) -> CALM: sell the iron condor")
        lines.append(f"  SELL {sc} call / BUY {lc} call, SELL {sp} put / BUY {lp} put, all expiring today")
        if all(q):
            natural = q[0][0] - q[1][1] + q[2][0] - q[3][1]
            mid = sum((b + a) / 2 * (1 if i in (0, 2) else -1) for i, (b, a) in enumerate(q))
            width = max(lc - sc, sp - lp)
            max_loss = (width - mid) * 100 + 6 * 0.65
            n = int(1_500_000 * 0.02 // max_loss) if max_loss > 0 else 0
            lines.append(f"  real prices now: mid credit ${mid:.2f}/share (bid/ask: ${natural:.2f} if you sell at the bid), "
                         f"model estimate ${c['model_credit']:.2f}; lowest credit the backtest still made money at: ${c['fair_credit']:.2f}")
            if mid >= c["fair_credit"]:
                lines.append(f"  -> {n} sets with a limit order for the whole condor at ${mid:.2f} or better: max loss "
                             f"{money(n * max_loss)} (2% of the account), credit ~{money(n * mid * 100)}")
                actionable = True
            else:
                lines.append("  -> credit too thin today: skip (below what the backtest needed)")
            real, rq = mid, {"sc": q[0], "lc": q[1], "sp": q[2], "lp": q[3]}
        else:
            lines.append("  real quotes unavailable for some legs: use the morning email's estimate and a limit at the mid price")
            n, real, rq = int(1_500_000 * 0.02 // c["model_max_loss"]), np.nan, {}
            actionable = True
        record([{"plan_date": today.isoformat(), "kind": "qqq_condor", "symbol": "QQQ", "ref_price": opens["QQQ"],
                 "legs": {"expiry": today.isoformat(), "short_call": sc, "long_call": lc, "short_put": sp, "long_put": lp,
                          "contracts": n},
                 "model_credit": c["model_credit"], "max_loss": c["model_max_loss"], "real_credit": real,
                 "real_quotes": json.dumps(rq), "rule": "calm-day 0DTE condor, 1% strikes"}])
        lines.append("  Close anything still in the money by 3:50 PM ET; otherwise let it expire.")
    else:
        lines.append(f"0DTE QQQ: big-move probability {v['prob']:.0%} (calm only if <= {v['threshold']:.0%}) -> no condor today")
    j = load()
    rows = j[(j.plan_date == today.isoformat()) & j.kind.str.endswith("put_spread") & (j.status == "open")]
    if len(rows):
        lines.append("PUT SPREADS FROM THIS MORNING'S EMAIL - real prices at the open:")
    for i, r in rows.iterrows():
        msg, actionable_row = spread_check(j, i, r, leg_quote)
        lines.append(msg)
        actionable |= actionable_row
    save(j)
    text = "\n".join(lines)
    print(text)
    if actionable:
        out = text_out or WORK / "open.txt"
        Path(out).write_text(f"Subject: Open check {today:%a %b %d}: "
                             + ("condor + spreads" if "condor" in st and len(rows) else "condor" if "condor" in st else "spreads")
                             + f"\n\n{text}")
        print(f"OK: {out} written - send it")
    else:
        print("NOTHING TO SEND today")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 0
    step, rest = argv[0], argv[1:]
    opt = lambda name: rest[rest.index(name) + 1] if name in rest else None
    if step == "plan":
        calls = plan()
        print(f"CALLS ({len(calls)}; make every one, up to 10 at a time; save any inline result into {INLINE}/):")
        print("\n".join("  " + c for c in calls))
        return 0
    if step == "daily":
        return daily(opt("--holdings"), Path(opt("--text-out")) if opt("--text-out") else None)
    if step == "open-plan":
        calls = open_plan()
        print(f"CALLS (save each inline result as its own file in {INLINE}/):")
        print("\n".join("  " + c for c in calls))
        return 0
    if step == "open-legs":
        return open_legs()
    if step == "open-quotes":
        return open_quotes()
    if step == "open-build":
        return open_build()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
