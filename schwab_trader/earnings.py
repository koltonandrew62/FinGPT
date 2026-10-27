"""Earnings play book: 8-quarter fundamental analysis with cash flow as the primary input.

For each company we rebuild its last 8+ quarters from SEC EDGAR filings (10-Q/10-K),
using only figures as they were *first reported* and the date they became public,
so the backtest never sees a number before the market did. From those quarters we
compute a cash-flow-led score:

  Cash flow     55%  TTM free-cash-flow growth, FCF margin trend, cash conversion
                     (operating cash flow / net income), # of FCF-positive quarters
  Earnings      25%  standardized EPS surprise vs. the same quarter last year
                     (seasonal random walk, the classic PEAD measure), revenue growth
  Balance sheet 20%  net-debt change, share-count change (buybacks), liquidity trend

Weights and scales are fixed in advance (not fitted to returns). The score is in
[-1, 1]; >= BULLISH is a buy signal for the earnings-drift strategy.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .config import STATE_DIR

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CACHE_DIR = STATE_DIR / "sec_cache"

# Field -> XBRL concepts in priority order (companies switch tags over the years).
FLOW_CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
}
AVERAGE_CONCEPTS = {"shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                               "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"]}
INSTANT_CONCEPTS = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "assets": ["Assets"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
}
FORMS = {"10-Q", "10-K"}

# (weight, transform) per feature; each transform maps the raw value onto [-1, 1].
WEIGHTS = {
    "fcf_growth":        (0.20, lambda x: np.tanh(x / 0.25)),
    "fcf_margin_change": (0.15, lambda x: np.tanh(x / 0.03)),
    "cash_conversion":   (0.10, lambda x: np.tanh((x - 1.0) / 0.3)),
    "fcf_positive_qtrs": (0.10, lambda x: 2.0 * x - 1.0),
    "eps_surprise":      (0.15, lambda x: np.tanh(x / 1.0)),
    "revenue_growth":    (0.10, lambda x: np.tanh(x / 0.10)),
    "net_debt_change":   (0.08, lambda x: -np.tanh(x / 0.05)),
    "share_change":      (0.07, lambda x: -np.tanh(x / 0.02)),
    "current_ratio_change": (0.05, lambda x: np.tanh(x / 0.2)),
}
BULLISH, BEARISH = 0.25, -0.25


# --------------------------------------------------------------------------- SEC data
def _sec_get(url: str, user_agent: str):
    import requests

    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    time.sleep(0.12)   # SEC asks for <= 10 requests/second
    return resp.json()


def sec_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("Set SEC_USER_AGENT to 'your name your@email.com' (SEC requires a contact).")
    return ua


def ticker_to_cik(user_agent: str, cache_dir: Path = CACHE_DIR) -> dict[str, int]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "company_tickers.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
        path.write_text(json.dumps(_sec_get(SEC_TICKERS_URL, user_agent)))
    rows = json.loads(path.read_text()).values()
    return {r["ticker"].upper(): int(r["cik_str"]) for r in rows}


def company_facts(ticker: str, user_agent: str | None = None, cache_dir: Path = CACHE_DIR,
                  max_age_days: float = 1.0) -> dict:
    """SEC companyfacts JSON for `ticker`, cached on disk."""
    user_agent = user_agent or sec_user_agent()
    cik = ticker_to_cik(user_agent, cache_dir)[ticker.upper().replace(".", "-")]
    path = cache_dir / f"{ticker.upper()}.json"
    if not path.exists() or time.time() - path.stat().st_mtime > max_age_days * 86400:
        path.write_text(json.dumps(_sec_get(SEC_FACTS_URL.format(cik=cik), user_agent)))
    return json.loads(path.read_text())


def recent_filings(ticker: str, days: int = 7, user_agent: str | None = None,
                   today: pd.Timestamp | None = None) -> pd.DataFrame:
    """10-Q/10-K filings by `ticker` in the last `days` days (small SEC request)."""
    user_agent = user_agent or sec_user_agent()
    cik = ticker_to_cik(user_agent)[ticker.upper().replace(".", "-")]
    recent = _sec_get(SEC_SUBMISSIONS_URL.format(cik=cik), user_agent)["filings"]["recent"]
    df = pd.DataFrame({"form": recent["form"], "filed": pd.to_datetime(recent["filingDate"]),
                       "period": recent["reportDate"]})
    today = today or pd.Timestamp.today().normalize()
    return df[df.form.isin(FORMS) & (df.filed > today - pd.Timedelta(days=days))]


# ----------------------------------------------------------------- quarter rebuilding
def _facts(facts: dict, concept: str) -> pd.DataFrame:
    node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return pd.DataFrame()
    rows = [f for unit in node["units"].values() for f in unit if f.get("form") in FORMS]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["end"] = pd.to_datetime(df["end"])
    df["filed"] = pd.to_datetime(df["filed"])
    if "start" in df:
        df["start"] = pd.to_datetime(df["start"])
    # Point in time: keep the first-filed value for each period (later restatements ignored).
    keys = ["start", "end"] if "start" in df else ["end"]
    return df.sort_values("filed").drop_duplicates(keys, keep="first")


def _quarterly_flow(df: pd.DataFrame, derive: bool = True) -> pd.DataFrame:
    """Quarter values (end, val, filed) from direct ~3-month facts, plus quarters
    derived by differencing year-to-date facts (10-Q cash-flow statements and
    10-K annual figures are cumulative)."""
    if df.empty or "start" not in df:
        return pd.DataFrame(columns=["end", "val", "filed"])
    df = df.dropna(subset=["start"]).copy()
    df["days"] = (df["end"] - df["start"]).dt.days
    direct = df[df.days.between(80, 100)][["end", "val", "filed"]]
    derived = []
    if derive:
        for _, grp in df[df.days >= 80].groupby("start"):
            grp = grp.sort_values("end")
            prev = None
            for _, row in grp.iterrows():
                if prev is not None and 80 <= (row["end"] - prev["end"]).days <= 100:
                    derived.append({"end": row["end"], "val": row["val"] - prev["val"],
                                    "filed": max(row["filed"], prev["filed"])})
                prev = row
    out = pd.concat([direct, pd.DataFrame(derived, columns=["end", "val", "filed"])])
    out = out.sort_values("filed").drop_duplicates("end", keep="first").sort_values("end")
    if derive:
        # Q4 = full year - (Q1 + Q2 + Q3) when the 10-Qs carried no year-to-date figure.
        q4 = []
        for _, fy in df[df.days.between(350, 380)].iterrows():
            if (out["end"] == fy["end"]).any():
                continue
            inside = out[(out["end"] > fy["start"]) & (out["end"] < fy["end"])]
            if len(inside) == 3:
                q4.append({"end": fy["end"], "val": fy["val"] - inside["val"].sum(),
                           "filed": max(fy["filed"], inside["filed"].max())})
        if q4:
            out = pd.concat([out, pd.DataFrame(q4)]).sort_values("end")
    return out


def _merge_concepts(facts: dict, concepts: list, builder) -> pd.DataFrame:
    parts = [builder(_facts(facts, c)) for c in concepts]
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=["end", "val", "filed"])
    return pd.concat(parts).drop_duplicates("end", keep="first").sort_values("end")


def quarterly_fundamentals(facts: dict) -> pd.DataFrame:
    """One row per fiscal quarter end with every field and `filed`, the date the
    quarter's report became public (when the backtest may first use it)."""
    series = {}
    for field, concepts in FLOW_CONCEPTS.items():
        series[field] = _merge_concepts(facts, concepts, _quarterly_flow)
    for field, concepts in AVERAGE_CONCEPTS.items():
        series[field] = _merge_concepts(facts, concepts, lambda d: _quarterly_flow(d, derive=False))
    for field, concepts in INSTANT_CONCEPTS.items():
        series[field] = _merge_concepts(facts, concepts,
                                        lambda d: d[["end", "val", "filed"]] if len(d) else d)
    base = series["revenue"] if len(series["revenue"]) else series["net_income"]
    if base.empty:
        return pd.DataFrame()
    q = pd.DataFrame(index=pd.DatetimeIndex(base["end"], name="end"))
    filed = base.set_index("end")["filed"]
    for field, s in series.items():
        if len(s):
            s = s.set_index("end")
            # Balance-sheet dates can differ by a few days from the income-statement quarter end.
            q[field] = s["val"].reindex(q.index, method="nearest", tolerance=pd.Timedelta(days=7))
            if field in ("net_income", "ocf"):
                filed = pd.concat([filed, s["filed"].reindex(q.index)], axis=1).max(axis=1)
        else:
            q[field] = np.nan
    q["filed"] = pd.to_datetime(filed)
    # Drop the long-ago quarters SEC sometimes back-fills with very late filed dates.
    q = q[(q["filed"] - q.index.to_series()).dt.days.between(0, 120)]
    q["fcf"] = q["ocf"] - q["capex"].fillna(0.0)
    return q


