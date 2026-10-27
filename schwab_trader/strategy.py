"""Signal and position-sizing logic. Pure functions, no I/O, so the backtest,
the live trader and the weekly report all make identical decisions."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import BENCHMARK, LOOKBACK_SCHEMES, SPLITS, StrategyConfig


@dataclass
class Signal:
    asof: pd.Timestamp
    scores: pd.Series                         # composite score per eligible fund, best first
    momentum: pd.Series                       # raw trailing return used by the absolute filter
    weights: dict = field(default_factory=dict)   # symbol -> target weight (rest is cash)
    risk_off: list = field(default_factory=list)  # top-ranked funds parked in cash by the filter
    spy_above_sma: bool | None = None


def factor_table(close: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Per-fund signal inputs from closes up to and including the last row."""
    missing = [s for s in cfg.symbols if s not in close]
    if missing:   # never silently trade a smaller universe than the one tested
        raise ValueError(f"price history missing for {missing}")
    rows = {}
    for sym in cfg.symbols:
        if np.isnan(close[sym].iloc[-1]):   # fund not trading yet (e.g. XLC before 2018)
            continue
        px = close[sym].dropna().to_numpy()
        if len(px) < cfg.min_history:
            continue
        if cfg.lookback == "12-1":
            mom = px[-22] / px[-253] - 1.0
        else:
            mom = float(np.mean([px[-1] / px[-1 - n] - 1.0 for n in LOOKBACK_SCHEMES[cfg.lookback]]))
        daily = np.diff(px[-cfg.vol_days - 1:]) / px[-cfg.vol_days - 1:-1]
        rows[sym] = {"momentum": mom, "vol": daily.std() * np.sqrt(252), "ret_1w": px[-1] / px[-6] - 1.0}
    return pd.DataFrame.from_dict(rows, orient="index", columns=["momentum", "vol", "ret_1w"])


def composite_scores(table: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    if table.empty:
        return pd.Series(dtype=float)
    trend = table["momentum"] / table["vol"] if cfg.risk_adjusted else table["momentum"]
    if not cfg.reversal_weight:
        return trend.sort_values(ascending=False)
    # Mix signals on a common scale: cross-sectional percentile ranks.
    score = trend.rank(pct=True) + cfg.reversal_weight * (-table["ret_1w"]).rank(pct=True)
    return score.sort_values(ascending=False)


def compute_signal(close: pd.DataFrame, cfg: StrategyConfig) -> Signal:
    """Target weights from closes up to the signal date (no look-ahead: the caller
    passes only history it would have had)."""
    table = factor_table(close, cfg)
    scores = composite_scores(table, cfg)
    sig = Signal(asof=close.index[-1], scores=scores, momentum=table["momentum"])
    picks = list(scores.index[: cfg.top_n])
    slot = 1.0 / cfg.top_n

    if cfg.trend_filter == "spy_sma":
        spy = close[BENCHMARK].dropna()
        sig.spy_above_sma = bool(spy.iloc[-1] > spy.iloc[-cfg.sma_days:].mean())
        if not sig.spy_above_sma:
            sig.risk_off = picks
            return sig
    for sym in picks:
        if cfg.trend_filter == "absolute" and sig.momentum[sym] <= 0:
            sig.risk_off.append(sym)
        else:
            sig.weights[sym] = slot
    return sig


def split_factor(symbol: str, date, splits: dict | None = None) -> float:
    """Real share price / split-adjusted price on `date`."""
    date = pd.Timestamp(date)
    factor = 1.0
    for when, ratio in (SPLITS if splits is None else splits).get(symbol, []):
        if date < pd.Timestamp(when):
            factor *= ratio
    return factor


def whole_share_targets(weights: dict, equity: float, prices: dict, cash_buffer: float = 0.003,
                        max_overweight: float = 1.5) -> dict:
    """Turn target weights into whole-share counts at real per-share `prices`.

    Floors each position, then spends what's left one share at a time in weight
    order (best-ranked first, dict order) so little cash sits idle in a small
    account. No position may exceed `max_overweight` x its dollar target, and
    cash freed by the trend filter stays in cash.
    """
    alloc = {s: equity * w * (1.0 - cash_buffer) for s, w in weights.items()}
    shares = {s: int(alloc[s] // prices[s]) for s in weights}
    leftover = sum(alloc.values()) - sum(shares[s] * prices[s] for s in weights)
    added = True
    while added:
        added = False
        for s in weights:
            p = prices[s]
            if p <= leftover and (shares[s] + 1) * p <= alloc[s] * max_overweight:
                shares[s] += 1
                leftover -= p
                added = True
    return shares


def is_rebalance_day(prev_date, date, rebalance: str) -> bool:
    """True when `date` is the first trading day of a new month/week."""
    prev_date, date = pd.Timestamp(prev_date), pd.Timestamp(date)
    if rebalance == "monthly":
        return (prev_date.year, prev_date.month) != (date.year, date.month)
    if rebalance == "weekly":
        return prev_date.isocalendar()[:2] != date.isocalendar()[:2]
    raise ValueError(f"unknown rebalance frequency {rebalance!r}")
