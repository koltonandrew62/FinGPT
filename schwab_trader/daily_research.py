"""Research behind Dad's daily email ($1.5M margin account, highest win rate first).

1. EPS picks. Earnings reports for the 59 stocks from Robinhood's earnings calendar
   (report date, am/pm, EPS estimate and actual) from October 2017; earlier years are
   the highest-volume day within 10 days of the same calendar date 1-16 years before a
   known report (checked against the known reports; no EPS numbers for those).
   Variants: EPS beat by 0%/5%+ or ignore EPS; buy the reaction day's open when the
   stock gaps up (the gap is visible pre-market) or the next open after a reaction day
   that closed above its open; hold 1-20 sessions.
2. Options on the picks and on QQQ: sell put credit spreads or buy calls on each
   dip-buy signal (the regular stock picks), each EPS pick, QQQ dips and QQQ every
   week in an uptrend. Priced with Black-Scholes; implied volatility = a jump-
   trimmed 40-day realized volatility x a multiplier: "fair" (no volatility premium,
   the worst case for a seller), "rich" (the premium VIX carries over SPY's realized
   moves) and "mid" (halfway). Bid/ask max($0.02, 3% of the price) per leg and
   $0.65 per contract per leg each way.
3. Asia -> US: how US sectors open with the Asian session and whether the day goes on
   or reverses after big Asian moves.
4. Patterns: forward win rates of chart patterns, and the "most similar past
   mornings" forecaster checked walk-forward.

Same protocol as the rest of the project: every choice is made on 2011-02 .. 2021-10
by a rule written down before running, then tested once on 2021-10 .. 2026-10. The
stock list is October 2021's largest companies, so 2011-2021 has hindsight.

    python -m schwab_trader.daily_research [EPS_JSON]     # everything (about 25 minutes)
    python -m schwab_trader.daily_research --combined     # only the whole-plan backtest
    python -m schwab_trader.daily_research --revalidate   # refresh the options rules and the whole plan on the
                                                          # current stock list and the email's share rules
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .backtest import performance
from .config import DATA_DIR, RESULTS_DIR, SPLITS, Costs
from .data import load_prices
from .options_backtest import COMMISSION, MULTIPLIER, bs_price, fair_multipliers, implied_vol, rate
from .patterns import (ASIA, FEATURES, SECTORS, analog_forecast, asia_impact, asia_overnight, forward_returns,
                       market_features, market_outcomes, pattern_frames, pattern_stats)
from .pullback import INDEX, POOLS, PullbackConfig, indicators, signals
from .strategy import split_factor
from .universe import sp500_mask, was_member

OUT = RESULTS_DIR / "daily"
DESIGN = ("2011-02-01", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")
STOCKS = INDEX                    # S&P 500 + Nasdaq-100 + Dow 30
CASH = 1_500_000.0
SLIP = Costs().slippage_bps / 1e4
# the regular stock picks: pullback.py pass 2's selected rule (70% / 66% of trades won)
DIP = PullbackConfig(pool="index+etfs", momentum_top=10, entry_rsi=10, exit_rule="rsi70", max_hold=20, max_positions=3)


def in_window(idx, w) -> np.ndarray:
    return (idx >= pd.Timestamp(w[0])) & (idx <= pd.Timestamp(w[1]))


# ================================================================ 1. EPS picks
def load_eps(path) -> pd.DataFrame:
    rows = []
    for r in json.loads(open(path).read()):
        eps, rep = r.get("eps") or {}, r.get("report") or {}
        if not rep.get("date"):
            continue
        num = lambda x: float(x) if x not in (None, "") else np.nan
        rows.append({"symbol": r["symbol"], "date": pd.Timestamp(rep["date"]), "timing": rep.get("timing") or "am",
                     "estimate": num(eps.get("estimate")), "actual": num(eps.get("actual"))})
    return pd.DataFrame(rows).drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])


def merge_eps_history(paths, out=None, symbols=None) -> int:
    """Merge saved get_earnings_calendar results (and the existing history) into
    results/daily/eps_history.json, keeping one row per company and report date."""
    out = out or OUT / "eps_history.json"
    symbols = set(symbols or STOCKS)
    rows = json.loads(out.read_text()) if out.exists() else []
    for p in paths:
        t = open(p).read()
        payload = json.loads(t[t.index("{"):] if t.lstrip()[:1] != "[" else t)
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        rows += data.get("results", []) if isinstance(data, dict) else data
    keep = {}
    for r in rows:
        d = (r.get("report") or {}).get("date")
        if d and r.get("symbol") in symbols:
            k = (r["symbol"], d)
            if k not in keep or (r.get("eps") or {}).get("actual") not in (None, ""):
                keep[k] = r
    merged = sorted(keep.values(), key=lambda r: (r["symbol"], r["report"]["date"]))
    out.write_text(json.dumps(merged, separators=(",", ":")))
    return len(merged)


def surprise(actual: float, estimate: float) -> float:
    """EPS surprise as a fraction of the estimate's size (works for losses too)."""
    return (actual - estimate) / abs(estimate) if np.isfinite(actual) and np.isfinite(estimate) and estimate else np.nan


def reaction_day(index: pd.DatetimeIndex, date, timing: str):
    """First session that trades on the news: the report day for 'am', the next for 'pm'."""
    day = pd.Timestamp(date) + pd.Timedelta(days=1 if timing == "pm" else 0)
    pos = index.searchsorted(day)
    return index[pos] if pos < len(index) else None