# ------------------------------------------------------------------- scoring
def _ttm(s: pd.Series, k: int) -> float:
    window = s.iloc[k - 3: k + 1] if k >= 3 else s.iloc[:0]
    return float(window.sum()) if len(window) == 4 and window.notna().all() else np.nan


def _ratio_change(now: float, then: float) -> float:
    return (now - then) / abs(then) if then and np.isfinite(then) and np.isfinite(now) else np.nan


def earnings_features(q: pd.DataFrame) -> dict:
    """Features for the latest quarter in `q` from its trailing 8 quarters."""
    q = q.tail(8)
    k = len(q) - 1
    gaps = q.index.to_series().diff().dt.days.dropna()
    if k < 7 or not gaps.between(80, 100).all():    # need 8 consecutive quarters
        return {}
    f = {}
    fcf_now, fcf_then = _ttm(q.fcf, k), _ttm(q.fcf, k - 4)
    rev_now, rev_then = _ttm(q.revenue, k), _ttm(q.revenue, k - 4)
    f["fcf_growth"] = float(np.clip(_ratio_change(fcf_now, fcf_then), -1, 1))
    if rev_now and rev_then:
        f["fcf_margin_change"] = fcf_now / rev_now - fcf_then / rev_then
    ni = _ttm(q.net_income, k)
    ocf = _ttm(q.ocf, k)
    if np.isfinite(ni) and np.isfinite(ocf):
        f["cash_conversion"] = float(np.clip(ocf / ni, -1, 3)) if ni > 0 else (2.0 if ocf > 0 else -1.0)
    f["fcf_positive_qtrs"] = float((q.fcf > 0).mean()) if q.fcf.notna().all() else np.nan
    eps = q.eps
    if eps.notna().all() and eps.std() > 0:
        f["eps_surprise"] = float((eps.iloc[k] - eps.iloc[k - 4]) / eps.std())
    f["revenue_growth"] = float(np.clip(_ratio_change(q.revenue.iloc[k], q.revenue.iloc[k - 4]), -1, 1))
    nd = q.debt.fillna(0.0) - q.cash
    if q.assets.iloc[k] and np.isfinite(nd.iloc[k]) and np.isfinite(nd.iloc[k - 4]):
        f["net_debt_change"] = float((nd.iloc[k] - nd.iloc[k - 4]) / q.assets.iloc[k])
    f["share_change"] = float(np.clip(_ratio_change(q.shares.iloc[k], q.shares.iloc[k - 4]), -0.5, 0.5))
    cr = q.current_assets / q.current_liabilities
    f["current_ratio_change"] = float(cr.iloc[k] - cr.iloc[k - 4])
    return {key: v for key, v in f.items() if v is not None and np.isfinite(v)}


