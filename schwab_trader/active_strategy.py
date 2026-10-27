"""The share strategies the weekly emails and the Schwab bot use: the winners of
share_competition.py at each account size, with a market-trend check.

  under LARGE_ACCOUNT ($25k)  momentum top 3 (59 stocks + 17 ETFs): won at $1,500
  LARGE_ACCOUNT and up        scorecard v2 top 2 (momentum + volatility score, 59 stocks):
                              won at $150,000 ($150k -> $687,531, +34.5%/yr, worst drop -52%);
                              $1.5M -> $6.71M (+33.8%/yr) with the trend check

Every week, rank 59 large US stocks and 17 ETFs by 12-month return skipping the
latest month and hold the top 3 in equal dollar amounts, skipping any whose single
share costs more than 1.5x a slot (so a $1,500 account takes the next-ranked name
instead). The market trend is the S&P 500 (SPY) vs. its 200-day average; in a
downtrend only names whose own 12-1 return is still positive are bought. Whole
shares only, no margin, no options.

Last 5 years (2021-10 .. 2026-10), $1,500 start: $6,398 (33.6%/yr) vs SPY $2,508
(10.8%/yr), worst drop -38%. It beat SPY on both uptrend days (+27%/yr vs +10%) and
downtrend days (+58%/yr vs +14%), and in 2011-2021 too (results/regime/RESULTS.md).
Switching to defensive assets or cash in downtrends did worse (missed the rebounds).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import UNIVERSES
from .earnings_backtest import EARNINGS_UNIVERSE

POOL = tuple(dict.fromkeys(tuple(EARNINGS_UNIVERSE) + tuple(UNIVERSES["sectors+"])))
DEFENSIVE = ("GLD", "TLT", "IEF", "XLP", "XLU", "XLV")   # what a downtrend rotates into
TOP_N = 3
REGIME = "absolute"   # downtrend rule chosen by regime_research.py
LABEL = "momentum top 3 (59 stocks + 17 ETFs, weekly, trend-checked)"
LARGE_ACCOUNT = 25_000.0
LARGE_TOP_N = 2
LARGE_LABEL = "scorecard v2 top 2 (momentum + volatility, 59 stocks, weekly, trend-checked)"
REGIMES = ("none", "defensive", "cash", "absolute")
# 2021-10 .. 2026-10 backtests quoted in the emails (share_competition.py, regime_research.py)
WORST_DROP = {LABEL: 0.38, LARGE_LABEL: 0.52}
BACKTEST = {
    LABEL: "$1,500 -> $6,398 (+33.6%/yr) vs S&P $2,508; beat the S&P in uptrends (+27%/yr vs +10%) "
           "and downtrends (+58%/yr vs +14%)",
    LARGE_LABEL: "$1.5M -> $6.71M (+33.8%/yr) vs S&P $2.51M; beat the S&P in uptrends (+21%/yr vs +10%) "
                 "and downtrends (+85%/yr vs +14%); worst drop -52% (about -$3.0M from the peak)",
}


def label_for(account_value: float) -> str:
    return LARGE_LABEL if account_value >= LARGE_ACCOUNT else LABEL


def market_trend(closes: pd.DataFrame, symbol: str = "SPY", days: int = 200) -> str:
    """'uptrend' when the S&P 500 (SPY) closed above its 200-day average, else 'downtrend'."""
    spy = closes[symbol].dropna()
    return "uptrend" if spy.iloc[-1] > spy.iloc[-days:].mean() else "downtrend"


def momentum(closes: pd.DataFrame, pool=POOL) -> pd.Series:
    """12-1 momentum on the last row, for names with a full year of history; best first."""
    px = closes[[s for s in pool if s in closes]]
    if len(px) < 253:
        return pd.Series(dtype=float)
    enough = px.notna().tail(253).sum() >= 253
    mom = px.iloc[-22] / px.iloc[-253] - 1
    return mom[enough].dropna().sort_values(ascending=False)


def scorecard_ranking(closes: pd.DataFrame, volumes: pd.DataFrame) -> pd.Series:
    """Scorecard v2 totals (0-100) for the latest row: buy-grade names only, best first."""
    from .scorecard import BUY_MIN, VETO, WEIGHTS_V2, components, total_score
    stocks = [s for s in EARNINGS_UNIVERSE if s in closes and s in volumes]
    comp = components(closes[stocks].iloc[-300:], volumes[stocks].iloc[-300:])
    score = total_score(comp, weights=WEIGHTS_V2).iloc[-1]
    ok = pd.concat([comp[k].iloc[-1] >= VETO for k in WEIGHTS_V2], axis=1).all(axis=1)
    return score[ok & (score >= BUY_MIN)].dropna().sort_values(ascending=False)


def target_weights_for(closes: pd.DataFrame, volumes: pd.DataFrame | None, account_value: float, real_price) -> dict:
    """The size-appropriate strategy's picks (falls back to momentum top 3 without volume data)."""
    if account_value >= LARGE_ACCOUNT and volumes is not None:
        return target_weights(closes, account_value, real_price, LARGE_TOP_N, ranked=scorecard_ranking(closes, volumes))
    return target_weights(closes, account_value, real_price)


def target_weights(closes: pd.DataFrame, account_value: float, real_price, top_n: int = TOP_N,
                   regime: str = REGIME, ranked: pd.Series | None = None) -> dict:
    """{symbol: weight} best first. `real_price(sym)` is the actual price per share now;
    `ranked` overrides the momentum ranking (e.g. with scorecard_ranking()).

    regime: what to do when the market is in a downtrend (SPY below its 200-day average):
      none       ignore the trend
      defensive  hold the top_n rising defensive assets (gold, Treasuries, staples, utilities, health care)
      cash       hold cash
      absolute   keep momentum picks, but only names still rising over 12-1 months
    """
    slot = account_value / top_n
    custom = ranked is not None
    ranked = momentum(closes) if ranked is None else ranked
    if regime != "none" and market_trend(closes) == "downtrend":
        if regime == "cash":
            return {}
        if regime == "defensive":
            ranked = momentum(closes, DEFENSIVE)
        rising = momentum(closes) > 0
        ranked = ranked[rising.reindex(ranked.index).fillna(False)] if custom else ranked[ranked > 0]
    picks = []
    for sym in ranked.index:
        px = real_price(sym)
        if px is not None and np.isfinite(px) and px <= 1.5 * slot:
            picks.append(sym)
        if len(picks) == top_n:
            break
    return {s: 1.0 / top_n for s in picks}


def backtest_weights_fn(top_n: int = TOP_N, regime: str = REGIME):
    """weights_fn for backtest.run_backtest."""
    def fn(history, account_value, real_price):
        return target_weights(history, account_value, real_price, top_n, regime)
    fn.wants_budget = True
    return fn


def large_account_weights_fn(volumes: pd.DataFrame):
    """weights_fn for backtest.run_backtest running the large-account strategy."""
    def fn(history, account_value, real_price):
        return target_weights(history, account_value, real_price, LARGE_TOP_N,
                              ranked=scorecard_ranking(history, volumes.loc[:history.index[-1]]))
    fn.wants_budget = True
    return fn