def volume_ratio(px: dict, symbols) -> pd.DataFrame:
    v = px["volume"][list(symbols)]
    return v / v.rolling(50, min_periods=20).median().shift(1)


def project_events(ratio: pd.Series, anchors, years: int = 16, window: int = 10) -> list:
    """Highest-volume session within `window` calendar days of each anchor date moved
    back 1..`years` years."""
    found = []
    for d0 in anchors:
        for k in range(1, years + 1):
            center = d0 - pd.DateOffset(years=k)
            win = ratio.loc[center - pd.Timedelta(days=window):center + pd.Timedelta(days=window)].dropna()
            if len(win):
                found.append(win.idxmax())
    return found


def earnings_events(px: dict, eps: pd.DataFrame, symbols=STOCKS) -> tuple[pd.DataFrame, dict]:
    """(events, validation). events: symbol, day (reaction session), known (from
    Robinhood), surprise (actual/estimate - 1 when known)."""
    ratio = volume_ratio(px, symbols)
    idx = px["close"].index
    rows, hits, checked = [], 0, 0
    for s in symbols:
        rep = eps[(eps.symbol == s) & eps.actual.notna()]
        known = {}
        for r in rep.itertuples():
            d = reaction_day(idx, r.date, r.timing)
            if d is not None:
                known[d] = surprise(r.actual, r.estimate)
        if not known:
            continue
        # validation: project each known report from the one a year later
        for d in known:
            later = [k for k in known if 330 <= (k - d).days <= 400]
            if later:
                checked += 1
                proj = project_events(ratio[s], later[:1], years=1)
                hits += bool(proj) and abs(idx.get_loc(proj[0]) - idx.get_loc(d)) <= 1
        found = sorted(set(project_events(ratio[s], list(known))) - set(known))
        first_known = min(known)
        cands = [(d, ratio[s].get(d, np.nan)) for d in found if d < first_known - pd.Timedelta(days=40)]
        cands.sort(key=lambda x: -np.nan_to_num(x[1]))
        kept = []
        for d, _ in cands:                       # one event per quarter: drop near-duplicates
            if all(abs((d - k).days) > 40 for k in kept):
                kept.append(d)
        rows += [{"symbol": s, "day": d, "known": False, "surprise": np.nan} for d in kept]
        rows += [{"symbol": s, "day": d, "known": True, "surprise": v} for d, v in known.items()]
    ev = pd.DataFrame(rows).sort_values(["day", "symbol"]).reset_index(drop=True)
    return ev, {"checked": checked, "found_within_1_day": hits, "hit_rate": hits / checked if checked else np.nan}


@dataclass(frozen=True)
class EpsConfig:
    entry: str = "gap_open"     # gap_open: buy the reaction day's open | next_open: buy the next open
                                # after a reaction day that closed above its open
    min_gap: float = 0.0        # reaction-day opening gap at least this (-1 = any)
    hold: int = 5               # sessions held; sold at the open `hold` sessions after buying
    trend: bool = False         # only when the stock closed above its 50-day average before the report
    min_surprise: float | None = None   # EPS actual/estimate - 1 at least this (None = ignore EPS)

    def label(self) -> str:
        gap = "any gap" if self.min_gap <= -1 else f"gap>={self.min_gap:.0%}"
        eps = "" if self.min_surprise is None else f"EPS beat>={self.min_surprise:.0%} "
        return f"{eps}{self.entry} {gap} hold{self.hold}{' above-50d' if self.trend else ''}"


def eps_trades(px: dict, events: pd.DataFrame, cfg: EpsConfig) -> pd.DataFrame:
    c, o = px["close"], px["open"]
    idx = c.index
    sma50 = c.rolling(50).mean()
    rows = []
    for e in events.itertuples():
        d = idx.get_loc(e.day)
        if d < 1 or (cfg.min_surprise is not None and not (e.surprise >= cfg.min_surprise)):
            continue
        gap = o.at[e.day, e.symbol] / c.iat[d - 1, c.columns.get_loc(e.symbol)] - 1
        if not np.isfinite(gap) or gap < cfg.min_gap:
            continue
        if cfg.trend and not c.iat[d - 1, c.columns.get_loc(e.symbol)] > sma50.iat[d - 1, c.columns.get_loc(e.symbol)]:
            continue
        if cfg.entry == "next_open":
            if not c.at[e.day, e.symbol] > o.at[e.day, e.symbol]:
                continue
            ent = d + 1
        else:
            ent = d
        ex = ent + cfg.hold
        if ex >= len(idx):
            continue
        buy = o.iat[ent, o.columns.get_loc(e.symbol)] * (1 + SLIP)
        sell = o.iat[ex, o.columns.get_loc(e.symbol)] * (1 - SLIP)
        if np.isfinite(buy) and np.isfinite(sell):
            rows.append({"symbol": e.symbol, "event": e.day, "entry": idx[ent], "exit": idx[ex], "gap": gap,
                         "surprise": e.surprise, "return": sell / buy - 1})
    return pd.DataFrame(rows)


EPS_GRID = [EpsConfig(en, g, h, t, sp) for en, g, h, t, sp in itertools.product(
    ["gap_open", "next_open"], [-1.0, 0.0, 0.02, 0.04], [1, 3, 5, 10, 20], [False, True], [None, 0.0, 0.05])]


