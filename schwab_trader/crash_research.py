"""Crash test, crash shield and bubble gauges. No forecasts: everything here looks only at
prices that have already happened.

Data: the S&P 500 (SPX) and the Nasdaq 100 (NDX, the index behind QQQ) every day since
1993 from Robinhood's index history (data/long/), so the dot-com bubble and bust - the
closest past case to an AI bubble: the Nasdaq 100 rose about 5x from 1995 to March 2000,
then fell 83% - and 2008 are in the test. The plan's own stock data starts in 2010.

1. Crashes: every drop of 15%+ in either index since 1993 (10%+ for the plan test).
2. The crash shield: a market-trend rule chosen on 1993-2010 with the indexes alone (the
   dot-com bust, 2008 and everything between): the rule with the best return per unit of
   worst drop (CAGR / max drawdown, averaged over SPX and NDX) among a fixed list, holding
   the index while the rule is off and cash while it is on. While it is on the plan makes
   no new top-5 buys, EPS buys or options plays (open positions keep their exits). It is
   then checked once, unchanged, on the whole plan in 2011-2021 and 2021-2026 and crash
   by crash; it goes into the email only if it holds up there (rule below).
3. Bubble gauges: how stretched the Nasdaq 100 is above its 200-day average and over 3
   years, against its own history including 1999-2000, and what followed similar readings.

4. Crash exits for picks already open (--exits): sell when the shield turns on, after a
   fast market drop, or when a pick itself drops hard - each tested on the plan as it runs.

    python -m schwab_trader.crash_research
    python -m schwab_trader.crash_research --exits
    python -m schwab_trader.crash_research --current   # the plan as it runs: every crash, year and week
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import performance
from .config import DATA_DIR, RESULTS_DIR

OUT = RESULTS_DIR / "crashes"
LONG = DATA_DIR / "long"
CHOOSE = ("1993-06-01", "2010-12-31")     # the shield is chosen here (indexes only)
CHECK = ("2011-01-01", "2026-12-31")      # ...and checked here on the indexes too
SWITCH_COST = 0.001                       # 0.1% each time the proxy moves in or out
MAX_SWITCHES = 6.0                        # a year, on average: more is whipsaw
# a shield goes into the plan only if, on 2011-2021, it cuts the plan's worst drop by 2+
# points and costs at most 0.5%/yr, or it raises the plan's yearly return
MIN_DD_CUT, MAX_CAGR_COST = 0.02, 0.005


def load_index(sym: str) -> pd.Series:
    return pd.read_csv(LONG / f"{sym}.csv", parse_dates=["date"], index_col="date")["close"].sort_index()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def held(cond: pd.Series, days: int) -> pd.Series:
    """On after `days` closes in a row with cond, off after `days` closes in a row without."""
    on, run_on, run_off, out = False, 0, 0, []
    for c in cond.fillna(False).to_numpy():
        run_on, run_off = (run_on + 1, 0) if c else (0, run_off + 1)
        if not on and run_on >= days:
            on = True
        elif on and run_off >= days:
            on = False
        out.append(on)
    return pd.Series(out, index=cond.index)


RULES = {   # name -> (index, how), computed from closes; True = shield on after that close
    "S&P 500 below its 200-day average": ("SPX", "sma200"),
    "Nasdaq 100 below its 200-day average": ("NDX", "sma200"),
    "either index below its 200-day average": ("both", "sma200"),
    "S&P 500 below its 200-day average 3 days running (off after 3 days above)": ("SPX", "sma200x3"),
    "Nasdaq 100 below its 200-day average 3 days running (off after 3 days above)": ("NDX", "sma200x3"),
    "S&P 500 50-day average below its 200-day": ("SPX", "cross"),
    "S&P 500 10%+ below its 1-year high": ("SPX", "dd10"),
    "Nasdaq 100 15%+ below its 1-year high": ("NDX", "dd15"),
}


def rule_signal(how: str, s: pd.Series) -> pd.Series:
    if how == "sma200":
        return (s < sma(s, 200)) & sma(s, 200).notna()
    if how == "sma200x3":
        return held((s < sma(s, 200)) & sma(s, 200).notna(), 3)
    if how == "cross":
        return (sma(s, 50) < sma(s, 200)) & sma(s, 200).notna()
    if how in ("dd10", "dd15"):
        return s / s.rolling(252, min_periods=200).max() - 1 <= -(0.10 if how == "dd10" else 0.15)
    raise ValueError(how)


def shield_series(name: str, spx: pd.Series, ndx: pd.Series) -> pd.Series:
    idx, how = RULES[name]
    if idx == "both":
        a, b = rule_signal(how, spx), rule_signal(how, ndx).reindex(spx.index).fillna(False)
        return (a | b).astype(bool)
    return rule_signal(how, spx if idx == "SPX" else ndx).astype(bool)


def proxy(close: pd.Series, on: pd.Series, w) -> pd.Series:
    """The index while the shield is off, cash (no interest) while it is on; the shield
    seen at a close sets the next day's position."""
    c = close.loc[w[0]:w[1]]
    r = c.pct_change().fillna(0.0)
    inv = (~on.reindex(c.index).fillna(False)).astype(float).shift(1).fillna(1.0)
    cost = inv.diff().abs().fillna(0.0) * SWITCH_COST
    return (1 + inv * r - cost).cumprod() * 100


