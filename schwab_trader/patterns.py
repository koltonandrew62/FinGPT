"""Market trends and patterns learned from price history. Everything is rebuilt from the
full history on every run, so each new trading day is added to what the program
"remembers".

  trend_label()      up / down / sideways over 20, 50 and 200 days for any price series
  pattern_frames()   chart patterns (breakouts, crosses, dips, gaps, volume surges)
  pattern_stats()    how each pattern did afterwards: buy the next open, hold 5 or 20 days
  todays_patterns()  which stocks show a pattern on the latest day, with its track record
  market_features()  what is known at 8:30 AM about the market (trend, VIX, Asia overnight)
  analog_forecast()  the most similar past mornings and what SPY/QQQ did from the open
  asia_overnight()   overnight move of US-listed Asian stock funds (their opening gap)
  asia_impact()      how much each US sector/stock opens with Asia (beta), and whether
                     the US day continued or reversed after big Asian moves
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ASIA = {"EWJ": "Japan", "FXI": "China large caps", "MCHI": "China", "EWH": "Hong Kong",
        "EWY": "South Korea", "EWT": "Taiwan", "EWA": "Australia", "INDA": "India"}
ASIA_ADRS = {"TSM": "Taiwan Semiconductor", "BABA": "Alibaba"}
SECTORS = {"XLK": "Technology", "XLF": "Financials", "XLV": "Health care", "XLE": "Energy",
           "XLI": "Industrials", "XLY": "Consumer discretionary", "XLP": "Consumer staples",
           "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate", "XLC": "Communication"}
FEATURES = ["spy_r1", "spy_r5", "spy_r20", "spy_d50", "spy_d200", "spy_rsi2", "rv20", "vix", "vix_chg5", "asia"]


# ------------------------------------------------------------------ trends
def trend_label(close: pd.Series) -> dict:
    """{'short': .., 'medium': .., 'long': ..} each 'up', 'down' or 'sideways'."""
    c = close.dropna()
    out = {}
    for name, n, slope_days in (("short", 20, 5), ("medium", 50, 10), ("long", 200, 20)):
        if len(c) < n + slope_days:
            out[name] = "n/a"
            continue
        sma = c.rolling(n).mean()
        rising = sma.iloc[-1] > sma.iloc[-1 - slope_days]
        if c.iloc[-1] > sma.iloc[-1] and rising:
            out[name] = "up"
        elif c.iloc[-1] < sma.iloc[-1] and not rising:
            out[name] = "down"
        else:
            out[name] = "sideways"
    return out


# ---------------------------------------------------------------- patterns
def _rsi2(close: pd.DataFrame) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def pattern_frames(px: dict, symbols) -> dict[str, pd.DataFrame]:
    """{pattern: bool frame (dates x symbols)}, each evaluated at that day's close."""
    c, o, v = px["close"][symbols], px["open"][symbols], px["volume"][symbols]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    hi252 = c.rolling(252).max()
    ret = c / c.shift(1) - 1
    gap = o / c.shift(1) - 1
    vol_ratio = v / v.rolling(50).median().shift(1)
    rsi2 = _rsi2(c)
    up = c > sma200
    pats = {
        "new 52-week high": (c >= hi252) & (c.shift(1) < hi252.shift(1)),
        "golden cross (50d above 200d)": (sma50 > sma200) & (sma50.shift(1) <= sma200.shift(1)),
        "death cross (50d below 200d)": (sma50 < sma200) & (sma50.shift(1) >= sma200.shift(1)),
        "sharp dip in uptrend (RSI2<5)": up & (rsi2 < 5),
        "pullback to 50-day in uptrend": up & (c.shift(10) > 1.05 * sma50.shift(10)) & (c <= 1.01 * sma50) & (c >= 0.98 * sma50),
        "gap up 2%+ and held": (gap >= 0.02) & (c > o),
        "gap down 2%+ and fell": (gap <= -0.02) & (c < o),
        "volume surge up day": (vol_ratio >= 2) & (ret >= 0.02),
        "volume surge down day": (vol_ratio >= 2) & (ret <= -0.02),
        "5 down days in a row": (ret < 0).rolling(5).sum() == 5,
        "oversold in downtrend (RSI2<5)": ~up & sma200.notna() & (rsi2 < 5),
    }
    return {k: f.fillna(False).astype(bool) for k, f in pats.items()}


def forward_returns(px: dict, symbols, days: int) -> pd.DataFrame:
    """Buy at the next open, sell at the close `days` sessions later."""
    c, o = px["close"][symbols], px["open"][symbols]
    return c.shift(-days) / o.shift(-1) - 1


def pattern_stats(frames: dict, fwd: dict, start=None, end=None) -> pd.DataFrame:
    """One row per pattern: signals, win rate and average return after 5 and 20 days,
    next to the same numbers for every stock-day (the base rate)."""
    rows = []
    def window(f):
        return f.loc[pd.Timestamp(start) if start else f.index[0]:pd.Timestamp(end) if end else f.index[-1]]
    for name, f in frames.items():
        row = {"pattern": name}
        for h, r in fwd.items():
            vals = window(r)[window(f)].stack()
            vals = vals[np.isfinite(vals)]
            row |= {f"n_{h}d": int(len(vals)), f"win_{h}d": float((vals > 0).mean()) if len(vals) else np.nan,
                    f"avg_{h}d": float(vals.mean()) if len(vals) else np.nan}
        rows.append(row)
    base = {"pattern": "(every stock, every day)"}
    for h, r in fwd.items():
        vals = window(r).stack()
        vals = vals[np.isfinite(vals)]
        base |= {f"n_{h}d": int(len(vals)), f"win_{h}d": float((vals > 0).mean()), f"avg_{h}d": float(vals.mean())}
    return pd.DataFrame(rows + [base]).set_index("pattern")