def trade_stats(tr: pd.DataFrame, w, col: str = "return") -> dict:
    t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)] if len(tr) else tr
    if not len(t):
        return {"trades": 0, "win_rate": np.nan, "avg": np.nan}
    return {"trades": int(len(t)), "win_rate": float((t[col] > 0).mean()), "avg": float(t[col].mean())}


def select(grid: pd.DataFrame, min_trades: int, avg_col: str = "design_avg") -> pd.Series:
    """Rule fixed before running: highest design win rate among variants with at least
    `min_trades` design trades and a positive average design result; ties -> higher average."""
    ok = grid[(grid.design_trades >= min_trades) & (grid[avg_col] > 0)]
    return (ok if len(ok) else grid).sort_values(["design_win_rate", avg_col], ascending=False).iloc[0]


def eps_portfolio(px: dict, trades: pd.DataFrame, w, cash: float, max_positions: int, splits: dict):
    """Equal slots, whole shares at real prices; trades in entry order, skipped when full."""
    idx = px["close"].index
    days = idx[in_window(idx, w)]
    t = trades[in_window(pd.DatetimeIndex(trades["entry"]), w)].sort_values(["entry", "gap"], ascending=[True, False])
    by_entry = {d: g for d, g in t.groupby("entry")}
    o, c = px["open"], px["close"]
    held, equity, done = [], [], []
    for day in days:
        for h in [h for h in held if h["exit"] == day]:
            f = split_factor(h["symbol"], day, splits)
            cash += h["n"] * o.at[day, h["symbol"]] * f * (1 - SLIP)
            held.remove(h)
            done.append(h)
        for tr in (by_entry[day].itertuples() if day in by_entry else []):
            if len(held) >= max_positions:
                break
            value = cash + sum(h["n"] * c.at[day, h["symbol"]] * split_factor(h["symbol"], day, splits) for h in held)
            f = split_factor(tr.symbol, day, splits)
            fill = o.at[day, tr.symbol] * f * (1 + SLIP)
            n = int(min(value / max_positions, cash) // fill)
            if n > 0:
                cash -= n * fill
                held.append({"symbol": tr.symbol, "n": n, "exit": tr.exit})
        equity.append(cash + sum(h["n"] * c.at[day, h["symbol"]] * split_factor(h["symbol"], day, splits) for h in held))
    return pd.Series(equity, index=days)


# ======================================================= 2. options on the picks
def calm_vol(close: pd.DataFrame, n: int = 40, trim: int = 2) -> pd.DataFrame:
    """Annualized realized volatility over n sessions without the `trim` largest moves
    (so one earnings jump doesn't set the price of next month's options)."""
    r = np.log(close).diff()
    def one(x):
        x = x[np.isfinite(x)]
        if len(x) < n // 2:
            return np.nan
        x = np.sort(np.abs(x))[:-trim] if trim else x
        return math.sqrt(np.mean(x ** 2) * 252)
    return r.rolling(n).apply(one, raw=True)


def opt_half_spread(mid: float) -> float:
    return max(0.02, 0.03 * mid)


@dataclass(frozen=True)
class OptConfig:
    signal: str = "dip"          # dip | eps | qqq_dip | qqq_weekly
    structure: str = "put_spread"  # put_spread (sell) | call (buy)
    otm: float = 0.04            # put spread: short strike this far below; call: strike this far above
    width: float = 0.05          # put spread: long put this much further below (fraction of the price)
    dte: int = 10                # sessions to expiry at entry
    take_profit: float = 0.0     # put spread: buy back at (1-tp) of the credit; call: sell at +tp
    hold: int = 0                # call: sell at the close after `hold` sessions (0 = at expiry)

    def label(self) -> str:
        if self.structure == "put_spread":
            tp = f" tp{self.take_profit:g}" if self.take_profit else ""
            return f"{self.signal}: sell put spread {self.otm:.0%} OTM w{self.width:.1%} {self.dte}d{tp}"
        return (f"{self.signal}: buy call {self.otm:.0%} OTM {self.dte}d hold{self.hold or self.dte}"
                f"{f' tp+{self.take_profit:.0%}' if self.take_profit else ''}")


def option_trade(cfg: OptConfig, path_open: float, closes: np.ndarray, iv: float, r: float, real: float) -> dict:
    """One trade from the entry open through `closes` (the closes from the entry day on).
    Returns pnl and risk per contract in real dollars (prices x `real` split factor)."""
    s0 = path_open
    if cfg.structure == "put_spread":
        ks, kl = s0 * (1 - cfg.otm), s0 * (1 - cfg.otm - cfg.width)
        t0 = (cfg.dte + 1) / 252
        ps, pl = (bs_price(s0, k, t0, implied_vol(iv, s0, k, t0), r, False) for k in (ks, kl))
        credit = (ps - opt_half_spread(ps)) - (pl + opt_half_spread(pl))
        width = ks - kl
        if credit <= 0.01 * width:
            return {}
        risk = (width - credit) * real * MULTIPLIER + 4 * COMMISSION
        for j in range(cfg.dte + 1):
            s = closes[j]
            if j == cfg.dte:                                   # expiry: settle at intrinsic
                owed = max(0.0, ks - s) - max(0.0, kl - s)
                cost = owed + (opt_half_spread(owed) if owed > 0 else 0.0)
                legs = 4 if owed > 0 else 2
                break
            if cfg.take_profit:
                t = (cfg.dte - j) / 252
                vs, vl = (bs_price(s, k, t, implied_vol(iv, s, k, t), r, False) for k in (ks, kl))
                buyback = (vs + opt_half_spread(vs)) - (vl - opt_half_spread(vl))
                if buyback <= (1 - cfg.take_profit) * credit:
                    cost, legs = max(buyback, 0.0), 4
                    break
        pnl = (credit - cost) * real * MULTIPLIER - legs * COMMISSION
        return {"pnl": pnl, "risk": risk, "ret": pnl / risk, "days": j}
    # long call
    k = s0 * (1 + cfg.otm)
    t0 = (cfg.dte + 1) / 252
    mid = bs_price(s0, k, t0, implied_vol(iv, s0, k, t0), r, True)
    paid = mid + opt_half_spread(mid)
    if paid <= 0.05:
        return {}
    last = min(cfg.hold or cfg.dte, cfg.dte)
    for j in range(last + 1):
        t = (cfg.dte - j) / 252
        v = bs_price(closes[j], k, t, implied_vol(iv, closes[j], k, t), r, True)
        got = max(v - opt_half_spread(v), 0.0)
        if (cfg.take_profit and got >= (1 + cfg.take_profit) * paid) or j == last:
            break
    pnl = (got - paid) * real * MULTIPLIER - 2 * COMMISSION
    risk = paid * real * MULTIPLIER + COMMISSION
    return {"pnl": pnl, "risk": risk, "ret": pnl / risk, "days": j}


def signal_entries(px: dict, eps_tr: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """{signal: DataFrame(symbol, entry)} of entry sessions (bought at that open)."""
    idx = px["close"].index
    syms = list(POOLS[DIP.pool])
    ind = indicators(px["close"][syms])
    entry, _ = signals(px["close"][syms], ind, DIP)
    fresh = entry & ~entry.shift(1, fill_value=False)
    out = {}
    st = fresh[[s for s in STOCKS if s in fresh]].stack()
    st = st[st]
    pos = idx.get_indexer(st.index.get_level_values(0)) + 1
    ok = pos < len(idx)
    out["dip"] = pd.DataFrame({"symbol": st.index.get_level_values(1)[ok], "entry": idx[pos[ok]]})
    out["eps"] = eps_tr[["symbol", "entry"]].copy()
    q = px["close"]["QQQ"]
    from .pullback import rsi
    up = q > q.rolling(200).mean()
    qdip = up & (rsi(q.to_frame())["QQQ"] < 10)
    qdip = qdip & ~qdip.shift(1, fill_value=False)
    pos = idx.get_indexer(qdip[qdip].index) + 1
    out["qqq_dip"] = pd.DataFrame({"symbol": "QQQ", "entry": idx[pos[pos < len(idx)]]})
    weeks = pd.Series(idx, index=idx).groupby(idx.to_period("W")).first()
    starts = [d for d in weeks if idx.get_loc(d) > 0 and up.iloc[idx.get_loc(d) - 1]]
    out["qqq_weekly"] = pd.DataFrame({"symbol": "QQQ", "entry": starts})
    return out


def pricing_multipliers(px: dict, vol: pd.DataFrame, entries: dict) -> dict:
    """fair multiplier per signal (std of the realized move over 10 sessions in units of
    the volatility estimate, on design entries) and the volatility premium VIX carries."""
    vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")[["open", "close"]]
    spy = pd.DataFrame({k: px[k]["SPY"] for k in ("open", "close")})
    premium = 1.0 / fair_multipliers(spy, vix, *DESIGN)[5]
    c, o = px["close"], px["open"]
    idx = c.index
    fair = {}
    for name, e in entries.items():
        e = e[in_window(pd.DatetimeIndex(e["entry"]), DESIGN)]
        z = []
        for s, d in zip(e["symbol"], e["entry"]):
            i = idx.get_loc(d)
            if i + 10 < len(idx):
                v = vol.at[idx[i - 1], s]
                if np.isfinite(v) and v > 0:
                    z.append(math.log(c.iat[i + 10, c.columns.get_loc(s)] / o.at[d, s]) / (v * math.sqrt(11 / 252)))
        fair[name] = float(np.nanstd(z)) if z else 1.0
    return {"fair": fair, "premium": premium}


def run_options(px: dict, entries: pd.DataFrame, cfg: OptConfig, vol: pd.DataFrame, mult: float,
                splits: dict) -> pd.DataFrame:
    c, o = px["close"], px["open"]
    idx = c.index
    rows = []
    for s, d in zip(entries["symbol"], entries["entry"]):
        i = idx.get_loc(d)
        if i < 1 or i + cfg.dte >= len(idx):
            continue
        v = vol.at[idx[i - 1], s]
        s0 = o.at[d, s]
        closes = c[s].values[i:i + cfg.dte + 1]
        if not (np.isfinite(v) and v > 0 and np.isfinite(s0) and np.isfinite(closes).all()):
            continue
        t = option_trade(cfg, s0, closes, v * mult, rate(d.year), split_factor(s, d, splits))
        if t:
            rows.append({"symbol": s, "entry": d, "exit": idx[i + t["days"]], **t})
    return pd.DataFrame(rows)


OPT_GRID = ([OptConfig(sig, "put_spread", k, w, dte, tp)
             for sig, k, w, dte, tp in itertools.product(["dip", "eps", "qqq_dip", "qqq_weekly"], [0.02, 0.04, 0.06, 0.08],
                                                         [0.025, 0.05], [5, 10, 21], [0.0, 0.5])]
            + [OptConfig(sig, "call", k, 0, dte, tp, hold)
               for sig, k, dte, tp, hold in itertools.product(["dip", "eps", "qqq_dip", "qqq_weekly"], [0.0, 0.03],
                                                              [10, 21], [0.0, 0.5, 1.0], [5, 0])])


def options_portfolio(trades: pd.DataFrame, idx, w, cash: float, risk: float, max_open: int) -> pd.Series:
    """Each trade risks `risk` of equity (its max loss); at most `max_open` at a time."""
    days = idx[in_window(idx, w)]
    t = trades[in_window(pd.DatetimeIndex(trades["entry"]), w)].sort_values("entry")
    by_entry = {d: g for d, g in t.groupby("entry")}
    open_, equity = [], []
    for day in days:
        for p in [p for p in open_ if p["exit"] <= day]:
            cash += p["stake"] * p["ret"]
            open_.remove(p)
        for tr in (by_entry[day].itertuples() if day in by_entry else []):
            if len(open_) < max_open and cash > 0:
                open_.append({"exit": tr.exit, "ret": tr.ret, "stake": cash * risk})
        equity.append(cash)
    return pd.Series(equity, index=days)


# =========================================================== 3 + 4. Asia, patterns
def asia_and_patterns(px: dict, analogs: bool = True) -> dict:
    asia = asia_overnight(px)
    us = [s for s in list(SECTORS) + ["SPY", "QQQ"] + list(STOCKS) if s in px["close"]]
    impact = {name: asia_impact(px, us, asia["asia"], *w).round(4) for name, w in (("design", DESIGN), ("test", TEST))}
    stocks = [s for s in STOCKS if s in px["close"]]
    frames = pattern_frames(px, stocks)
    fwd = {5: forward_returns(px, stocks, 5), 20: forward_returns(px, stocks, 20)}
    pats = {name: pattern_stats(frames, fwd, *w).round(4) for name, w in (("design", DESIGN), ("test", TEST))}
    vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")["close"]
    X = market_features(px, vix, asia["asia"])
    Y = market_outcomes(px)
    # analog forecaster: choose k and the confidence cut on 2016-2021 (walk-forward),
    # then score 2021-2026 once; accuracy only on days it is confident
    analog = {}
    for target in (("spy_oc", "qqq_oc", "spy_5d") if analogs else ()):
        y = Y[target]
        design_days = X.index[in_window(X.index, ("2016-01-04", DESIGN[1]))]
        test_days = X.index[in_window(X.index, TEST)]
        best = None
        for k in (25, 50, 100):
            p = pd.Series({d: analog_forecast(X, y, d, k)["p_up"] for d in design_days[::2]})
            for cut in (0.55, 0.60, 0.65):
                conf = p[(p >= cut) | (p <= 1 - cut)]
                if len(conf) < 60:
                    continue
                acc = float(((conf >= 0.5) == (y.reindex(conf.index) > 0)).mean())
                if best is None or acc > best["design_accuracy"]:
                    best = {"k": k, "cut": cut, "design_accuracy": acc, "design_days": int(len(conf))}
        p = pd.Series({d: analog_forecast(X, y, d, best["k"])["p_up"] for d in test_days})
        conf = p[(p >= best["cut"]) | (p <= 1 - best["cut"])]
        yt = y.reindex(conf.index)
        analog[target] = best | {
            "test_accuracy_confident": float(((conf >= 0.5) == (yt > 0)).mean()) if len(conf) else np.nan,
            "test_confident_days": int(len(conf)),
            "test_base_rate_up": float((y.reindex(test_days) > 0).mean()),
            "test_accuracy_all_days": float(((p >= 0.5) == (y.reindex(p.index) > 0)).mean()),
        }
    return {"impact": impact, "patterns": pats, "analog": analog}


def condor_overlay(w):
    """The emails' calm-day 0DTE QQQ condor (vol_alert.py: 1% strikes, 1% wings, calmest
    30% of days, 2% max loss per trade) at mid pricing over window `w`."""
    from .condor_research import run_condor
    from .vol_alert import CALM_SHARE, CONDOR_K, CONDOR_W
    from .zero_dte_research import context
    ctx = context()
    p, q = ctx["probs"]["complex"], ctx["q"]
    mask = in_window(q.index, w)
    flags = (p <= p[ctx["design_mask"] & p.notna()].quantile(CALM_SHARE)) & mask
    return run_condor(q, ctx["vix"]["open"], flags, CONDOR_K, CONDOR_W, 0.02, ctx["mults"]["mid"], *w, CASH)


def combined_plan(px, splits, summary, eps_tr, entries, vol, mults) -> dict:
    """The whole plan on $1.5M: half dip-buys, half EPS picks, options on top (each
    options rule risking 1% a trade, at most 5 open; the condor 2%), mid pricing."""
    from .backtest import buy_and_hold
    from .pullback import run_pullback
    combined = {}
    for name, w in (("design", DESIGN), ("test", TEST)):
        dip_eq, dip_trips = run_pullback(px, DIP, *w, CASH / 2, splits)
        eps_eq = eps_portfolio(px, eps_tr, w, CASH / 2, 5, splits)
        total = dip_eq + eps_eq.reindex(dip_eq.index).ffill()
        parts = {"dip shares": float(dip_eq.iloc[-1] - dip_eq.iloc[0]), "EPS shares": float(eps_eq.iloc[-1] - eps_eq.iloc[0])}
        wins, n = int((dip_trips["return"] > 0).sum()), len(dip_trips)
        et = eps_tr[in_window(pd.DatetimeIndex(eps_tr["entry"]), w)]
        wins, n = wins + int((et["return"] > 0).sum()), n + len(et)
        for group in ("dip", "eps", "qqq"):
            cfg = OptConfig(**summary["options_pick"][group]["config"])
            f = mults["fair"][cfg.signal]
            tr = run_options(px, entries[cfg.signal], cfg, vol, f * (1 + mults["premium"]) / 2, splits)
            ov = options_portfolio(tr, px["close"].index, w, CASH, 0.01, 5)
            total = total + (ov - CASH).reindex(total.index).ffill().fillna(0)
            parts[f"{group} options"] = float(ov.iloc[-1] - CASH)
            tw = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]
            wins, n = wins + int((tw["ret"] > 0).sum()), n + len(tw)
        ce, ct = condor_overlay(w)
        total = total + (ce - CASH).reindex(total.index).ffill().fillna(0)
        parts["QQQ 0DTE condors"] = float(ce.iloc[-1] - CASH)
        wins, n = wins + int(ct.win.sum()), n + len(ct)
        m = performance(total)
        spy = performance(buy_and_hold(px, "SPY", *w, CASH))
        combined |= {f"{name}_final": m["final_value"], f"{name}_cagr": m["cagr"], f"{name}_maxdd": m["max_drawdown"],
                     f"{name}_trades": n, f"{name}_win_rate": wins / n if n else np.nan, f"{name}_parts": parts,
                     f"{name}_spy_final": spy["final_value"], f"{name}_spy_cagr": spy["cagr"]}
        if name == "test":
            total.to_csv(OUT / "combined_test_equity.csv", float_format="%.2f")
            dm = performance(dip_eq)
            summary.setdefault("dip_pick", {}).update(
                {"label": DIP.label(), "test_win_rate": float((dip_trips["return"] > 0).mean()),
                 "test_trades": int(len(dip_trips)), "test_cagr": dm["cagr"], "test_maxdd": dm["max_drawdown"]})
        else:
            summary.setdefault("dip_pick", {})["design_win_rate"] = float((dip_trips["return"] > 0).mean())
    combined["spy_final"] = combined["test_spy_final"]
    return combined


def rerun_combined():
    """Recompute only the whole-plan backtest from the saved picks."""
    syms = list(dict.fromkeys(list(POOLS["stocks+etfs"]) + ["SPY", "QQQ"]))
    px = load_prices(syms)
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    summary = json.loads((OUT / "summary.json").read_text())
    events, _ = earnings_events(px, load_eps(OUT / "eps_history.json"))
    eps_tr = eps_trades(px, events, EpsConfig(**summary["eps_pick"]["config"]))
    vol = calm_vol(px["close"][list(STOCKS) + ["QQQ"]])
    summary["combined"] = combined_plan(px, splits, summary, eps_tr, signal_entries(px, eps_tr), vol, summary["option_pricing"])
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary["combined"], indent=1, default=str))