def proxy_stats(close: pd.Series, on: pd.Series, w) -> dict:
    eq = proxy(close, on, w)
    m = performance(eq)
    inv = (~on.reindex(eq.index).fillna(False)).astype(int)
    years = len(eq) / 252
    return {"cagr": m["cagr"], "max_drawdown": m["max_drawdown"], "calmar": m["cagr"] / abs(m["max_drawdown"]),
            "time_in": float(inv.mean()), "switches_per_year": float(inv.diff().abs().sum() / years)}


def crashes(close: pd.Series, min_drop: float) -> list[dict]:
    """Peak-to-trough drops of `min_drop` or more (closes), with the recovery date."""
    out, peak_i, trough_i, in_crash = [], 0, 0, False
    v = close.to_numpy()
    for i in range(1, len(v)):
        if v[i] >= v[peak_i]:
            if in_crash:
                out.append((peak_i, trough_i, i))
                in_crash = False
            peak_i = trough_i = i
        elif v[i] < v[trough_i]:
            trough_i = i
            if v[i] / v[peak_i] - 1 <= -min_drop:
                in_crash = True
    if in_crash:
        out.append((peak_i, trough_i, None))
    idx = close.index
    return [{"peak": idx[p].date().isoformat(), "trough": idx[t].date().isoformat(),
             "recovered": idx[r].date().isoformat() if r is not None else None, "drop": float(v[t] / v[p] - 1)}
            for p, t, r in out]


def window_return(eq: pd.Series, a, b) -> float:
    e = eq.loc[a:b]
    return float(e.iloc[-1] / e.iloc[0] - 1) if len(e) > 1 else np.nan


def window_drawdown(eq: pd.Series, a, b) -> float:
    e = eq.loc[a:b]
    return float((e / e.cummax() - 1).min()) if len(e) > 1 else np.nan


# ------------------------------------------------------------------ gauges
def gauges(ndx: pd.Series, spx: pd.Series) -> dict:
    """Facts about how stretched the Nasdaq 100 is, against its own history since 1993."""
    ext = (ndx / sma(ndx, 200) - 1).dropna()
    run3 = (ndx / ndx.shift(756) - 1).dropna()
    rel3 = ((ndx / spx.reindex(ndx.index)) / (ndx / spx.reindex(ndx.index)).shift(756) - 1).dropna()
    fwd = {h: ndx.shift(-h) / ndx - 1 for h in (126, 252)}
    top = (ext >= ext.quantile(0.9)).reindex(ndx.index, fill_value=False)
    after = {}
    for h, f in fwd.items():
        x = f[top].dropna()
        after[f"{h // 21}m"] = {"median": float(x.median()), "down": float((x < 0).mean()), "worst": float(x.min()),
                                "n_days": int(len(x))}
    base = {f"{h // 21}m": {"median": float(f.dropna().median()), "down": float((f.dropna() < 0).mean())}
            for h, f in fwd.items()}
    bubble = slice("1999-01-01", "2000-03-31")
    return {"ext200_deciles": [float(ext.quantile(q)) for q in np.linspace(0, 1, 11)],
            "ext200_now": float(ext.iloc[-1]), "ext200_pct_now": float((ext < ext.iloc[-1]).mean()),
            "ext200_peak_2000": float(ext.loc[bubble].max()), "ext200_at_top_2000": float(ext.loc["2000-03-10"]),
            "run3_now": float(run3.iloc[-1]), "run3_at_top_2000": float(run3.loc["2000-03-10"]),
            "run3_pct_now": float((run3 < run3.iloc[-1]).mean()),
            "rel3_now": float(rel3.iloc[-1]), "rel3_at_top_2000": float(rel3.loc["2000-03-10"]),
            "after_top_decile_ext": after, "all_days": base, "as_of": ext.index[-1].date().isoformat()}


