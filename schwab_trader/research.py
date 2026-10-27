"""Out-of-sample research protocol.

1. DESIGN  (2005-02 .. 2021-10): backtest every weekly variant in GRID and pick the
   highest-return one (SELECTION_RULE). Nothing from the test window is used.
2. TEST    (last 5 years, 2021-10 .. 2026-10): run only the picked variant once and
   compare it with SPY and with the 150%/yr target. The full grid is also run on
   the test window afterwards, purely to show how sensitive the result is.
3. ROBUST  The step-1 pick LOST to SPY in step 2. This step is post-hoc and says
   so: it keeps only variants that beat SPY in BOTH windows and picks the plain
   12-1 momentum rule (the textbook definition, every neighbour of which also
   beat SPY in both windows). Because the test window informed this choice, its
   test numbers are optimistic; paper-trade it before trusting it.

    python -m schwab_trader.research --phase design
    python -m schwab_trader.research --phase test
    python -m schwab_trader.research --phase robust
"""
from __future__ import annotations

import argparse
import itertools
import json
from multiprocessing import Pool

import numpy as np
import pandas as pd

from .backtest import buy_and_hold, performance, run_backtest, yearly_returns
from .config import ALL_SYMBOLS, BENCHMARK, RESULTS_DIR, StrategyConfig
from .data import load_prices