def revalidate():
    """Re-check the saved options rules and the whole plan on the current stock list,
    with the email's share rules (results/accuracy/summary.json), without re-running
    the options grid (the rules stay as chosen; their numbers are refreshed)."""
    from .accuracy_research import Exit, arrays, dip_entries, eps_entries, filtered, portfolio, research_setup, simulate
    from .backtest import buy_and_hold
    acc = json.loads((RESULTS_DIR / "accuracy" / "summary.json").read_text())
    px, syms, eligible, _, events, check = research_setup(
        list(SECTORS) + [a for a in ASIA if (DATA_DIR / f"{a}.csv").exists()])
    summary = json.loads((OUT / "summary.json").read_text())
    summary["earnings_day_check"] = check
    arr = arrays(px, syms)
    d, e = acc["dip"], acc["eps"]
    dex, eex = Exit(**d["exit"]), Exit(**e["exit"])
    dip_ent = filtered(px, dip_entries(px, d["rsi_max"], d["momentum_top"], eligible), d["spy_trend"],
                       dex.hold if d["no_earnings"] else None, events)
    r = e["rule"]
    eps_ent = filtered(px, eps_entries(px, events, EpsConfig(r["entry"], r["min_gap"], 1, False, r.get("min_surprise")))[["symbol", "entry"]],
                       e["spy_trend"], None)
    dip_tr, eps_tr = simulate(px, arr, dip_ent, dex, syms), simulate(px, arr, eps_ent, eex, syms)
    qqq = signal_entries(px, eps_ent.assign(entry=pd.DatetimeIndex(eps_ent["entry"])))
    entries = {"dip": dip_ent[dip_ent.symbol.isin(STOCKS)].reset_index(drop=True), "eps": eps_ent.reset_index(drop=True),
               "qqq_dip": qqq["qqq_dip"], "qqq_weekly": qqq["qqq_weekly"]}
    vol = calm_vol(px["close"][list(dict.fromkeys([s for s in STOCKS if s in px["close"]] + ["QQQ"]))])
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    mults = pricing_multipliers(px, vol, entries)
    summary["option_pricing"] = mults
    print("pricing:", mults, flush=True)
    trades = {}
    for group, pick in summary["options_pick"].items():
        cfg = OptConfig(**pick["config"])
        f = mults["fair"][cfg.signal]
        res = {"config": asdict(cfg), "label": cfg.label()}
        for lvl, m in (("fair", f), ("mid", f * (1 + mults["premium"]) / 2), ("rich", f * mults["premium"])):
            tr = run_options(px, entries[cfg.signal], cfg, vol, m, splits)
            if lvl == "mid":
                tr.to_csv(OUT / f"options_trades_{group}_mid.csv", index=False, float_format="%.4f")
                trades[group] = tr
            for name, w in (("design", DESIGN), ("test", TEST)):
                st = trade_stats(tr, w, "ret")
                m_ = performance(options_portfolio(tr, px["close"].index, w, CASH, 0.01, 5))
                res[f"{name}_{lvl}"] = st | {"final": m_["final_value"], "cagr": m_["cagr"], "max_drawdown": m_["max_drawdown"],
                                             "worst_trade": float(tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]["ret"].min())}
        summary["options_pick"][group] = res
        print(group, json.dumps({k: v for k, v in res.items() if k.startswith("test")}, default=str), flush=True)
    combined = {}
    for name, w in (("design", DESIGN), ("test", TEST)):
        dip_eq, eps_eq = portfolio(px, dip_tr, w, 3, CASH / 2), portfolio(px, eps_tr, w, 5, CASH / 2)
        total = dip_eq + eps_eq.reindex(dip_eq.index).ffill()
        parts = {"dip shares": float(dip_eq.iloc[-1] - dip_eq.iloc[0]), "EPS shares": float(eps_eq.iloc[-1] - eps_eq.iloc[0])}
        wins = n = 0
        for tr in (dip_tr, eps_tr):
            t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]
            wins, n = wins + int((t["return"] > 0).sum()), n + len(t)
        for group, tr in trades.items():
            ov = options_portfolio(tr, px["close"].index, w, CASH, 0.01, 5)
            total = total + (ov - CASH).reindex(total.index).ffill().fillna(0)
            parts[f"{group} options"] = float(ov.iloc[-1] - CASH)
            t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]
            wins, n = wins + int((t["ret"] > 0).sum()), n + len(t)
        ce, ct = condor_overlay(w)
        total = total + (ce - CASH).reindex(total.index).ffill().fillna(0)
        parts["QQQ 0DTE condors"] = float(ce.iloc[-1] - CASH)
        wins, n = wins + int(ct.win.sum()), n + len(ct)
        m = performance(total)
        spy = performance(buy_and_hold(px, "SPY", *w, CASH))
        combined |= {f"{name}_final": m["final_value"], f"{name}_cagr": m["cagr"], f"{name}_maxdd": m["max_drawdown"],
                     f"{name}_trades": n, f"{name}_win_rate": wins / n if n else np.nan, f"{name}_parts": parts,
                     f"{name}_spy_final": spy["final_value"], f"{name}_spy_cagr": spy["cagr"]}
        if name == "test":
            total.to_csv(OUT / "combined_test_equity.csv", float_format="%.2f")
    combined["spy_final"] = combined["test_spy_final"]
    summary["combined"] = combined
    print("combined:", json.dumps(combined, indent=1, default=str), flush=True)
    ap = asia_and_patterns(px, analogs=False)
    for name, df in ap["impact"].items():
        df.to_csv(OUT / f"asia_impact_{name}.csv", float_format="%.4f")
    for name, df in ap["patterns"].items():
        df.to_csv(OUT / f"patterns_{name}.csv", float_format="%.4f")
    t = ap["impact"]["test"]
    summary["asia_continuation_test"] = float(t.loc[["SPY", "QQQ"], "continuation_rate"].mean())
    summary["asia_beta_test"] = t.loc[["SPY", "QQQ"], ["beta", "corr"]].to_dict()
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))