# ------------------------------------------------------------------ the plan with a shield
def plan_with(shield: pd.Series | None, work: Path) -> tuple[dict, dict]:
    """The whole plan (top5_research.combined) with the top-5 list rebuilt without picks made
    on shield days, and no EPS buys / options plays opened after them."""
    from .accuracy_research import arrays, research_setup
    from . import top5_research as t5
    acc = json.loads((RESULTS_DIR / "accuracy" / "summary.json").read_text())
    sm = json.loads((t5.OUT / "summary.json").read_text())
    px, syms, eligible, _, events, _ = research_setup()
    idx = px["close"].index
    cache = work / "candidates.pkl"
    if cache.exists():
        c = pd.read_pickle(cache)
    else:
        c = t5.candidates(px, acc, eligible, events, no_earnings=sm.get("no_earnings", False))
        c.to_pickle(cache)
    if shield is not None:
        on = shield.reindex(idx).ffill().fillna(False).astype(bool)
        c = c[~on.reindex(pd.DatetimeIndex(c.date)).to_numpy(dtype=bool)]
    picks = t5.pick_daily(c, acc, idx)
    tr = t5.trades_for(px, arrays(px, syms), syms, picks)
    if sm.get("earnings_exit"):
        tr = t5.sell_before_earnings(px, tr, events)
    src = work / ("shield" if shield is not None else "base")
    src.mkdir(exist_ok=True)
    picks.to_csv(src / "picks.csv", index=False)
    tr.to_csv(src / "trades.csv", index=False)
    on = None if shield is None else shield.reindex(idx).ffill().fillna(False).astype(bool)
    return t5.combined(src=src, shield=on, save=False)


# ------------------------------------------------------------------ crash exits for open picks
EXITS = {   # name -> (trigger, argument); market triggers are seen at a close and act at the next open
    "sell every open pick at the next open once the crash shield turns on": ("shield", "all"),
    "sell the open picks that are below their buy price once the crash shield turns on": ("shield", "losers"),
    "sell every open pick at the next open after the S&P 500 falls 7%+ in 5 sessions": ("fast", "all"),
    "sell the losing picks at the next open after the S&P 500 falls 7%+ in 5 sessions": ("fast", "losers"),
    "sell a pick at the next open after it falls 8%+ in one session": ("drop", 0.08),
    "sell a pick at the open when it opens 5%+ below the last close": ("gap", 0.05),
    "sell a pick at the open when it opens 8%+ below the last close": ("gap", 0.08),
}


FAST_DROP, FAST_DAYS = 0.07, 5


def fast_drop(close: pd.Series) -> pd.Series:
    """True after a close at least FAST_DROP below the close FAST_DAYS sessions earlier."""
    return (close / close.shift(FAST_DAYS) - 1 <= -FAST_DROP).fillna(False)


def market_trigger(kind: str, spx: pd.Series, shield: pd.Series) -> pd.Series:
    if kind == "shield":
        return shield.astype(bool)
    return fast_drop(spx)


