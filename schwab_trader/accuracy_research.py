"""Raising the hit rate of each part of the daily plan email.

Same protocol as everything else: every choice is made on 2011-02 .. 2021-10, then
tested once on 2021-10 .. 2026-10. Trade rules: among variants that won at least 70%
of design trades (the 70-80% target), the one with the best average design trade.
(Taking the single highest hit rate instead gave 90%+ with +1% targets but almost no
profit and rare large losses - see the grids.)

  1. Stock picks (dip-buys, EPS picks) and chart patterns as trades with a limit-order
     profit target (+1/2/3%), an optional stop and a time limit. A target fills when
     the day's high reaches it (at the open if the stock opens above it); a stop fills
     when the low reaches it (at the open on a gap down); if both are touched the same
     day the stop is assumed first. Costs: 5 bps a side.
  2. Market direction: "similar past mornings" and a trend rule over 5, 10 and 20
     sessions, only on days the signal is confident.
  3. Asia -> each US sector's opening direction when Asia moved enough.
  4. Earnings: will a company beat its EPS estimate, from its own beat record.
  5. Gaps: how often a stock that opens 1%+ away from yesterday's close (no earnings)
     trades back to that close the same day.

    python -m schwab_trader.accuracy_research                  # everything
    python -m schwab_trader.accuracy_research --reuse-market   # keep the saved market-direction results
    python -m schwab_trader.accuracy_research --finalize       # re-select from the saved grids
"""
from __future__ import annotations

import itertools
import json
import sys
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .backtest import performance
from .config import DATA_DIR, RESULTS_DIR
from .daily_research import (DESIGN, DIP, OUT as DAILY_OUT, STOCKS, TEST, EpsConfig, earnings_events, in_window,
                             load_eps)
from .data import load_prices
from .patterns import (ASIA, SECTORS, analog_forecast, asia_overnight, market_features, pattern_frames)
from .pullback import POOLS, indicators, signals
from .universe import sp500_mask, was_member

OUT = RESULTS_DIR / "accuracy"
SLIP = 0.0005
CASH = 1_500_000.0


# ------------------------------------------------------------- trade engine
@dataclass(frozen=True)
class Exit:
    target: float | None = 0.02      # sell when up this much (limit order); None = no target
    stop: float | None = None        # sell when down this much (stop order); None = no stop
    hold: int = 10                   # sessions at most; sold at the close of the last one

    def label(self) -> str:
        t = f"+{self.target:.0%} target" if self.target else "no target"
        s = f", -{self.stop:.0%} stop" if self.stop else ""
        return f"{t}{s}, sell by session {self.hold}"


def arrays(px: dict, symbols) -> dict:
    return {k: px[k][list(symbols)].to_numpy(dtype=float) for k in ("open", "high", "low", "close")}


def simulate(px: dict, arr: dict, entries: pd.DataFrame, ex: Exit, symbols) -> pd.DataFrame:
    """entries: DataFrame(symbol, entry) bought at that session's open. Returns one row
    per trade with exit session, exit reason and return after costs."""
    if entries.empty:
        return pd.DataFrame(columns=["symbol", "entry", "exit", "reason", "return"])
    idx = px["close"].index
    col = {s: i for i, s in enumerate(symbols)}
    e = entries[entries.symbol.isin(col)]
    r = idx.get_indexer(pd.DatetimeIndex(e["entry"]))
    keep = r >= 0
    e, r = e[keep], r[keep]
    c = np.array([col[s] for s in e.symbol])
    T = len(idx)
    rows = r[:, None] + np.arange(ex.hold)[None, :]
    valid = rows < T
    rows_c = np.minimum(rows, T - 1)
    O, H, L, C = (arrays_[rows_c, c[:, None]] for arrays_ in (arr["open"], arr["high"], arr["low"], arr["close"]))
    entry = O[:, 0] * (1 + SLIP)
    tgt = entry * (1 + ex.target) if ex.target else np.full(len(e), np.inf)
    stp = entry * (1 - ex.stop) if ex.stop else np.full(len(e), -np.inf)
    hit_t = (H >= tgt[:, None]) & valid
    hit_s = (L <= stp[:, None]) & valid
    first_t = np.where(hit_t.any(1), hit_t.argmax(1), ex.hold)
    first_s = np.where(hit_s.any(1), hit_s.argmax(1), ex.hold)
    last = valid.sum(1) - 1
    k = np.minimum(np.minimum(first_t, first_s), last)
    i = np.arange(len(e))
    px_exit = C[i, last]
    reason = np.array(["time"] * len(e), dtype=object)
    stop_first = first_s <= first_t
    by_stop = (first_s < ex.hold) & stop_first & (first_s <= last)
    by_tgt = (first_t < ex.hold) & ~stop_first & (first_t <= last)
    px_exit = np.where(by_stop, np.where(k > 0, np.minimum(O[i, k], stp), stp), px_exit)
    px_exit = np.where(by_tgt, np.where(k > 0, np.maximum(O[i, k], tgt), tgt), px_exit)
    reason[by_stop], reason[by_tgt] = "stop", "target"
    k = np.where(by_stop | by_tgt, k, last)
    ret = px_exit * (1 - SLIP) / entry - 1
    ok = np.isfinite(ret) & (valid[:, 0])
    out = pd.DataFrame({"symbol": e.symbol.values, "entry": idx[r], "exit": idx[np.minimum(r + k, T - 1)],
                        "reason": reason, "return": ret})
    complete = (r + ex.hold - 1 < T) | (reason != "time")          # skip trades still open at the end of data
    return out[ok & complete].reset_index(drop=True)