# ===================================================================== main
def main():
    if "--combined" in sys.argv:
        return rerun_combined()
    if "--revalidate" in sys.argv:
        return revalidate()
    eps_path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else str(OUT / "eps_history.json")
    OUT.mkdir(parents=True, exist_ok=True)
    syms = list(dict.fromkeys(list(POOLS["stocks+etfs"]) + ["SPY", "QQQ"] + list(SECTORS) + [a for a in ASIA if (DATA_DIR / f"{a}.csv").exists()]))
    px = load_prices(syms)
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    eps = load_eps(eps_path)
    if eps_path != str(OUT / "eps_history.json"):
        (OUT / "eps_history.json").write_text(open(eps_path).read())
    summary = {}

    # ---- 1. EPS picks
    events, check = earnings_events(px, eps)
    summary["earnings_day_check"] = check
    summary["events"] = {"total": int(len(events)), "known": int(events.known.sum()),
                         "first": str(events.day.min().date()), "last": str(events.day.max().date())}
    print("earnings days:", summary["events"], "check:", check, flush=True)
    rows = []
    for cfg in EPS_GRID:
        tr = eps_trades(px, events, cfg)
        d, t = trade_stats(tr, DESIGN), trade_stats(tr, TEST)
        rows.append({"label": cfg.label(), **asdict(cfg), **{f"design_{k}": v for k, v in d.items()},
                     **{f"test_{k}": v for k, v in t.items()}})
    eg = pd.DataFrame(rows)
    eg.to_csv(OUT / "eps_grid.csv", index=False, float_format="%.4f")
    pick = select(eg, 150)
    eps_cfg = EpsConfig(pick.entry, float(pick.min_gap), int(pick.hold), bool(pick.trend),
                        None if pd.isna(pick.min_surprise) else float(pick.min_surprise))
    eps_tr = eps_trades(px, events, eps_cfg)
    eps_tr.to_csv(OUT / "eps_trades.csv", index=False, float_format="%.4f")
    known = eps_tr[eps_tr.surprise.notna()]
    summary["eps_pick"] = {"config": asdict(eps_cfg), "label": eps_cfg.label(), **pick.drop(["label"]).to_dict(),
                           "with_eps_data": {"trades": int(len(known)),
                                             "win_rate_beats": float((known[known.surprise > 0]["return"] > 0).mean()) if (known.surprise > 0).any() else None,
                                             "win_rate_misses": float((known[known.surprise <= 0]["return"] > 0).mean()) if (known.surprise <= 0).any() else None}}
    for name, w in (("design", DESIGN), ("test", TEST)):
        eq = eps_portfolio(px, eps_tr, w, CASH, 5, splits)
        m = performance(eq)
        summary["eps_pick"][f"{name}_portfolio"] = {"final": m["final_value"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"]}
    print("EPS pick:", json.dumps(summary["eps_pick"], default=str, indent=1), flush=True)

    # ---- 2. options
    allsyms = list(dict.fromkeys(list(STOCKS) + ["QQQ"]))
    vol = calm_vol(px["close"][allsyms])
    entries = signal_entries(px, eps_tr)
    mults = pricing_multipliers(px, vol, entries)
    summary["option_pricing"] = mults
    print("pricing:", mults, flush=True)
    rows = []
    for cfg in OPT_GRID:
        f = mults["fair"][cfg.signal]
        levels = {"fair": f, "mid": f * (1 + mults["premium"]) / 2, "rich": f * mults["premium"]}
        r = {"label": cfg.label(), **asdict(cfg)}
        for lvl, m in levels.items():
            tr = run_options(px, entries[cfg.signal], cfg, vol, m, splits)
            for name, w in (("design", DESIGN), ("test", TEST)):
                st = trade_stats(tr, w, "ret")
                r |= {f"{name}_{k}_{lvl}": v for k, v in st.items()}
        r |= {"design_trades": r["design_trades_mid"], "design_win_rate": r["design_win_rate_mid"], "design_avg": r["design_avg_mid"]}
        rows.append(r)
        print(f"{cfg.label():58s} design win {r['design_win_rate']:.0%} avg {r['design_avg']:+.1%} n={r['design_trades']} | "
              f"test win {r['test_win_rate_mid']:.0%} avg {r['test_avg_mid']:+.1%}", flush=True)
    og = pd.DataFrame(rows)
    og.to_csv(OUT / "options_grid.csv", index=False, float_format="%.4f")
    summary["options_pick"] = {}
    for group, sigs, n_min in (("dip", ["dip"], 150), ("eps", ["eps"], 100), ("qqq", ["qqq_dip", "qqq_weekly"], 60)):
        pick = select(og[og.signal.isin(sigs)], n_min)
        cfg = OptConfig(str(pick.signal), str(pick.structure), float(pick.otm), float(pick.width), int(pick.dte),
                        float(pick.take_profit), int(pick.hold))
        f = mults["fair"][cfg.signal]
        res = {"config": asdict(cfg), "label": cfg.label()}
        for lvl, m in (("fair", f), ("mid", f * (1 + mults["premium"]) / 2), ("rich", f * mults["premium"])):
            tr = run_options(px, entries[cfg.signal], cfg, vol, m, splits)
            if lvl == "mid":
                tr.to_csv(OUT / f"options_trades_{group}_mid.csv", index=False, float_format="%.4f")
            for name, w in (("design", DESIGN), ("test", TEST)):
                st = trade_stats(tr, w, "ret")
                eq = options_portfolio(tr, px["close"].index, w, CASH, 0.01, 5)
                m_ = performance(eq)
                res[f"{name}_{lvl}"] = st | {"final": m_["final_value"], "cagr": m_["cagr"], "max_drawdown": m_["max_drawdown"],
                                             "worst_trade": float(tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]["ret"].min())}
        summary["options_pick"][group] = res
        print(group, json.dumps(res, default=str, indent=1), flush=True)

    summary["combined"] = combined_plan(px, splits, summary, eps_tr, entries, vol, mults)
    print("combined:", json.dumps(summary["combined"], indent=1, default=str), flush=True)

    # ---- 3 + 4. Asia and patterns
    ap = asia_and_patterns(px)
    for name, df in ap["impact"].items():
        df.to_csv(OUT / f"asia_impact_{name}.csv", float_format="%.4f")
    for name, df in ap["patterns"].items():
        df.to_csv(OUT / f"patterns_{name}.csv", float_format="%.4f")
    summary["analog_forecaster"] = ap["analog"]
    t = ap["impact"]["test"]
    summary["asia_continuation_test"] = float(t.loc[["SPY", "QQQ"], "continuation_rate"].mean())
    summary["asia_beta_test"] = t.loc[["SPY", "QQQ"], ["beta", "corr"]].to_dict()
    print("analogs:", json.dumps(ap["analog"], indent=1), flush=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