def crash_exit(px: dict, tr: pd.DataFrame, spec: tuple, market: pd.Series | None = None) -> pd.DataFrame:
    """The same share trades with one crash exit added: a trade still open when the exit
    fires is sold at that open (or the next open, for signals seen at a close)."""
    from .accuracy_research import SLIP
    if not len(tr):
        return tr
    idx = px["close"].index
    kind, arg = spec
    O, C = px["open"].to_numpy(dtype=float), px["close"].to_numpy(dtype=float)
    col = {s: j for j, s in enumerate(px["close"].columns)}
    mk = None if market is None else market.reindex(idx).ffill().fillna(False).to_numpy(dtype=bool)
    t = tr.copy().reset_index(drop=True)
    a_, b_ = idx.get_indexer(pd.DatetimeIndex(t.entry)), idx.get_indexer(pd.DatetimeIndex(t.exit))
    ret, ex, why = t["return"].to_numpy(dtype=float).copy(), t["exit"].to_numpy().copy(), t["reason"].to_numpy(dtype=object).copy()
    for j, (s, a, b) in enumerate(zip(t.symbol, a_, b_)):
        k = col.get(s)
        if k is None or a < 0 or b < 0 or not np.isfinite(O[a, k]):
            continue
        entry = O[a, k] * (1 + SLIP)
        sell = None
        if kind in ("shield", "fast"):
            for d in range(a, b):
                if mk[d] and (arg == "all" or C[d, k] < entry):
                    sell = d + 1
                    break
        elif kind == "drop":
            for d in range(a, b):
                ref = C[d - 1, k] if d > a else entry
                if C[d, k] / ref - 1 <= -arg:
                    sell = d + 1
                    break
        elif kind == "gap":
            for d in range(a + 1, b + 1):
                if O[d, k] / C[d - 1, k] - 1 <= -arg:
                    sell = d
                    break
        if sell is not None and sell <= b and np.isfinite(O[sell, k]):
            ret[j] = O[sell, k] * (1 - SLIP) / entry - 1
            ex[j] = idx[sell]
            why[j] = "crash"
    t["return"], t["exit"], t["reason"] = ret, pd.DatetimeIndex(ex), why
    return t