def stats(tr: pd.DataFrame, w) -> dict:
    t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)] if len(tr) else tr
    if not len(t):
        return {"trades": 0, "win_rate": np.nan, "avg": np.nan, "avg_win": np.nan, "avg_loss": np.nan, "worst": np.nan}
    win = t["return"] > 0
    return {"trades": int(len(t)), "win_rate": float(win.mean()), "avg": float(t["return"].mean()),
            "avg_win": float(t.loc[win, "return"].mean()) if win.any() else np.nan,
            "avg_loss": float(t.loc[~win, "return"].mean()) if (~win).any() else np.nan,
            "worst": float(t["return"].min())}


def portfolio(px: dict, tr: pd.DataFrame, w, slots: int, cash: float = CASH) -> pd.Series:
    """Equal slots of current equity, trades taken in entry order when a slot is free,
    marked to market at each close."""
    close = px["close"]
    idx = close.index
    days = idx[in_window(idx, w)]
    t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)].sort_values("entry").rename(columns={"return": "ret"})
    by_entry = {d: g for d, g in t.groupby("entry")}
    open_, eq = [], []
    for d in days:
        if d in by_entry:
            value = cash + sum(p["stake"] * close.at[d, p["sym"]] / p["px0"] for p in open_)
            for tr_ in by_entry[d].itertuples():
                if len(open_) >= slots:
                    break
                stake = min(value / slots, cash)
                if stake <= 0:
                    break
                cash -= stake
                open_.append({"sym": tr_.symbol, "exit": tr_.exit, "ret": tr_.ret, "stake": stake,
                              "px0": px["open"].at[d, tr_.symbol]})
        settled = [p for p in open_ if p["exit"] == d]
        for p in settled:
            cash += p["stake"] * (1 + p["ret"])
            open_.remove(p)
        eq.append(cash + sum(p["stake"] * close.at[d, p["sym"]] / p["px0"] for p in open_))
    return pd.Series(eq, index=days)


EXITS = [Exit(t, s, h) for t, s, h in itertools.product([0.01, 0.02, 0.03, None], [None, 0.05, 0.08, 0.10, 0.15],
                                                         [5, 10, 20])]


TARGET_HIT_RATE = 0.70
WORST_ALLOWED = -0.20      # safety: no design trade may lose more than this


def select_safe(rows: pd.DataFrame, min_trades: int) -> tuple[pd.Series, bool]:
    """(pick, safety_met): the best average design trade among variants that won at
    least TARGET_HIT_RATE of their design trades AND never lost more than WORST_ALLOWED
    on one; if none qualifies, `select` (no limit on the worst trade)."""
    ok = rows[(rows.design_trades >= min_trades) & (rows.design_avg > 0) & (rows.design_win_rate >= TARGET_HIT_RATE)
              & (rows.design_worst >= WORST_ALLOWED)]
    if len(ok):
        return ok.sort_values("design_avg", ascending=False).iloc[0], True
    return select(rows, min_trades), False


