"""Volatile-day detector for QQQ: will today's open-to-close move be big?

Every feature is known at 9:30 AM ET (yesterday's bars plus today's opening price
and opening VIX), so the model can be run right after the open, before buying a
0DTE option. Two feature sets:

  simple   VIX level and overnight change, QQQ's overnight gap, yesterday's move and
           range, 14-day ATR, 5- and 20-day realized volatility, day of week,
           first-Friday-of-the-month (jobs report)
  complex  simple + Bollinger band width, RSI, MACD histogram, distance from the 20/50/
           200-day averages, up/down streak, distance from the 52-week high/low,
           yesterday's volume vs. its 20-day average, 5-day VIX change, VIX vs. its
           20-day average, 5-day return, QQQ vs. SPY divergence

Label: |ln(close / open)| at or above BIG_MOVE_QUANTILE of design-period days.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BIG_MOVE_QUANTILE = 0.80   # "volatile day" = one of the biggest 20% of open-to-close moves
SIMPLE = ["vix_open", "vix_gap", "gap", "prev_move", "prev_range", "atr14", "rv5", "rv20", "dow", "first_friday"]
COMPLEX = SIMPLE + ["bb_width", "rsi_dist", "macd_hist", "d_sma20", "d_sma50", "d_sma200", "streak", "d_high252",
                    "d_low252", "vol_ratio", "vix_chg5", "vix_vs_sma20", "ret5", "qqq_spy_div"]


def features(qqq: pd.DataFrame, vix: pd.DataFrame, spy: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per trading day; row t uses only data available at the open of day t."""
    o, h, l, c, v = (qqq[k] for k in ("open", "high", "low", "close", "volume"))
    vx = vix.reindex(qqq.index).ffill()
    pc = c.shift(1)                                  # yesterday's close
    ret = np.log(c / pc)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    f = pd.DataFrame(index=qqq.index)
    f["vix_open"] = vx["open"]
    f["vix_gap"] = vx["open"] / vx["close"].shift(1) - 1
    f["gap"] = (o / pc - 1).abs()
    f["prev_move"] = np.log(c / o).abs().shift(1)
    f["prev_range"] = ((h - l) / c).shift(1)
    f["atr14"] = (tr.rolling(14).mean() / c).shift(1)
    f["rv5"] = ret.rolling(5).std().shift(1)
    f["rv20"] = ret.rolling(20).std().shift(1)
    f["dow"] = qqq.index.dayofweek
    f["first_friday"] = ((qqq.index.dayofweek == 4) & (qqq.index.day <= 7)).astype(int)
    sma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    f["bb_width"] = (4 * sd20 / sma20).shift(1)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    f["rsi_dist"] = ((100 - 100 / (1 + gain / loss)) - 50).abs().shift(1)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    f["macd_hist"] = ((macd - macd.ewm(span=9, adjust=False).mean()) / c).shift(1)
    for n in (20, 50, 200):
        f[f"d_sma{n}"] = (c / c.rolling(n).mean() - 1).shift(1)
    up = np.sign(c.diff())
    f["streak"] = up.groupby((up != up.shift()).cumsum()).cumsum().shift(1)
    f["d_high252"] = (c / c.rolling(252).max() - 1).shift(1)
    f["d_low252"] = (c / c.rolling(252).min() - 1).shift(1)
    f["vol_ratio"] = (v / v.rolling(20).mean()).shift(1)
    f["vix_chg5"] = (vx["close"] / vx["close"].shift(5) - 1).shift(1)
    f["vix_vs_sma20"] = (vx["close"] / vx["close"].rolling(20).mean() - 1).shift(1)
    f["ret5"] = (c / c.shift(5) - 1).shift(1)
    if spy is not None:
        s = spy["close"].reindex(qqq.index)
        f["qqq_spy_div"] = (np.log(c / c.shift(5)) - np.log(s / s.shift(5))).shift(1)
    else:
        f["qqq_spy_div"] = 0.0
    return f


def target(qqq: pd.DataFrame) -> pd.Series:
    """Size of today's open-to-close move (what a 0DTE straddle bought at the open pays off on)."""
    return np.log(qqq["close"] / qqq["open"]).abs()


def make_model(kind: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "simple":
        return make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, C=1.0))
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=300, l2_regularization=1.0,
                                          random_state=0)


def fit(kind: str, X: pd.DataFrame, y: pd.Series):
    cols = SIMPLE if kind == "simple" else COMPLEX
    ok = X[cols].notna().all(axis=1) & y.notna()
    model = make_model(kind).fit(X.loc[ok, cols], y[ok])
    return model, cols


def predict(model, cols, X: pd.DataFrame) -> pd.Series:
    ok = X[cols].notna().all(axis=1)
    p = pd.Series(np.nan, index=X.index)
    p[ok] = model.predict_proba(X.loc[ok, cols])[:, 1]
    return p