def test_exits():
    """Each crash exit on the plan as it runs now (with the crash shield), chosen on 2011-2021."""
    from .daily_research import DESIGN, TEST
    from . import top5_research as t5
    sm = json.loads((OUT / "summary.json").read_text())
    spx, ndx = load_index("SPX"), load_index("NDX")
    ndx = ndx.reindex(spx.index).ffill().dropna()
    spx = spx.loc[ndx.index]
    on = shield_series(sm["shield"]["rule"], spx, ndx)
    work = Path(tempfile.mkdtemp(prefix="exits_"))
    res = {}
    try:
        base, base_eq = plan_with(on, work)
        res["none (the shield only stops new buys)"] = (base, base_eq)
        src = work / "shield"
        from .accuracy_research import research_setup
        px = research_setup()[0]
        idx = px["close"].index
        shield_px = on.reindex(idx).ffill().fillna(False).astype(bool)
        for name, spec in EXITS.items():
            market = market_trigger(spec[0], spx, on) if spec[0] in ("shield", "fast") else None
            adj = (lambda p, tr, spec=spec, market=market: crash_exit(p, tr, spec, market))
            res[name] = t5.combined(src=src, shield=shield_px, save=False, adjust=adj)
            print(name, json.dumps({w: {k: res[name][0][w][k] for k in ("cagr", "max_drawdown")} for w in ("design", "test")}),
                  flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rows = []
    for name, (r, eq) in res.items():
        row = {"exit": name}
        for w in ("design", "test"):
            row |= {f"{w}_cagr": r[w]["cagr"], f"{w}_max_drawdown": r[w]["max_drawdown"], f"{w}_final": r[w]["final"],
                    f"{w}_win_rate": r[w]["win_rate"]}
        for cr in crashes(spx.loc["2011-02-01":], 0.10):
            w = "design" if cr["peak"] <= DESIGN[1] else "test"
            win = DESIGN if w == "design" else TEST
            a, b = max(cr["peak"], win[0]), min(cr["trough"], win[1])
            end = min(cr["recovered"] or win[1], win[1])
            row[f"crash {cr['peak'][:7]}"] = window_return(eq[w], a, b)
            row[f"crash {cr['peak'][:7]} to recovery"] = window_return(eq[w], a, end)
        rows.append(row)
    grid = pd.DataFrame(rows)
    grid.to_csv(OUT / "exit_grid.csv", index=False, float_format="%.4f")
    b0 = grid.iloc[0]
    tried = grid.iloc[1:].sort_values("design_cagr", ascending=False)
    best = tried.iloc[0]
    adopt = bool(best.design_cagr > b0.design_cagr or (best.design_max_drawdown - b0.design_max_drawdown >= MIN_DD_CUT
                                                       and b0.design_cagr - best.design_cagr <= MAX_CAGR_COST))
    sm["exits"] = {"best": best.exit, "spec": EXITS[best.exit], "use": adopt, "grid": grid.to_dict(orient="records"),
                   "result": res[best.exit][0]}
    (OUT / "summary.json").write_text(json.dumps(sm, indent=2, default=str))
    print(grid.round(4).to_string())
    print("best:", best.exit, "use:", adopt)


def current_plan():
    """The plan exactly as it runs (crash shield + the adopted crash exit), day by day over
    2011-2026: its return in every crash, every year and every week, next to the S&P 500
    (SPY) and Nasdaq 100 (QQQ). Writes plan_daily.csv and plan_weekly.csv."""
    from .daily_research import DESIGN, TEST
    from . import top5_research as t5
    from .accuracy_research import research_setup
    sm = json.loads((OUT / "summary.json").read_text())
    spx, ndx = load_index("SPX"), load_index("NDX")
    ndx = ndx.reindex(spx.index).ffill().dropna()
    spx = spx.loc[ndx.index]
    on = shield_series(sm["shield"]["rule"], spx, ndx)
    ex = sm.get("exits") or {}
    work = Path(tempfile.mkdtemp(prefix="plan_"))
    try:
        plan_with(on, work)                                   # builds the shielded top-5 list in work/shield
        px = research_setup()[0]
        idx = px["close"].index
        adj = None
        if ex.get("use"):
            spec = tuple(ex["spec"])
            market = market_trigger(spec[0], spx, on) if spec[0] in ("shield", "fast") else None
            adj = lambda p, tr: crash_exit(p, tr, spec, market)
        res, eq = t5.combined(src=work / "shield", shield=on.reindex(idx).ffill().fillna(False).astype(bool), save=False,
                              adjust=adj)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    r = pd.concat([eq["design"].pct_change(), eq["test"].pct_change()]).fillna(0.0)
    r = r[~r.index.duplicated()].sort_index()
    daily = pd.DataFrame({"plan": r})
    for sym in ("SPY", "QQQ"):
        daily[sym] = px["close"][sym].reindex(daily.index).pct_change().fillna(0.0)
    daily.iloc[0] = 0.0
    growth = (1 + daily).cumprod()
    growth.to_csv(OUT / "plan_daily.csv", float_format="%.6f")
    weekly = (1 + daily).groupby(daily.index.to_period("W-FRI")).prod() - 1
    weekly.index = [p.end_time.date().isoformat() for p in weekly.index]
    weekly.index.name = "week_ending"
    weekly.to_csv(OUT / "plan_weekly.csv", float_format="%.5f")
    yearly = (1 + daily).groupby(daily.index.year).prod() - 1
    out_crashes = []
    for cr in crashes(spx.loc["2011-02-01":], 0.10):
        a, b = cr["peak"], cr["trough"]
        end = cr["recovered"] or daily.index[-1].date().isoformat()
        g = growth.loc[a:b]
        out_crashes.append({**cr, "plan": float(g.plan.iloc[-1] / g.plan.iloc[0] - 1),
                            "spy": float(g.SPY.iloc[-1] / g.SPY.iloc[0] - 1), "qqq": float(g.QQQ.iloc[-1] / g.QQQ.iloc[0] - 1),
                            "plan_worst": float((growth.plan.loc[a:end] / growth.plan.loc[a:end].cummax() - 1).min()),
                            "plan_to_recovery": float(growth.plan.loc[end] / growth.plan.loc[a] - 1) if end in growth.index
                            else float(growth.plan.loc[:end].iloc[-1] / growth.plan.loc[a] - 1)})
    m = performance(growth.plan * CASH_START)
    sm["current_plan"] = {
        "label": "crash shield" + (" + crash exit" if adj else ""), "start": daily.index[0].date().isoformat(),
        "end": daily.index[-1].date().isoformat(), "results": res, "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
        "growth": {k: float(growth[k].iloc[-1]) for k in growth}, "yearly": {str(y): row.to_dict() for y, row in yearly.iterrows()},
        "crashes": out_crashes,
        "weeks": {"n": int(len(weekly)), "up": float((weekly.plan > 0).mean()), "best": float(weekly.plan.max()),
                  "worst": float(weekly.plan.min()), "best_week": weekly.plan.idxmax(), "worst_week": weekly.plan.idxmin(),
                  "beat_spy": float((weekly.plan > weekly.SPY).mean())}}
    (OUT / "summary.json").write_text(json.dumps(sm, indent=2, default=str))
    print(json.dumps({k: v for k, v in sm["current_plan"].items() if k != "results"}, indent=1, default=str))


CASH_START = 1_500_000


def main():
    from .daily_research import DESIGN, TEST
    OUT.mkdir(parents=True, exist_ok=True)
    spx, ndx = load_index("SPX"), load_index("NDX")
    ndx = ndx.reindex(spx.index).ffill().dropna()
    spx = spx.loc[ndx.index]
    summary = {"data": {"SPX": [spx.index[0].date().isoformat(), spx.index[-1].date().isoformat()],
                        "NDX": [ndx.index[0].date().isoformat(), ndx.index[-1].date().isoformat()]},
               "crashes": {"SPX": crashes(spx, 0.15), "NDX": crashes(ndx, 0.15)}}

    # 1-2. the shield, chosen on 1993-2010, then the same rule on 2011-2026
    never = pd.Series(False, index=spx.index)
    rows = []
    for name in ["none"] + list(RULES):
        on = never if name == "none" else shield_series(name, spx, ndx)
        r = {"rule": name}
        for lab, w in (("choose", CHOOSE), ("check", CHECK)):
            for sym, s in (("SPX", spx), ("NDX", ndx)):
                r |= {f"{lab}_{sym}_{k}": v for k, v in proxy_stats(s, on, w).items()}
            r[f"{lab}_calmar"] = (r[f"{lab}_SPX_calmar"] + r[f"{lab}_NDX_calmar"]) / 2
            r[f"{lab}_switches"] = (r[f"{lab}_SPX_switches_per_year"] + r[f"{lab}_NDX_switches_per_year"]) / 2
        rows.append(r)
    grid = pd.DataFrame(rows)
    grid.to_csv(OUT / "shield_grid.csv", index=False, float_format="%.4f")
    ok = grid[(grid.rule != "none") & (grid.choose_switches <= MAX_SWITCHES)]
    best = ok.sort_values("choose_calmar", ascending=False).iloc[0]
    name = best.rule
    on = shield_series(name, spx, ndx)
    summary["shield"] = {"rule": name, "spec": RULES[name], "grid": grid.to_dict(orient="records")}
    print("shield:", name, flush=True)

    # each crash: the index, and the index with the shield
    for sym, s in (("SPX", spx), ("NDX", ndx)):
        eq_s = proxy(s, on, (s.index[0], s.index[-1]))
        for cr in summary["crashes"][sym]:
            cr["with_shield"] = window_return(eq_s, cr["peak"], cr["trough"])
            end = cr["recovered"] or s.index[-1]
            cr["with_shield_to_recovery"] = window_return(eq_s, cr["peak"], end)
            cr["index_to_recovery"] = window_return(s, cr["peak"], end)

    summary["gauges"] = gauges(ndx, spx)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # 3. the plan, 2011-2026, without and with the shield
    work = Path(tempfile.mkdtemp(prefix="crash_"))
    try:
        base, base_eq = plan_with(None, work)
        shld, shld_eq = plan_with(on.astype(bool), work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    summary["plan"] = {"without": base, "with_shield": shld}
    d0, d1 = base["design"], shld["design"]
    adopt = bool(d1["cagr"] > d0["cagr"] or (d1["max_drawdown"] - d0["max_drawdown"] >= MIN_DD_CUT
                                             and d0["cagr"] - d1["cagr"] <= MAX_CAGR_COST))
    summary["shield"]["use"] = adopt
    plan_crashes = []
    for cr in crashes(spx.loc["2011-02-01":], 0.10):
        w = "design" if cr["peak"] <= DESIGN[1] else "test"
        a, b = max(cr["peak"], (DESIGN if w == "design" else TEST)[0]), min(cr["trough"], (DESIGN if w == "design" else TEST)[1])
        end = min(cr["recovered"] or TEST[1], (DESIGN if w == "design" else TEST)[1])
        plan_crashes.append({**cr, "window": w, "ndx": window_return(ndx, a, b),
                             "plan": window_return(base_eq[w], a, b), "plan_shield": window_return(shld_eq[w], a, b),
                             "plan_dd": window_drawdown(base_eq[w], a, end), "plan_shield_dd": window_drawdown(shld_eq[w], a, end)})
    summary["plan_crashes"] = plan_crashes
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("shield",)}, indent=1, default=str))
    print("use shield:", adopt)


if __name__ == "__main__":
    import sys
    if "--exits" in sys.argv:
        test_exits()
    elif "--current" in sys.argv:
        current_plan()
    else:
        main()