def select(rows: pd.DataFrame, min_trades: int) -> pd.Series:
    """Best average design trade among variants with at least `min_trades` design
    trades that won at least TARGET_HIT_RATE of them (falls back to the highest hit rate)."""
    ok = rows[(rows.design_trades >= min_trades) & (rows.design_avg > 0) & (rows.design_win_rate >= TARGET_HIT_RATE)]
    if len(ok):
        return ok.sort_values("design_avg", ascending=False).iloc[0]
    ok = rows[(rows.design_trades >= min_trades) & (rows.design_avg > 0)]
    return (ok if len(ok) else rows).sort_values(["design_win_rate", "design_avg"], ascending=False).iloc[0]


def grid(px, arr, entries, symbols, exits=EXITS) -> pd.DataFrame:
    rows = []
    for ex in exits:
        tr = simulate(px, arr, entries, ex, symbols)
        d, t = stats(tr, DESIGN), stats(tr, TEST)
        rows.append({"exit": ex.label(), **asdict(ex), **{f"design_{k}": v for k, v in d.items()},
                     **{f"test_{k}": v for k, v in t.items()}})
    return pd.DataFrame(rows)


# --------------------------------------------------------------- entries
def dip_entries(px: dict, rsi_max: float, top: int | None = None, eligible: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every fresh dip signal (no position limit), bought at the next open. `eligible`:
    S&P membership (stocks outside the index that day are neither ranked nor bought)."""
    syms = list(POOLS[DIP.pool])
    close = px["close"][syms]
    ind = indicators(close, None if eligible is None else eligible.reindex(columns=syms).fillna(True))
    cfg = DIP.__class__(**{**asdict(DIP), "entry_rsi": rsi_max, "momentum_top": top or DIP.momentum_top})
    entry, _ = signals(close, ind, cfg)
    fresh = entry & ~entry.shift(1, fill_value=False)
    st = fresh.stack()
    st = st[st]
    idx = close.index
    pos = idx.get_indexer(st.index.get_level_values(0)) + 1
    ok = pos < len(idx)
    return pd.DataFrame({"symbol": st.index.get_level_values(1)[ok], "entry": idx[pos[ok]]})


def eps_entries(px: dict, events: pd.DataFrame, cfg: EpsConfig) -> pd.DataFrame:
    from .daily_research import eps_trades
    return eps_trades(px, events, cfg)[["symbol", "entry", "surprise"]]


def spy_uptrend(px: dict) -> pd.Series:
    """SPY above its 200-day average at each close."""
    c = px["close"]["SPY"]
    return (c > c.rolling(200).mean()).fillna(False)


def sessions_to_earnings(px: dict, events: pd.DataFrame, entries: pd.DataFrame) -> np.ndarray:
    """For each entry: sessions from the entry day to the next earnings reaction day
    (0 = that day; large when none). Earnings dates are announced weeks ahead."""
    idx = px["close"].index
    by = {s: np.sort(idx.get_indexer(pd.DatetimeIndex(g.day))) for s, g in events.groupby("symbol")}
    pos = idx.get_indexer(pd.DatetimeIndex(entries["entry"]))
    out = np.full(len(entries), 10_000)
    for j, (s, p) in enumerate(zip(entries["symbol"], pos)):
        d = by.get(s)
        if d is not None and len(d):
            k = np.searchsorted(d, p)
            if k < len(d):
                out[j] = d[k] - p
    return out


def filtered(px: dict, entries: pd.DataFrame, trend: bool, no_earn_hold: int | None, events=None) -> pd.DataFrame:
    """Entries kept by the safety filters: SPY in an uptrend at the signal close, and/or
    no earnings report during the first `no_earn_hold` sessions of the trade."""
    e = entries
    if trend and len(e):
        up = spy_uptrend(px)
        idx = px["close"].index
        prev = idx[np.maximum(idx.get_indexer(pd.DatetimeIndex(e["entry"])) - 1, 0)]
        e = e[up.reindex(prev).to_numpy()]
    if no_earn_hold and len(e):
        e = e[sessions_to_earnings(px, events, e) >= no_earn_hold]
    return e


def pattern_entries(px: dict, frames: dict, name: str, uptrend_only: bool, eligible: pd.DataFrame | None = None) -> pd.DataFrame:
    f = frames[name]
    if eligible is not None:
        f = f & eligible.reindex(index=f.index, columns=f.columns).fillna(True).astype(bool)
    if uptrend_only:
        c = px["close"][f.columns]
        f = f & (c > c.rolling(200).mean())
    st = f.stack()
    st = st[st]
    idx = f.index
    pos = idx.get_indexer(st.index.get_level_values(0)) + 1
    ok = pos < len(idx)
    return pd.DataFrame({"symbol": st.index.get_level_values(1)[ok], "entry": idx[pos[ok]]})


def _exit(pick) -> Exit:
    return Exit(None if pd.isna(pick.target) else float(pick.target), None if pd.isna(pick.stop) else float(pick.stop),
                int(pick.hold))


def _summarize(px, tr, slots: int) -> dict:
    out = {"design": stats(tr, DESIGN), "test": stats(tr, TEST)}
    for name, w in (("design", DESIGN), ("test", TEST)):
        m = performance(portfolio(px, tr, w, slots))
        out[f"{name}_portfolio"] = {"cagr": m["cagr"], "max_drawdown": m["max_drawdown"]}
    return out


def dip_rule(px, arr, syms, grid_rows: pd.DataFrame, eligible, events) -> dict:
    pick, safe = select_safe(grid_rows, 500)
    ex = _exit(pick)
    ent = filtered(px, dip_entries(px, float(pick.rsi_max), int(pick.momentum_top), eligible), bool(pick.spy_trend),
                   ex.hold if pick.no_earnings else None, events)
    tr = simulate(px, arr, ent, ex, syms)
    return {"rsi_max": float(pick.rsi_max), "momentum_top": int(pick.momentum_top), "spy_trend": bool(pick.spy_trend),
            "no_earnings": bool(pick.no_earnings), "exit": asdict(ex), "label": ex.label(), "safety_rule_met": safe,
            **_summarize(px, tr, 3)}


def eps_rule(px, arr, syms, grid_rows: pd.DataFrame, events) -> dict:
    pick, safe = select_safe(grid_rows, 300)
    ex = _exit(pick)
    cfg = EpsConfig(pick.entry_kind, float(pick.min_gap), 1, False, None if pd.isna(pick.min_surprise) else float(pick.min_surprise))
    ent = filtered(px, eps_entries(px, events, cfg)[["symbol", "entry"]], bool(pick.spy_trend), None)
    tr = simulate(px, arr, ent, ex, syms)
    return {"rule": {**asdict(cfg), "hold": ex.hold}, "spy_trend": bool(pick.spy_trend), "exit": asdict(ex),
            "label": f"{pick.eps_rule}; {ex.label()}", "safety_rule_met": safe, **_summarize(px, tr, 5)}


def pattern_rules(pat: pd.DataFrame) -> dict:
    """{pattern: its exit and record} for each pattern on any stock (the email's list)."""
    best = {}
    for name, g in pat[~pat.uptrend_only.astype(bool)].groupby("pattern"):
        p = select(g, 150)
        best[name] = {"exit": {"target": float(p.target), "stop": None, "hold": int(p.hold)}, "label": p.exit,
                      "design_win_rate": float(p.design_win_rate), "design_trades": int(p.design_trades),
                      "design_avg": float(p.design_avg), "test_win_rate": float(p.test_win_rate),
                      "test_trades": int(p.test_trades), "test_avg": float(p.test_avg)}
    return best


def research_setup(extra=()):
    """Prices for the dip pool + market symbols, S&P membership and earnings events
    (events only on days the company was in the S&P 500)."""
    pool = list(POOLS[DIP.pool])
    syms = list(dict.fromkeys(pool + ["SPY", "QQQ"] + list(extra)))
    px = load_prices(syms)
    eligible = sp500_mask(px["close"].index, syms)
    eps = load_eps(DAILY_OUT / "eps_history.json")
    events, check = earnings_events(px, eps, [s for s in STOCKS if s in px["close"]])
    events = events[was_member(events.symbol, events.day)].reset_index(drop=True)
    return px, syms, eligible, eps, events, check


def finalize():
    """Re-select the trade rules from the saved grids and refresh their numbers."""
    px, syms, eligible, eps, events, _ = research_setup()
    arr = arrays(px, syms)
    summary = json.loads((OUT / "summary.json").read_text())
    summary["dip"] = dip_rule(px, arr, syms, pd.read_csv(OUT / "dip_grid.csv"), eligible, events)
    summary["eps"] = eps_rule(px, arr, syms, pd.read_csv(OUT / "eps_grid.csv"), events)
    summary["patterns"] = pattern_rules(pd.read_csv(OUT / "pattern_grid.csv"))
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: summary[k] for k in ("dip", "eps")}, indent=1, default=str))