def todays_patterns(frames: dict, stats: pd.DataFrame, date=None) -> list[dict]:
    """[{symbol, pattern, win_5d, avg_5d, win_20d, avg_20d, n}] for the latest (or given) day."""
    out = []
    for name, f in frames.items():
        day = f.index[-1] if date is None else pd.Timestamp(date)
        hits = f.loc[day]
        for sym in hits[hits].index:
            s = stats.loc[name]
            out.append({"symbol": sym, "pattern": name, "win_5d": s.win_5d, "avg_5d": s.avg_5d,
                        "win_20d": s.win_20d, "avg_20d": s.avg_20d, "n": int(s.n_5d)})
    return out


# ------------------------------------------------------------------- Asia
def asia_overnight(px: dict, funds=tuple(ASIA)) -> pd.DataFrame:
    """Each Asian fund's gap (US open vs prior US close) and their average ('asia').
    The gap is how US traders price that day's Asian session; live, the 8 AM
    pre-market price stands in for the open."""
    funds = [f for f in funds if f in px["close"]]
    gaps = px["open"][funds] / px["close"][funds].shift(1) - 1
    gaps["asia"] = gaps.mean(axis=1, skipna=True)
    return gaps


def asia_impact(px: dict, symbols, asia: pd.Series, start=None, end=None, big: float = 0.01) -> pd.DataFrame:
    """Per US symbol: beta and correlation of its opening gap to Asia's overnight move,
    and on big Asian moves (|move| >= `big`), how often the US open->close moved the
    same way (continuation) and the average open->close move in Asia's direction."""
    rows = []
    sl = slice(pd.Timestamp(start) if start else None, pd.Timestamp(end) if end else None)
    a = asia.loc[sl]
    for s in symbols:
        if s not in px["close"]:
            continue
        gap = (px["open"][s] / px["close"][s].shift(1) - 1).loc[sl]
        oc = (px["close"][s] / px["open"][s] - 1).loc[sl]
        ok = a.notna() & gap.notna()
        if ok.sum() < 100:
            continue
        x, y = a[ok], gap[ok]
        beta = float(np.cov(x, y)[0, 1] / x.var())
        bigd = ok & (a.abs() >= big) & oc.notna()
        same = np.sign(oc[bigd]) == np.sign(a[bigd])
        rows.append({"symbol": s, "beta": beta, "corr": float(x.corr(y)), "big_days": int(bigd.sum()),
                     "continuation_rate": float(same.mean()) if bigd.any() else np.nan,
                     "avg_oc_in_asia_direction": float((oc[bigd] * np.sign(a[bigd])).mean()) if bigd.any() else np.nan})
    return pd.DataFrame(rows).set_index("symbol")


# ---------------------------------------------------------------- analogs
def market_features(px: dict, vix: pd.Series, asia: pd.Series) -> pd.DataFrame:
    """One row per trading day t with what is known before t's open: SPY's trend and
    momentum through t-1's close, VIX at t-1's close, and Asia's overnight move into t."""
    spy = px["close"]["SPY"]
    prev = spy.shift(1)
    f = pd.DataFrame(index=spy.index)
    f["spy_r1"] = prev / spy.shift(2) - 1
    f["spy_r5"] = prev / spy.shift(6) - 1
    f["spy_r20"] = prev / spy.shift(21) - 1
    f["spy_d50"] = prev / prev.rolling(50).mean() - 1
    f["spy_d200"] = prev / prev.rolling(200).mean() - 1
    f["spy_rsi2"] = _rsi2(spy.to_frame())["SPY"].shift(1)
    f["rv20"] = np.log(spy).diff().rolling(20).std().shift(1) * np.sqrt(252)
    v = vix.reindex(spy.index).ffill()
    f["vix"] = v.shift(1)
    f["vix_chg5"] = v.shift(1) / v.shift(6) - 1
    f["asia"] = asia.reindex(spy.index)
    return f


def market_outcomes(px: dict) -> pd.DataFrame:
    c, o = px["close"], px["open"]
    return pd.DataFrame({"spy_oc": c["SPY"] / o["SPY"] - 1, "qqq_oc": c["QQQ"] / o["QQQ"] - 1,
                         "spy_5d": c["SPY"].shift(-4) / o["SPY"] - 1})


def analog_forecast(X: pd.DataFrame, y: pd.Series, day, k: int = 50, gap: int = 5) -> dict:
    """The k past mornings most like `day` (standardized distance over FEATURES, using
    only days at least `gap` sessions earlier so no outcome overlaps) and how often
    `y` was positive on them."""
    X = X[FEATURES]
    pos = X.index.get_loc(pd.Timestamp(day))
    hist = X.iloc[:max(0, pos - gap)]
    ok = hist.notna().all(axis=1) & y.reindex(hist.index).notna()
    hist = hist[ok]
    today = X.iloc[pos]
    if len(hist) < 4 * k or today.isna().any():
        return {"p_up": np.nan, "avg": np.nan, "n": 0, "dates": []}
    mu, sd = hist.mean(), hist.std().replace(0, 1)
    d = (((hist - mu) / sd - (today - mu) / sd) ** 2).sum(axis=1)
    near = d.nsmallest(k).index
    outcome = y.loc[near]
    return {"p_up": float((outcome > 0).mean()), "avg": float(outcome.mean()), "n": k,
            "dates": [t.date().isoformat() for t in d.nsmallest(5).index]}


def walk_forward_analogs(X: pd.DataFrame, y: pd.Series, days, k: int) -> pd.Series:
    """p_up for each day in `days`, each using only earlier history."""
    return pd.Series({d: analog_forecast(X, y, d, k)["p_up"] for d in days}, dtype=float)