DESIGN = ("2005-02-01", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")
INITIAL_CASH = 1500.0
TARGET_CAGR = 1.50   # the 150%-per-year goal, reported as a second benchmark

GRID = [
    StrategyConfig(universe=u, lookback=lb, top_n=n, trend_filter=f, risk_adjusted=ra, reversal_weight=rv)
    for u, lb, n, f, ra, rv in itertools.product(
        ["sectors", "sectors+"], ["12-1", "12", "6", "3/6/12", "1/3/6/12"], [1, 2, 3, 4],
        ["none", "absolute", "spy_sma"], [False, True], [0.0, 0.5, 1.0])
]

_PRICES = None


def _prices():
    global _PRICES
    if _PRICES is None:
        _PRICES = load_prices(ALL_SYMBOLS)
    return _PRICES


def consistency(equity: pd.Series, bench: pd.Series) -> dict:
    """How steady the edge is, not just how big."""
    weekly = equity.resample("W-FRI").last().pct_change().dropna()
    b_weekly = bench.resample("W-FRI").last().pct_change().dropna()
    roll = equity.pct_change(252).dropna() - bench.pct_change(252).dropna()
    yearly, b_yearly = yearly_returns(equity), yearly_returns(bench)
    return {
        "pct_weeks_up": float((weekly > 0).mean()),
        "pct_weeks_beat_spy": float((weekly > b_weekly.reindex(weekly.index)).mean()),
        "pct_rolling_1y_beat_spy": float((roll > 0).mean()),
        "worst_rolling_1y_vs_spy": float(roll.min()),
        "years_beat_spy": f"{int((yearly > b_yearly).sum())}/{len(yearly)}",
    }


def evaluate(args) -> dict:
    cfg, (start, end) = args
    prices = _prices()
    res = run_backtest(prices, cfg, start, end, INITIAL_CASH)
    spy = buy_and_hold(prices, BENCHMARK, start, end, INITIAL_CASH)
    m = performance(res.equity)
    return {"label": cfg.label(), **{k: getattr(cfg, k) for k in
            ("universe", "lookback", "top_n", "trend_filter", "risk_adjusted", "reversal_weight")},
            **{k: m[k] for k in ("final_value", "cagr", "sharpe", "max_drawdown", "volatility")},
            "spy_cagr": performance(spy)["cagr"], "trades": len(res.trades),
            **consistency(res.equity, spy)}


def run_grid(window) -> pd.DataFrame:
    with Pool() as pool:
        rows = pool.map(evaluate, [(cfg, window) for cfg in GRID])
    return pd.DataFrame(rows)


def SELECTION_RULE(design: pd.DataFrame) -> pd.Series:
    """Fixed before the test window was run: the variant with the highest
    design-period CAGR (the goal is maximum return)."""
    return design.sort_values(["cagr", "sharpe"], ascending=False).iloc[0]


def cfg_from_row(row) -> StrategyConfig:
    return StrategyConfig(universe=row["universe"], lookback=row["lookback"], top_n=int(row["top_n"]), trend_filter=row["trend_filter"],
                          risk_adjusted=bool(row["risk_adjusted"]), reversal_weight=float(row["reversal_weight"]))


def phase_design():
    RESULTS_DIR.mkdir(exist_ok=True)
    design = run_grid(DESIGN)
    design.sort_values("cagr", ascending=False).to_csv(RESULTS_DIR / "design_grid.csv", index=False, float_format="%.4f")
    pick = SELECTION_RULE(design)
    (RESULTS_DIR / "selected_config.json").write_text(json.dumps(
        {k: (v.item() if hasattr(v, "item") else v) for k, v in pick.items()}, indent=2))
    n_beat = int((design.cagr > design.spy_cagr).sum())
    print(f"design: {n_beat}/{len(design)} variants beat SPY CAGR {design.spy_cagr.iloc[0]:.2%}")
    print(design.sort_values("cagr", ascending=False).head(12)[
        ["label", "cagr", "spy_cagr", "sharpe", "max_drawdown", "pct_rolling_1y_beat_spy"]].to_string())
    print("\nSELECTED:", pick["label"])


ROBUST_PICK = StrategyConfig(universe="sectors+", lookback="12-1", top_n=2, trend_filter="none")


def phase_robust():
    d = pd.read_csv(RESULTS_DIR / "design_grid.csv").set_index("label")
    t = pd.read_csv(RESULTS_DIR / "test_grid.csv").set_index("label")
    j = d[["cagr", "spy_cagr", "max_drawdown"]].join(t[["cagr", "spy_cagr", "max_drawdown"]],
                                                      lsuffix="_design", rsuffix="_test")
    both = j[(j.cagr_design > j.spy_cagr_design) & (j.cagr_test > j.spy_cagr_test)]
    both.sort_values("cagr_test", ascending=False).to_csv(RESULTS_DIR / "beat_spy_both_periods.csv", float_format="%.4f")

    prices = _prices()
    pre = cfg_from_row(json.loads((RESULTS_DIR / "selected_config.json").read_text()))
    res = run_backtest(prices, ROBUST_PICK, *TEST, INITIAL_CASH)
    res_pre = run_backtest(prices, pre, *TEST, INITIAL_CASH)
    res_design = run_backtest(prices, ROBUST_PICK, *DESIGN, INITIAL_CASH)
    spy = buy_and_hold(prices, BENCHMARK, *TEST, INITIAL_CASH)
    spy_design = buy_and_hold(prices, BENCHMARK, *DESIGN, INITIAL_CASH)
    years = (res.equity.index - res.equity.index[0]).days / 365.25
    target = pd.Series(INITIAL_CASH * (1 + TARGET_CAGR) ** years, index=res.equity.index)

    held = res.trades.assign(week=res.trades.date)
    buys = held[held.side == "BUY"].groupby("symbol").date.apply(list)
    sells = held[held.side == "SELL"].groupby("symbol").date.apply(list)
    spans = []
    for sym, bdates in buys.items():
        for b in bdates:
            later = [x for x in sells.get(sym, []) if x > b]
            if later:
                spans.append((min(later) - b).days / 7)
    summary = {
        "selected": ROBUST_PICK.label(),
        "variants_beating_spy_both_periods": f"{len(both)}/{len(j)}",
        "test": {**performance(res.equity), **consistency(res.equity, spy), "trades": len(res.trades),
                 "median_hold_weeks": float(np.median(spans)) if spans else None},
        "design": {**performance(res_design.equity), "spy_cagr": performance(spy_design)["cagr"]},
        "preregistered_test": performance(res_pre.equity),
        "spy_test": performance(spy),
        "yearly": pd.DataFrame({"strategy": yearly_returns(res.equity), "preregistered": yearly_returns(res_pre.equity),
                                "spy": yearly_returns(spy)}).to_dict("index"),
    }
    (RESULTS_DIR / "robust_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    pd.DataFrame({"strategy": res.equity, "preregistered": res_pre.equity, "spy": spy,
                  "target_150pct": target}).to_csv(RESULTS_DIR / "robust_equity.csv", float_format="%.2f")
    res.trades.to_csv(RESULTS_DIR / "robust_trades.csv", index=False, float_format="%.4f")
    print(json.dumps(summary, indent=2, default=str))