def market_section(px, vix, asia) -> dict:
    """Market direction: the trend rule and "similar past mornings" over 5/10/20 sessions."""
    X = market_features(px, vix, asia["asia"])
    spy_o, spy_c = px["open"]["SPY"], px["close"]["SPY"]
    cands = []
    design_days = X.index[in_window(X.index, ("2014-01-02", DESIGN[1]))][::3]
    test_days = X.index[in_window(X.index, TEST)]
    sma50, sma200 = spy_c.rolling(50).mean(), spy_c.rolling(200).mean()
    for h in (5, 10, 20):
        y = spy_c.shift(-(h - 1)) / spy_o - 1
        # trend rule: SPY above its 200-day average and the 50-day average rising (at the prior close) -> up
        up = ((spy_c > sma200) & (sma50 > sma50.shift(10))).shift(1)
        for name, days in (("design", X.index[in_window(X.index, DESIGN)]), ("test", test_days)):
            sig = up.reindex(days).fillna(False).astype(bool)
            yy = y.reindex(days)
            ok = sig & yy.notna()
            cands.append({"model": "trend rule", "horizon": h, "k": None, "cut": None, "window": name,
                          "accuracy": float((yy[ok] > 0).mean()), "days": int(ok.sum()), "coverage": float(ok.mean()),
                          "base_rate": float((yy.dropna() > 0).mean())})
        for k in (50, 100):
            p = pd.Series({d: analog_forecast(X, y, d, k, gap=h)["p_up"] for d in design_days})
            pt = pd.Series({d: analog_forecast(X, y, d, k, gap=h)["p_up"] for d in test_days})
            for cut in (0.6, 0.65, 0.7):
                for name, pp in (("design", p), ("test", pt)):
                    conf = pp[(pp >= cut) | (pp <= 1 - cut)].dropna()
                    yy = y.reindex(conf.index)
                    cands.append({"model": "similar past mornings", "horizon": h, "k": k, "cut": cut, "window": name,
                                  "accuracy": float(((conf >= 0.5) == (yy > 0)).mean()) if len(conf) else np.nan,
                                  "days": int(len(conf)), "coverage": float(len(conf) / max(len(pp), 1)),
                                  "base_rate": float((y.reindex(pp.index).dropna() > 0).mean())})
        print("market horizon", h, "done", flush=True)
    mk = pd.DataFrame(cands)
    mk.to_csv(OUT / "market_grid.csv", index=False, float_format="%.4f")
    d = mk[mk.window == "design"].copy()
    d = d[d.coverage >= 0.2]
    best_d = d.sort_values("accuracy", ascending=False).iloc[0]
    t = mk[(mk.window == "test") & (mk.model == best_d.model) & (mk.horizon == best_d.horizon)
           & ((mk.k == best_d.k) | (mk.k.isna() & pd.isna(best_d.k))) & ((mk.cut == best_d.cut) | (mk.cut.isna() & pd.isna(best_d.cut)))].iloc[0]
    return {"model": best_d.model, "horizon": int(best_d.horizon),
            "k": None if pd.isna(best_d.k) else int(best_d.k), "cut": None if pd.isna(best_d.cut) else float(best_d.cut),
            "design_accuracy": best_d.accuracy, "design_coverage": best_d.coverage, "design_base_rate": best_d.base_rate,
            "test_accuracy": t.accuracy, "test_coverage": t.coverage, "test_days": int(t.days), "test_base_rate": t.base_rate}


