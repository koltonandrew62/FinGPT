"""The share strategy the weekly emails and the Schwab bot use: the winner of
share_competition.py ("stocks+ETFs momentum top 3"), with a market-trend check.

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
REGIMES = ("none", "defensive", "cash", "absolute")


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


def target_weights(closes: pd.DataFrame, account_value: float, real_price, top_n: int = TOP_N,
                   regime: str = REGIME) -> dict:
    """{symbol: weight} best first. `real_price(sym)` is the actual price per share now.

    regime: what to do when the market is in a downtrend (SPY below its 200-day average):
      none       ignore the trend
      defensive  hold the top_n rising defensive assets (gold, Treasuries, staples, utilities, health care)
      cash       hold cash
      absolute   keep momentum picks, but only names still rising over 12-1 months
    """
    slot = account_value / top_n
    ranked = momentum(closes)
    if regime != "none" and market_trend(closes) == "downtrend":
        if regime == "cash":
            return {}
        if regime == "defensive":
            ranked = momentum(closes, DEFENSIVE)
        ranked = ranked[ranked > 0]
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
