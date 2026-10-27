"""Which share-only strategy turns $1,500 into the most money?

Every candidate trades weekly in whole shares at real prices with $1,500, and the
stock-picking ones skip stocks whose share price the account can't afford (taking the
next-ranked one instead). Rule fixed before running: the winner is the candidate with
the highest final value on 2021-10 .. 2026-10 among those that ALSO beat SPY on
2011-02 .. 2021-10. The winner becomes the strategy in the weekly emails.

Caveat: the stock list is the largest companies as of October 2021, so 2011-2021
flatters the stock-picking candidates (it already "knows" which firms became giants);
the 2021-2026 test is clean.

    python -m schwab_trader.share_competition
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .backtest import buy_and_hold, performance, run_backtest
from .config import ALL_SYMBOLS, BENCHMARK, DATA_DIR, RESULTS_DIR, SPLITS, UNIVERSES, StrategyConfig
from .data import load_prices
from .earnings_backtest import EARNINGS_UNIVERSE
from .scorecard import BUY_MIN, VETO, WEIGHTS_V2, components, total_score

DESIGN = ("2011-02-01", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")
CASH = 1500.0
OUT = RESULTS_DIR / "share_competition"


def momentum_12_1(close: pd.DataFrame) -> pd.DataFrame:
    enough = close.notna().rolling(253).sum() >= 253
    return (close.shift(21) / close.shift(252) - 1).where(enough)


def ranked_weights_fn(score: pd.DataFrame, top_n: int):
    """Equal-weight the top_n scores each week, skipping shares the account can't afford."""
    def fn(history, account_value, real_price):
        row = score.loc[history.index[-1]].dropna().sort_values(ascending=False)
        slot = account_value / top_n
        picks = []
        for sym in row.index:
            px = real_price(sym)
            if np.isfinite(px) and px <= 1.5 * slot:
                picks.append(sym)
            if len(picks) == top_n:
                break
        return {s: 1.0 / top_n for s in picks}
    fn.wants_budget = True
    return fn


def candidates(prices: dict) -> dict:
    stocks = list(EARNINGS_UNIVERSE)
    etfs = list(UNIVERSES["sectors+"])
    close = prices["close"]
    mom = momentum_12_1(close)
    comp = components(close[stocks], prices["volume"][stocks])
    score = total_score(comp, weights=WEIGHTS_V2)
    failed = pd.concat([(comp[k] < VETO) for k in WEIGHTS_V2]).groupby(level=0).any()
    card = score.where(~failed & (score >= BUY_MIN))
    out = {}
    for n in (1, 2, 3):
        out[f"ETF rotation top{n}"] = {"cfg": StrategyConfig(universe="sectors+", lookback="12-1", top_n=n,
                                                              trend_filter="none")}
    for n in (1, 2, 3, 5):
        out[f"stock momentum top{n}"] = {"fn": ranked_weights_fn(mom[stocks], n)}
        out[f"scorecard v2 top{n}"] = {"fn": ranked_weights_fn(card, n)}
    for n in (1, 2, 3):
        out[f"stocks+ETFs momentum top{n}"] = {"fn": ranked_weights_fn(mom[stocks + etfs], n)}
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    prices = load_prices(list(dict.fromkeys(list(ALL_SYMBOLS) + list(EARNINGS_UNIVERSE))))
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    spy = {w: buy_and_hold(prices, BENCHMARK, *w, CASH) for w in (DESIGN, TEST)}
    spy_m = {w: performance(e) for w, e in spy.items()}
    rows, curves = [], {}
    for name, c in candidates(prices).items():
        res = {}
        for w in (DESIGN, TEST):
            r = run_backtest(prices, c.get("cfg", StrategyConfig()), *w, CASH, weights_fn=c.get("fn"), splits=splits)
            res[w] = r
        d, t = performance(res[DESIGN].equity), performance(res[TEST].equity)
        curves[name] = res[TEST].equity
        rows.append({"strategy": name, "design_cagr": d["cagr"], "design_maxdd": d["max_drawdown"],
                     "test_final": t["final_value"], "test_cagr": t["cagr"], "test_maxdd": t["max_drawdown"],
                     "test_trades": len(res[TEST].trades),
                     "beat_spy_both": d["cagr"] > spy_m[DESIGN]["cagr"] and t["cagr"] > spy_m[TEST]["cagr"]})
        print(f"{name:28s} design {d['cagr']:+6.1%}  test ${t['final_value']:>8,.0f} {t['cagr']:+6.1%}  "
              f"maxDD {t['max_drawdown']:6.1%}  both={rows[-1]['beat_spy_both']}", flush=True)
    table = pd.DataFrame(rows).sort_values("test_final", ascending=False)
    eligible = table[table.beat_spy_both]
    winner = eligible.iloc[0].strategy if len(eligible) else None
    table.to_csv(OUT / "results.csv", index=False, float_format="%.4f")
    pd.DataFrame(curves | {"SPY": spy[TEST]}).to_csv(OUT / "test_equity.csv", float_format="%.2f")
    summary = {"winner": winner, "spy_design_cagr": spy_m[DESIGN]["cagr"], "spy_test_final": spy_m[TEST]["final_value"],
               "spy_test_cagr": spy_m[TEST]["cagr"], "table": table.to_dict("records")}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nSPY: design {spy_m[DESIGN]['cagr']:+.1%}, test ${spy_m[TEST]['final_value']:,.0f} "
          f"{spy_m[TEST]['cagr']:+.1%}\nWINNER: {winner}")


if __name__ == "__main__":
    main()
