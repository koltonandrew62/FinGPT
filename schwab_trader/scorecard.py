"""Weekly stock scorecard: each stock gets a 0-100 score from several components,
and ANY failing component rejects it as a buy. The weakest stocks, where trend
and momentum agree they are falling, form the put/short list.

Components (0 = worst, 100 = best), all from data up to Friday's close:
  trend        price vs. its 50- and 200-day averages
  momentum     12-month return skipping the latest month, ranked within the universe
  rel_strength 3-month return, ranked within the universe
  rsi          14-day RSI; best near 60, penalised when oversold (<30) or overheated (>85)
  volatility   63-day volatility, ranked (calmer = higher)
  volume       up-day vs. down-day volume over 20 days (accumulation), ranked
  drawdown     distance below the 52-week high (0 at -30% or worse)
  cash_flow    the earnings play book's cash-flow-led score, when SEC data is available

V1 (WEIGHTS_V1) uses all of the above with weights fixed in advance. It failed out of
sample: its total score did not predict next-month returns. V2 (the default) keeps
only what predicted returns on 2011-2021 data: momentum, and volatility counted the
other way round (more volatile scored higher). See results/scorecard/RESULTS.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WEIGHTS_V1 = {"trend": 0.20, "momentum": 0.20, "rel_strength": 0.15, "rsi": 0.10,
              "volatility": 0.10, "volume": 0.10, "drawdown": 0.15}
WEIGHTS_V2 = {"momentum": 0.50, "high_volatility": 0.50}
WEIGHTS = WEIGHTS_V2
CASH_FLOW_WEIGHT = 0.25    # when available, the other weights shrink to make room
VETO = 25                  # any component below this rejects the stock as a buy
BUY_MIN = 60               # total score needed to be a buy
PUT_MAX, PUT_TREND, PUT_MOMENTUM = 35, 30, 30   # put list: weak total AND weak trend AND weak momentum


def _rank(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True) * 100


def _rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def components(close: pd.DataFrame, volume: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Every component for every date (rows) and stock (columns); NaN until a stock has a year of history."""
    enough = close.notna().rolling(253).sum() >= 253
    sma50, sma200 = close.rolling(50).mean(), close.rolling(200).mean()
    trend = 50 + 50 * np.tanh(((close / sma50 - 1) + (close / sma200 - 1)) / 0.10)
    mom = close.shift(21) / close.shift(252) - 1
    ret63 = close / close.shift(63) - 1
    rsi = _rsi(close)
    rsi_score = (100 - 2.5 * (rsi - 60).abs()).clip(0, 100)
    rsi_score = rsi_score.where(rsi <= 85, 0)
    vol = np.log(close).diff().rolling(63).std() * np.sqrt(252)
    signed = np.sign(close.diff()) * volume
    accumulation = signed.rolling(20).sum() / volume.rolling(20).sum()
    dd = close / close.rolling(252).max() - 1
    out = {
        "trend": trend, "momentum": _rank(mom), "rel_strength": _rank(ret63), "rsi": rsi_score,
        "volatility": 100 - _rank(vol), "high_volatility": _rank(vol), "volume": _rank(accumulation),
        "drawdown": (100 * (1 + dd / 0.30)).clip(0, 100),
    }
    return {k: v.where(enough) for k, v in out.items()}


def total_score(comp: dict[str, pd.DataFrame], cash_flow: pd.DataFrame | None = None,
                weights: dict | None = None) -> pd.DataFrame:
    weights = dict(weights or WEIGHTS)
    parts = dict(comp)
    if cash_flow is not None:
        weights = {k: w * (1 - CASH_FLOW_WEIGHT) for k, w in weights.items()} | {"cash_flow": CASH_FLOW_WEIGHT}
        parts["cash_flow"] = cash_flow
    num = sum(parts[k].fillna(0) * w for k, w in weights.items() if k in parts)
    den = sum(parts[k].notna() * w for k, w in weights.items() if k in parts)
    return (num / den.replace(0, np.nan)).where(comp["trend"].notna())


def weekly_table(comp: dict[str, pd.DataFrame], date, cash_flow: pd.Series | None = None,
                 weights: dict | None = None) -> pd.DataFrame:
    """One row per stock for `date`: the scored components, total, failed components and verdict."""
    weights_in = weights or WEIGHTS
    rows = pd.DataFrame({k: comp[k].loc[date] for k in weights_in} | {"trend": comp["trend"].loc[date],
                                                                       "momentum": comp["momentum"].loc[date]})
    if cash_flow is not None:
        rows["cash_flow"] = cash_flow.reindex(rows.index)
    rows = rows.dropna(subset=["trend"])
    names = list(weights_in) + (["cash_flow"] if cash_flow is not None else [])
    w = {k: weights_in.get(k, 0.0) for k in names}
    if "cash_flow" in rows:
        w = {k: v * (1 - CASH_FLOW_WEIGHT) for k, v in w.items()} | {"cash_flow": CASH_FLOW_WEIGHT}
    weights = pd.Series(w)
    rows["score"] = (rows[names].fillna(0) * weights).sum(axis=1) / (rows[names].notna() * weights).sum(axis=1)
    rows["failed"] = rows[names].apply(lambda r: ", ".join(k for k in names if pd.notna(r[k]) and r[k] < VETO), axis=1)
    put = (rows.score <= PUT_MAX) & (rows.trend <= PUT_TREND) & (rows.momentum <= PUT_MOMENTUM)
    rows["verdict"] = np.where(put, "PUT/SHORT", np.where(rows.failed != "", "REJECT",
                               np.where(rows.score >= BUY_MIN, "BUY", "PASS")))
    return rows.sort_values("score", ascending=False)


def long_weights_fn(comp: dict[str, pd.DataFrame], top_n: int = 5, veto: bool = True, weights: dict | None = None):
    """weights_fn for backtest.run_backtest: equal-weight the top_n buys each week."""
    weights = weights or WEIGHTS
    total = total_score(comp, weights=weights)
    failed = pd.concat([(comp[k] < VETO) for k in weights]).groupby(level=0).any() if veto else None

    def fn(history: pd.DataFrame) -> dict:
        date = history.index[-1]
        s = total.loc[date].dropna()
        if veto:
            s = s[~failed.loc[date].reindex(s.index).fillna(False)]
        s = s[s >= BUY_MIN].sort_values(ascending=False).head(top_n)
        return {sym: 1.0 / top_n for sym in s.index}
    return fn


def put_list(comp: dict[str, pd.DataFrame], date, weights: dict | None = None) -> list:
    t = weekly_table(comp, date, weights=weights)
    return list(t[t.verdict == "PUT/SHORT"].sort_values("score").index)
