"""Does the weekly 0-100 scorecard work? Fixed weights, no tuning.

The stock universe is the largest US companies as of October 2021, so only the
test window that starts then (2021-10 .. 2026-10) is free of hindsight; the
2011-2021 numbers favour stocks that later became giants and are shown for
reference only.

    python -m schwab_trader.scorecard_research
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .backtest import buy_and_hold, performance, run_backtest, yearly_returns
from .config import BENCHMARK, DATA_DIR, RESULTS_DIR, StrategyConfig
from .data import load_prices
from .earnings_backtest import EARNINGS_UNIVERSE
from .scorecard import BUY_MIN, VETO, WEIGHTS_V1, WEIGHTS_V2, components, long_weights_fn, total_score

OUT = RESULTS_DIR / "scorecard"
TEST = ("2021-10-25", "2026-10-23")
DESIGN = ("2011-02-01", "2021-10-22")


def forward_returns(opens: pd.DataFrame, weeks: int) -> pd.DataFrame:
    """Return from the open of the first session after each Friday to the open `weeks` later."""
    first = opens.index.to_series().groupby(opens.index.to_period("W-SUN")).first()
    o = opens.loc[first.values]
    fwd = o.shift(-weeks) / o - 1
    # index by the signal date (last close before the entry open)
    prev = opens.index.to_series().shift(1).loc[first.values]
    fwd.index = pd.DatetimeIndex(prev.values)
    return fwd.dropna(how="all")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    prices = load_prices(list(EARNINGS_UNIVERSE) + [BENCHMARK])
    splits = json.loads((DATA_DIR / "splits.json").read_text())
    stocks = list(EARNINGS_UNIVERSE)
    comp = components(prices["close"][stocks], prices["volume"][stocks])
    report = {}
    for version, weights in (("v1", WEIGHTS_V1), ("v2", WEIGHTS_V2)):
        report[version] = evaluate(prices, splits, stocks, comp, weights)
    (OUT / "summary.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


def evaluate(prices, splits, stocks, comp, weights) -> dict:
    total = total_score(comp, weights=weights)
    failed = pd.concat([(comp[k] < VETO) for k in weights]).groupby(level=0).any()

    # 1. Does a higher score lead to higher returns? Forward 4-week return by score bucket.
    fwd4 = forward_returns(prices["open"][stocks], 4)
    spy4 = forward_returns(prices["open"][[BENCHMARK]], 4)[BENCHMARK]
    rows = []
    for date in fwd4.index:
        if not (pd.Timestamp(TEST[0]) <= date <= pd.Timestamp(TEST[1])) or date not in total.index:
            continue
        s, r = total.loc[date], fwd4.loc[date]
        ok = s.notna() & r.notna()
        for sym in s[ok].index:
            rows.append({"date": date, "symbol": sym, "score": s[sym], "rejected": bool(failed.loc[date, sym]),
                         "fwd4": r[sym], "excess_vs_spy": r[sym] - spy4.get(date, np.nan)})
    panel = pd.DataFrame(rows)
    panel["bucket"] = pd.cut(panel.score, [0, 20, 35, 50, 60, 70, 80, 100], include_lowest=True)
    buckets = panel.groupby("bucket", observed=True).agg(n=("fwd4", "size"), avg_4wk=("fwd4", "mean"),
                                                         avg_excess_vs_spy=("excess_vs_spy", "mean"),
                                                         pct_beat_spy=("excess_vs_spy", lambda x: (x > 0).mean()))
    by_reject = panel[panel.score >= BUY_MIN].groupby("rejected").agg(n=("fwd4", "size"), avg_4wk=("fwd4", "mean"),
                                                                       avg_excess_vs_spy=("excess_vs_spy", "mean"))
    # 2. Put list (the scorecard's own PUT/SHORT rule) vs everything else.
    put_rows = []
    for date, grp in panel.groupby("date"):
        c = {k: v.loc[date] for k, v in comp.items()}
        mask = (total.loc[date] <= 35) & (c["trend"] <= 30) & (c["momentum"] <= 30)
        picks = set(mask[mask].index) & set(grp.symbol)
        if picks:
            put_rows.append({"date": date, "n": len(picks), "put_list_4wk": grp[grp.symbol.isin(picks)].fwd4.mean(),
                             "universe_4wk": grp.fwd4.mean(), "spy_4wk": spy4.get(date, np.nan)})
    puts = pd.DataFrame(put_rows)

    # 2b. What would buying a ~1-month at-the-money put on each put-list stock have done?
    from .options_backtest import bs_price, half_spread
    opens = prices["open"][stocks]
    vol63 = np.log(prices["close"][stocks]).diff().rolling(63).std() * np.sqrt(252)
    first = opens.index.to_series().groupby(opens.index.to_period("W-SUN")).first()
    put_trades = []
    for date, grp in panel.groupby("date"):
        c = {k: v.loc[date] for k, v in comp.items()}
        mask = (total.loc[date] <= 35) & (c["trend"] <= 30) & (c["momentum"] <= 30)
        entry_i = opens.index.searchsorted(date) + 1
        if entry_i + 20 >= len(opens):
            continue
        for sym in set(mask[mask].index) & set(grp.symbol):
            s0, s4 = opens[sym].iloc[entry_i], opens[sym].iloc[entry_i + 20]
            if not (np.isfinite(s0) and np.isfinite(s4)):
                continue
            k = round(s0)
            row = {"date": date, "symbol": sym, "short_return": -(s4 / s0 - 1)}
            for label, mult in (("put_no_premium", 1.0), ("put_typical_premium", 1.25)):
                mid = bs_price(s0, k, 21 / 252, float(vol63.at[date, sym]) * mult, 0.03, False, 0.0)
                ask = mid + half_spread(mid)
                payoff = max(0.0, k - s4)
                bid = payoff - half_spread(payoff) if payoff > 0.005 else 0.0
                row[label] = (bid * 100 - 0.65) / (ask * 100 + 0.65) - 1
            put_trades.append(row)
    put_trades = pd.DataFrame(put_trades)

    # 3. Weekly top-5 portfolio, with and without the reject rule, for both account sizes.
    results = {}
    for cash in (1500.0, 1_500_000.0):
        for veto in (True, False):
            res = run_backtest(prices, StrategyConfig(), *TEST, cash,
                               weights_fn=long_weights_fn(comp, 5, veto, weights), splits=splits)
            results[f"top5 {'veto' if veto else 'no-veto'} ${cash:,.0f}"] = res
    spy = buy_and_hold(prices, BENCHMARK, *TEST, 1500.0)
    design = run_backtest(prices, StrategyConfig(), *DESIGN, 1_500_000.0,
                          weights_fn=long_weights_fn(comp, 5, True, weights), splits=splits)
    spy_design = buy_and_hold(prices, BENCHMARK, *DESIGN, 1500.0)

    summary = {
        "score_buckets_test": buckets.round(4).reset_index().astype({"bucket": str}).to_dict("records"),
        "buy_grade_rejected_vs_not_test": by_reject.round(4).reset_index().to_dict("records"),
        "put_list_test": {"weeks_with_puts": len(puts), "avg_names": float(puts.n.mean()) if len(puts) else 0,
                          "put_list_avg_4wk": float(puts.put_list_4wk.mean()), "universe_avg_4wk": float(puts.universe_4wk.mean()),
                          "spy_avg_4wk": float(puts.spy_4wk.mean()),
                          "pct_weeks_put_list_lagged_universe": float((puts.put_list_4wk < puts.universe_4wk).mean()),
                          "trades": len(put_trades),
                          "buy_1m_put_avg_return_no_premium": float(put_trades.put_no_premium.mean()),
                          "buy_1m_put_avg_return_typical_premium": float(put_trades.put_typical_premium.mean()),
                          "buy_1m_put_win_rate_typical_premium": float((put_trades.put_typical_premium > 0).mean()),
                          "short_sale_avg_4wk_return": float(put_trades.short_return.mean())},
        "portfolios_test": {k: {**performance(v.equity), "trades": len(v.trades)} for k, v in results.items()},
        "spy_test": performance(spy),
        "yearly_test": pd.DataFrame({"top5 veto": yearly_returns(results["top5 veto $1,500,000"].equity),
                                     "spy": yearly_returns(spy)}).to_dict("index"),
        "design_reference_biased": {"top5 veto $1.5M": performance(design.equity), "spy": performance(spy_design)},
    }
    tag = "v2" if weights is WEIGHTS_V2 else "v1"
    pd.DataFrame({k: v.equity for k, v in results.items()} | {"spy": spy}).to_csv(OUT / f"test_equity_{tag}.csv",
                                                                               float_format="%.2f")
    return summary


if __name__ == "__main__":
    main()