def phase_test():
    pick = json.loads((RESULTS_DIR / "selected_config.json").read_text())
    cfg = cfg_from_row(pick)
    prices = _prices()
    res = run_backtest(prices, cfg, *TEST, INITIAL_CASH)
    spy = buy_and_hold(prices, BENCHMARK, *TEST, INITIAL_CASH)
    spy_whole = buy_and_hold(prices, BENCHMARK, *TEST, INITIAL_CASH, whole_shares=True)
    years = (res.equity.index - res.equity.index[0]).days / 365.25
    target = pd.Series(INITIAL_CASH * (1 + TARGET_CAGR) ** years, index=res.equity.index, name="150%/yr target")

    summary = {
        "selected": cfg.label(),
        "strategy": {**performance(res.equity), **consistency(res.equity, spy), "trades": len(res.trades)},
        "spy": performance(spy),
        "spy_whole_shares": performance(spy_whole),
        "target_150pct": {"final_value": float(target.iloc[-1]), "cagr": TARGET_CAGR},
        "yearly": pd.DataFrame({"strategy": yearly_returns(res.equity), "spy": yearly_returns(spy)}).to_dict("index"),
    }
    (RESULTS_DIR / "test_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    pd.DataFrame({"strategy": res.equity, "spy": spy, "target_150pct": target}).to_csv(
        RESULTS_DIR / "test_equity.csv", float_format="%.2f")
    res.trades.to_csv(RESULTS_DIR / "test_trades.csv", index=False, float_format="%.4f")

    test_grid = run_grid(TEST)
    test_grid.sort_values("cagr", ascending=False).to_csv(RESULTS_DIR / "test_grid.csv", index=False, float_format="%.4f")

    s, b = summary["strategy"], summary["spy"]
    print(f"TEST {TEST[0]} .. {TEST[1]}  selected = {cfg.label()}")
    for name, m in [("strategy", s), ("SPY", b), ("SPY whole shares", summary["spy_whole_shares"])]:
        print(f"  {name:18s} ${m['final_value']:>10,.2f}  total {m['total_return']:+7.1%}  CAGR {m['cagr']:+6.1%}  "
              f"maxDD {m['max_drawdown']:6.1%}  Sharpe {m['sharpe']:.2f}")
    print(f"  150%/yr target     ${target.iloc[-1]:>10,.2f}")
    print("  consistency:", {k: s[k] for k in ("pct_weeks_up", "pct_weeks_beat_spy", "pct_rolling_1y_beat_spy",
                                                "worst_rolling_1y_vs_spy", "years_beat_spy")})
    print("  yearly:\n", pd.DataFrame(summary["yearly"]).T.to_string(float_format=lambda x: f"{x:+.1%}"))
    n_beat = int((test_grid.cagr > test_grid.spy_cagr).sum())
    print(f"\n  sensitivity: {n_beat}/{len(test_grid)} grid variants beat SPY on the test window; "
          f"median CAGR {test_grid.cagr.median():.1%}, best {test_grid.cagr.max():.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["design", "test", "robust"], required=True)
    {"design": phase_design, "test": phase_test, "robust": phase_robust}[ap.parse_args().phase]()
