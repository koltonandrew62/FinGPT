"""Does the momentum strategy beat the S&P 500 in BOTH uptrends and downtrends?

Market trend = SPY above / below its 200-day average at the previous close. Each
candidate's daily returns are split by that trend and compared with SPY's returns
on the same days, for 2011-2021 (stock list has hindsight) and 2021-2026 (clean),
with $1,500 in whole shares.

    python -m schwab_trader.regime_research
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .active_strategy import REGIMES, backtest_weights_fn
from .backtest import buy_and_hold, performance, run_backtest
from .config import ALL_SYMBOLS, BENCHMARK, DATA_DIR, RESULTS_DIR, SPLITS, StrategyConfig
from .data import load_prices
from .earnings_backtest import EARNINGS_UNIVERSE

WINDOWS = {"2011-2021": ("2011-02-01", "2021-10-22"), "2021-2026": ("2021-10-25", "2026-10-23")}
OUT = RESULTS_DIR / "regime"
NAMES = {"none": "A: momentum, ignores trend", "defensive": "B: downtrend -> defensive assets",
         "cash": "C: downtrend -> cash", "absolute": "D: downtrend -> rising names only"}


def by_trend(eq: pd.Series, spy: pd.Series, trend: pd.Series) -> dict:
    r, b = eq.pct_change().dropna(), spy.pct_change().dropna()
    out = {}
    for name in ("uptrend", "downtrend"):
        days = trend.reindex(r.index) == name
        n = int(days.sum())
        if n == 0:
            continue
        sr, br = (1 + r[days]).prod() - 1, (1 + b[days]).prod() - 1
        out[name] = {"days": n, "strategy": float(sr), "spy": float(br),
                     "strategy_per_yr": float((1 + sr) ** (252 / n) - 1), "spy_per_yr": float((1 + br) ** (252 / n) - 1)}
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    syms = list(dict.fromkeys(list(ALL_SYMBOLS) + list(EARNINGS_UNIVERSE) + ["IEF"]))
    prices = load_prices(syms)
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    spy_close = prices["close"][BENCHMARK]
    trend = (spy_close > spy_close.rolling(200).mean()).map({True: "uptrend", False: "downtrend"}).shift(1)
    report = {}
    for regime in REGIMES:
        rows = {}
        for wname, (a, b) in WINDOWS.items():
            eq = run_backtest(prices, StrategyConfig(), a, b, 1500.0, weights_fn=backtest_weights_fn(3, regime),
                              splits=splits).equity
            spy = buy_and_hold(prices, BENCHMARK, a, b, 1500.0)
            m, ms = performance(eq), performance(spy)
            rows[wname] = {"final": m["final_value"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                           "spy_cagr": ms["cagr"], "spy_max_drawdown": ms["max_drawdown"], "by_trend": by_trend(eq, spy, trend)}
        beats = all(t["strategy"] > t["spy"] for w in rows.values() for t in w["by_trend"].values())
        report[regime] = {"name": NAMES[regime], "beats_spy_in_every_trend_and_period": beats, **rows}
        print(f"\n{NAMES[regime]}   beats SPY in every trend & period: {beats}")
        for wname, w in rows.items():
            print(f"  {wname}: ${w['final']:>9,.0f} {w['cagr']:+6.1%}/yr maxDD {w['max_drawdown']:6.1%} "
                  f"(SPY {w['spy_cagr']:+.1%}, maxDD {w['spy_max_drawdown']:.1%})")
            for t, v in w["by_trend"].items():
                print(f"     {t:9s} {v['days']:4d} days: strategy {v['strategy']:+7.1%} vs SPY {v['spy']:+7.1%}  "
                      f"({v['strategy_per_yr']:+.0%}/yr vs {v['spy_per_yr']:+.0%}/yr)")
    (OUT / "summary.json").write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