# ------------------------------------------------------------------ main
def main():
    if "--finalize" in sys.argv:
        return finalize()
    OUT.mkdir(parents=True, exist_ok=True)
    old = json.loads((OUT / "summary.json").read_text()) if (OUT / "summary.json").exists() else {}
    px, syms, eligible, eps, events, check = research_setup(
        list(SECTORS) + [a for a in ASIA if (DATA_DIR / f"{a}.csv").exists()])
    arr = arrays(px, syms)
    n_members = eligible[[s for s in STOCKS if s in eligible]].sum(axis=1)
    summary = {"universe": {"stocks": int(sum(s in px["close"] for s in STOCKS)),
                            "sp500_members_with_prices_2011": int(n_members.loc["2011-02-01":].iloc[0]),
                            "sp500_members_with_prices_now": int(n_members.iloc[-1]),
                            "earnings_events": int(len(events)), "earnings_day_check": check}}
    print("universe:", summary["universe"], flush=True)

    # ---- 1a. dip-buys: entry threshold x how many of the strongest x safety filters x exits
    rows = []
    for rsi_max, top in itertools.product((5, 10), (10, 25, 50)):
        base = dip_entries(px, rsi_max, top, eligible)
        for trend, noearn in itertools.product((False, True), (False, True)):
            for hold in (5, 10, 20):
                ent = filtered(px, base, trend, hold if noearn else None, events)
                g = grid(px, arr, ent, syms, [e for e in EXITS if e.hold == hold])
                g.insert(0, "rsi_max", rsi_max)
                g.insert(1, "momentum_top", top)
                g.insert(2, "spy_trend", trend)
                g.insert(3, "no_earnings", noearn)
                rows.append(g)
        print("dip grid", rsi_max, top, flush=True)
    dip = pd.concat(rows, ignore_index=True)
    dip.to_csv(OUT / "dip_grid.csv", index=False, float_format="%.4f")
    summary["dip"] = dip_rule(px, arr, syms, dip, eligible, events)
    print("dip:", json.dumps(summary["dip"], indent=1), flush=True)

    # ---- 1b. EPS picks
    rows = []
    for en, gap, beat in itertools.product(["gap_open", "next_open"], [0.0, 0.02, 0.04], [None, 0.0]):
        cfg = EpsConfig(en, gap, 1, False, beat)
        base = eps_entries(px, events, cfg)[["symbol", "entry"]]
        for trend in (False, True):
            g = grid(px, arr, filtered(px, base, trend, None), syms)
            g.insert(0, "eps_rule", EpsConfig(en, gap, 0, False, beat).label().replace(" hold0", ""))
            g.insert(1, "entry_kind", en)
            g.insert(2, "min_gap", gap)
            g.insert(3, "min_surprise", beat)
            g.insert(4, "spy_trend", trend)
            rows.append(g)
    epsg = pd.concat(rows, ignore_index=True)
    epsg.to_csv(OUT / "eps_grid.csv", index=False, float_format="%.4f")
    summary["eps"] = eps_rule(px, arr, syms, epsg, events)
    print("eps:", json.dumps(summary["eps"], indent=1), flush=True)

    # ---- 1c. chart patterns as trades (one exit for all, chosen on design)
    stocks = [s for s in STOCKS if s in px["close"]]
    frames = pattern_frames(px, stocks)
    pexits = [Exit(t, None, h) for t, h in itertools.product([0.01, 0.02, 0.03], [5, 10, 20])]
    rows = []
    for name in frames:
        for up in (False, True):
            ent = pattern_entries(px, frames, name, up, eligible)
            for ex in pexits:
                tr = simulate(px, arr, ent, ex, syms)
                d, t = stats(tr, DESIGN), stats(tr, TEST)
                rows.append({"pattern": name, "uptrend_only": up, "exit": ex.label(), **asdict(ex),
                             **{f"design_{k}": v for k, v in d.items()}, **{f"test_{k}": v for k, v in t.items()}})
    pat = pd.DataFrame(rows)
    pat.to_csv(OUT / "pattern_grid.csv", index=False, float_format="%.4f")
    summary["patterns"] = pattern_rules(pat)
    print("patterns:", json.dumps({k: (round(v["design_win_rate"], 3), round(v["test_win_rate"], 3), v["label"])
                                   for k, v in summary["patterns"].items()}, indent=1), flush=True)

    # ---- 2. market direction (SPY, VIX and Asia only, so it doesn't change with the stock list)
    vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")["close"]
    asia = asia_overnight(px)
    if "--reuse-market" in sys.argv and "market" in old:
        summary["market"] = old["market"]
    else:
        summary["market"] = market_section(px, vix, asia)
    print("market:", json.dumps(summary["market"], indent=1, default=str), flush=True)

    # ---- 3. Asia -> opening direction of each US sector
    a = asia["asia"]
    rows = []
    for s in ["SPY", "QQQ"] + list(SECTORS):
        if s not in px["close"]:
            continue
        gap = px["open"][s] / px["close"][s].shift(1) - 1
        for th in (0.0025, 0.005, 0.0075, 0.01):
            for name, w in (("design", DESIGN), ("test", TEST)):
                m = in_window(a.index, w) & (a.abs() >= th).to_numpy() & gap.notna().to_numpy() & a.notna().to_numpy()
                rows.append({"symbol": s, "threshold": th, "window": name,
                             "accuracy": float((np.sign(a[m]) == np.sign(gap[m])).mean()) if m.any() else np.nan,
                             "days": int(m.sum()), "coverage": float(m.sum() / max(in_window(a.index, w).sum(), 1))})
    ag = pd.DataFrame(rows)
    ag.to_csv(OUT / "asia_grid.csv", index=False, float_format="%.4f")
    dsg = ag[(ag.window == "design") & (ag.coverage >= 0.15)].groupby("threshold").accuracy.mean()
    th = float(dsg.idxmax())
    res = ag[ag.threshold == th].pivot(index="symbol", columns="window", values="accuracy")
    summary["asia"] = {"threshold": th, "design_mean_accuracy": float(dsg.max()),
                       "by_symbol": {s: {"design": float(r.design), "test": float(r.test)} for s, r in res.iterrows()},
                       "test_coverage": float(ag[(ag.threshold == th) & (ag.window == "test")].coverage.mean())}
    print("asia:", json.dumps(summary["asia"], indent=1), flush=True)

    # ---- 4. earnings beats from each company's record
    e = eps.dropna(subset=["actual", "estimate"]).sort_values(["symbol", "date"]).copy()
    e = e[was_member(e.symbol, e.date)]
    e["beat"] = (e.actual > e.estimate).astype(float)
    rows = []
    for n in (4, 8):
        e[f"rate{n}"] = e.groupby("symbol").beat.transform(lambda b: b.shift(1).rolling(n, min_periods=n).mean())
        for r in (0.5, 0.625, 0.75, 0.875, 1.0):
            for name, w in (("design", ("2018-01-01", DESIGN[1])), ("test", TEST)):
                win = in_window(pd.DatetimeIndex(e.date), w) & e[f"rate{n}"].notna().to_numpy()
                call = win & (e[f"rate{n}"] >= r).to_numpy()
                rows.append({"n": n, "min_rate": r, "window": name, "accuracy": float(e.beat[call].mean()) if call.any() else np.nan,
                             "calls": int(call.sum()), "coverage": float(call.sum() / max(win.sum(), 1)),
                             "base_rate": float(e.beat[win].mean())})
    eg = pd.DataFrame(rows)
    eg.to_csv(OUT / "beat_grid.csv", index=False, float_format="%.4f")
    d = eg[(eg.window == "design") & (eg.coverage >= 0.3)].sort_values("accuracy", ascending=False).iloc[0]
    t = eg[(eg.window == "test") & (eg.n == d.n) & (eg.min_rate == d.min_rate)].iloc[0]
    summary["beats"] = {"n": int(d.n), "min_rate": float(d.min_rate), "design_accuracy": d.accuracy, "design_coverage": d.coverage,
                        "design_base_rate": d.base_rate, "test_accuracy": t.accuracy, "test_coverage": t.coverage,
                        "test_calls": int(t.calls), "test_base_rate": t.base_rate}
    print("beats:", json.dumps(summary["beats"], indent=1), flush=True)

    # ---- 5. gap fill (no earnings that day)
    c, o, h, l = (px[k][stocks] for k in ("close", "open", "high", "low"))
    prev = c.shift(1)
    gapr = o / prev - 1
    earn = pd.DataFrame(False, index=c.index, columns=stocks)
    for ev in events.itertuples():
        if ev.symbol in earn and ev.day in earn.index:
            earn.at[ev.day, ev.symbol] = True
    filled = ((gapr > 0) & (l <= prev)) | ((gapr < 0) & (h >= prev))
    earn |= ~eligible[stocks]                                 # count only days the stock was in the S&P 500
    rows = []
    for lo, hi in ((0.005, 0.01), (0.01, 0.02), (0.02, 0.04)):
        m = (gapr.abs() >= lo) & (gapr.abs() < hi) & ~earn
        for name, w in (("design", DESIGN), ("test", TEST)):
            mw = m & pd.DataFrame(np.repeat(in_window(c.index, w)[:, None], len(stocks), 1), index=c.index, columns=stocks)
            rows.append({"gap": f"{lo:.1%}-{hi:.1%}", "window": name, "fill_rate": float(filled[mw].stack().mean()),
                         "cases": int(mw.values.sum())})
    gf = pd.DataFrame(rows)
    gf.to_csv(OUT / "gap_fill.csv", index=False, float_format="%.4f")
    summary["gap_fill"] = gf.to_dict("records")
    print("gap fill:", gf.to_string(), flush=True)

    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
