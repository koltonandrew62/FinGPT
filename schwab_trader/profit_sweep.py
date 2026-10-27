"""$1,500 in the momentum share strategy, with half of each month's new profit moved
into an options sleeve (buying options only: a cash account can't sell spreads).

Each month-end, if the share account is above its previous high, 50% of the gain over
that high is moved to the options sleeve. The sleeve runs one of the long-option rules
from the options studies, but only trades when its per-trade budget covers one real
contract (otherwise it sits in cash). Daily returns of each piece come from the full
backtests; the share account is simulated at $1,500 and the option rules at scale.

    python -m schwab_trader.profit_sweep
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .active_strategy import backtest_weights_fn
from .backtest import buy_and_hold, performance, run_backtest
from .config import ALL_SYMBOLS, BENCHMARK, DATA_DIR, RESULTS_DIR, SPLITS, StrategyConfig
from .data import load_prices
from .earnings_backtest import EARNINGS_UNIVERSE
from .options_backtest import OptionsConfig, run_options_backtest
from . import zero_dte_research as z

WINDOWS = {"5y test (2021-10..2026-10)": ("2021-10-25", "2026-10-23"),
           "10y (2016-10..2026-10, stock list has hindsight before 2021)": ("2016-10-24", "2026-10-23")}
OUT = RESULTS_DIR / "profit_sweep"
TARGET = 2.0   # 200% a year


def sweep(shares_ret: pd.Series, opt_ret: pd.Series, opt_min_budget: pd.Series, risk: float,
          start_cash: float = 1500.0, share: float = 0.5) -> pd.DataFrame:
    s_val, o_val, hwm = start_cash, 0.0, start_cash
    rows = []
    for i, day in enumerate(shares_ret.index):
        s_val *= 1 + shares_ret.iat[i]
        can_trade = o_val * risk >= opt_min_budget.get(day, np.inf)
        o_val *= 1 + (opt_ret.get(day, 0.0) if can_trade else 0.0)
        month_end = i + 1 == len(shares_ret) or shares_ret.index[i + 1].month != day.month
        if month_end and s_val > hwm:
            moved = share * (s_val - hwm)
            s_val -= moved
            o_val += moved
            hwm = s_val
        rows.append((day, s_val, o_val))
    return pd.DataFrame(rows, columns=["date", "shares", "options"]).set_index("date")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    prices = load_prices(list(dict.fromkeys(list(ALL_SYMBOLS) + list(EARNINGS_UNIVERSE))))
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    ctx = z.context()
    q, vix_q, probs, mults, dm = ctx["q"], ctx["vix"], ctx["probs"], ctx["mults"], ctx["design_mask"]
    spy_ov = pd.DataFrame({"open": prices["open"][BENCHMARK], "close": prices["close"][BENCHMARK]})
    vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")[["open", "close"]]
    report = {}
    for wname, (start, end) in WINDOWS.items():
        shares = run_backtest(prices, StrategyConfig(), start, end, 1500.0, weights_fn=backtest_weights_fn(),
                              splits=splits).equity
        s_ret = shares.pct_change().fillna(0.0)
        spy = performance(buy_and_hold(prices, BENCHMARK, start, end, 1500.0))
        out = {"shares only": {**performance(shares), "options_share": 0.0}, "spy": spy}
        # Sleeve A: 2-month at-the-money SPY calls, 10% of the sleeve per trade (best long-option rule at $1.5M).
        # Sleeve B: 0DTE QQQ, buy in the direction of the opening gap on the detector's top-10% days, 10% per trade.
        thr = probs["complex"][dm & probs["complex"].notna()].quantile(0.9)
        flags = (probs["complex"] >= thr)
        for pricing in ("mid", "informed" if True else None):
            sleeves = {}
            cfg = OptionsConfig(signal="calls", frequency="daily", dte=42, hold=21, otm=0.0, risk=0.10,
                                pricing="mid" if pricing == "informed" else pricing)
            eq_a, _, _ = run_options_backtest(spy_ov, vix, cfg, start, end, initial_cash=1_500_000.0)
            # one 2-month ATM SPY call costs roughly 4% of SPY's price x 100
            sleeves["2-month SPY calls"] = (eq_a.pct_change().fillna(0.0), 0.04 * spy_ov["close"] * 100)
            eq_b, _ = z.run_0dte(q, vix_q["open"], flags & (q.index >= pd.Timestamp(start)), "follow_gap", 0.10,
                                 mults[pricing], start, end, 1_500_000.0)
            # one at-the-money 0DTE QQQ option costs roughly 0.5% of QQQ's price x 100
            sleeves["0DTE QQQ on volatile days"] = (eq_b.pct_change().fillna(0.0), 0.005 * q["open"] * 100)
            for sname, (o_ret, min_budget) in sleeves.items():
                path = sweep(s_ret, o_ret.reindex(s_ret.index).fillna(0.0), min_budget, 0.10)
                total = path.shares + path.options
                m = performance(total)
                out[f"half profits -> {sname} [{pricing} pricing]"] = {
                    **m, "options_share": float(path.options.iloc[-1] / total.iloc[-1])}
        years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
        out["200%/yr target"] = {"final_value": 1500.0 * (1 + TARGET) ** years, "cagr": TARGET}
        report[wname] = out
        print(f"\n== {wname}")
        for k, v in out.items():
            print(f"  {k:60s} ${v['final_value']:>14,.0f}  {v['cagr']:+7.1%}/yr"
                  + (f"  maxDD {v['max_drawdown']:6.1%}" if "max_drawdown" in v else "")
                  + (f"  options {v['options_share']:.0%}" if "options_share" in v else ""))
    (OUT / "summary.json").write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