def score_features(features: dict) -> float:
    """Weighted [-1, 1] score; missing features are dropped and weights renormalized."""
    num = den = 0.0
    for name, (w, transform) in WEIGHTS.items():
        if name in features:
            num += w * float(transform(features[name]))
            den += w
    return num / den if den >= 0.5 else np.nan   # need at least half the model's inputs


def outcome(score: float) -> str:
    if not np.isfinite(score):
        return "INSUFFICIENT DATA"
    return "BULLISH" if score >= BULLISH else "BEARISH" if score <= BEARISH else "NEUTRAL"


def score_history(q: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time score for every quarter that has 8 quarters of history."""
    rows = []
    for k in range(7, len(q)):
        window = q.iloc[k - 7: k + 1]
        feats = earnings_features(window)
        rows.append({"end": q.index[k], "filed": q["filed"].iloc[k], "score": score_features(feats), **feats})
    return pd.DataFrame(rows)


def analyze(ticker: str, user_agent: str | None = None) -> dict:
    """Latest-quarter analysis for one stock (used by the weekly report)."""
    q = quarterly_fundamentals(company_facts(ticker, user_agent))
    feats = earnings_features(q)
    score = score_features(feats)
    last = q.iloc[-1] if len(q) else None
    return {"ticker": ticker, "score": score, "outcome": outcome(score), "features": feats,
            "quarter_end": None if last is None else q.index[-1].date().isoformat(),
            "filed": None if last is None else last["filed"].date().isoformat(),
            "quarters": q.tail(8)}


def surprise_history(earnings_dates: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize a yfinance get_earnings_dates() frame to reported quarters with
    estimate, actual, surprise % and beat/miss."""
    if earnings_dates is None or earnings_dates.empty:
        return pd.DataFrame(columns=["date", "estimate", "actual", "surprise_pct", "result"])
    cols = {c.lower().replace(" ", ""): c for c in earnings_dates.columns}
    est = earnings_dates[cols.get("epsestimate")]
    act = earnings_dates[cols.get("reportedeps")]
    df = pd.DataFrame({"date": pd.to_datetime(earnings_dates.index), "estimate": est.to_numpy(),
                       "actual": act.to_numpy()}).dropna(subset=["actual", "estimate"])
    df["surprise_pct"] = (df.actual - df.estimate) / df.estimate.abs().replace(0, np.nan) * 100
    df["result"] = np.where(df.actual > df.estimate, "BEAT", np.where(df.actual < df.estimate, "MISS", "INLINE"))
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def beat_probability(history: pd.DataFrame, quarters: int = 8) -> float:
    """Laplace-smoothed beat rate over the last `quarters` reports."""
    recent = history.head(quarters)
    return (int((recent.result == "BEAT").sum()) + 1) / (len(recent) + 2)
