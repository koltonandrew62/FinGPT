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

    python -m schwab_trader.crash_research
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
    main()
