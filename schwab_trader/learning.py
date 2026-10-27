"""Learning from mistakes.

Two loops, both re-run every morning on all the data to date, so the program keeps
learning as new trades finish:

1. Losing-trade conditions. Every past dip-buy and EPS trade (the email's exact rules,
   over the full price history, so each new day adds trades) is tagged with what was
   true when it was bought: market in a downtrend, VIX high or jumping, the stock
   gapping down at the open, Asia falling overnight, its sector below its 50-day
   average, a broad sell-off, a very volatile stock, a crash-like drop, earnings in the
   holding period. A condition is learned as "avoid" only when trades bought under it
   won clearly less often (7+ points) AND made less on average, with enough cases
   (40+), in both the older and the newer half of the history - one bad streak can't
   create a rule. The learning itself is tested walk-forward: each year's rules are
   learned only from earlier years and then judged on that year. The email applies the
   learned rules only if they improved those unseen years.

2. Live track record. If a rule's real picks (journal.py) win significantly less often
   than its backtest (binomial test, 5% level, at least 10 closed picks), the email
   halves that rule's position size until the record recovers.

    python -m schwab_trader.learning          # walk-forward test of the learning (results/learning/)
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from .config import DATA_DIR, RESULTS_DIR

OUT = RESULTS_DIR / "learning"
EVENTS = OUT / "earnings_events.csv"      # every earnings reaction day (saved by main; used by the email)
MIN_CASES, MIN_GAP = 40, 0.07
SECTOR_ETF = {"Information Technology": "XLK", "Financials": "XLF", "Health Care": "XLV", "Energy": "XLE",
              "Industrials": "XLI", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Utilities": "XLU",
              "Materials": "XLB", "Real Estate": "XLRE", "Communication Services": "XLC"}
CONDITIONS = {
    "spy_downtrend": "the S&P 500 (SPY) was below its 200-day average",
    "vix_high": "VIX was above 25",
    "vix_jump": "VIX had risen 20%+ in 5 days",
    "gap_down_entry": "the stock opened 2%+ below the prior close on the buy day",
    "high_vol_stock": "the stock had been swinging more than 45% a year (20-day volatility)",
    "asia_down": "Asian markets fell 1%+ overnight",
    "sector_weak": "the stock's sector fund was below its 50-day average",
    "market_selloff": "the S&P 500 was sharply oversold (2-day RSI under 10)",
    "big_drop": "the stock had fallen 8%+ in the 3 days before the signal",
    "earnings_soon": "the company reported earnings while the trade was open",
}


# ----------------------------------------------------------------- features
def features(px: dict, vix: pd.Series, asia: pd.Series, trades: pd.DataFrame, events: pd.DataFrame | None = None,
             hold: int = 20) -> pd.DataFrame:
    """One row per trade (symbol, entry[, return, exit]) with the CONDITIONS as True/False,
    using only what was known at the signal close (or the entry open for the gap)."""
    from .pullback import rsi
    from .universe import sector
    c, o = px["close"], px["open"]
    idx = c.index
    pos = idx.get_indexer(pd.DatetimeIndex(trades["entry"]))
    ok = pos >= 4
    t = trades[ok].reset_index(drop=True).copy()
    pos = pos[ok]
    prev = pos - 1
    col = c.columns.get_indexer(t["symbol"])
    C, O = c.to_numpy(dtype=float), o.to_numpy(dtype=float)
    spy = c["SPY"]
    sma200 = spy.rolling(200).mean().to_numpy()
    sv = spy.to_numpy()
    v = vix.reindex(idx).ffill().to_numpy()
    logret = np.log(c).diff()
    vol20 = (logret.rolling(20).std() * math.sqrt(252)).to_numpy()
    spy_rsi = rsi(spy.to_frame())["SPY"].to_numpy()
    a = asia.reindex(idx).to_numpy() if asia is not None else np.full(len(idx), np.nan)
    t["spy_downtrend"] = sv[prev] < sma200[prev]
    t["vix_high"] = v[prev] > 25
    t["vix_jump"] = v[prev] / v[prev - 5] > 1.2
    t["gap_down_entry"] = O[pos, col] / C[prev, col] - 1 <= -0.02
    t["high_vol_stock"] = vol20[prev, col] > 0.45
    t["asia_down"] = a[pos] <= -0.01
    t["market_selloff"] = spy_rsi[prev] < 10
    t["big_drop"] = C[prev, col] / C[prev - 3, col] - 1 <= -0.08
    etf = [SECTOR_ETF.get(sector(s), s if s in c.columns else "SPY") for s in t["symbol"]]
    ecol = c.columns.get_indexer([e if e in c.columns else "SPY" for e in etf])
    s50 = c.rolling(50).mean().to_numpy()
    t["sector_weak"] = C[prev, ecol] < s50[prev, ecol]
    if events is not None and len(events):
        from .accuracy_research import sessions_to_earnings
        t["earnings_soon"] = sessions_to_earnings(px, events, t) < hold
    else:
        t["earnings_soon"] = False
    return t


# ------------------------------------------------------------------- learn
def _check(g: pd.DataFrame, cond: str, min_cases: int, gap: float) -> dict | None:
    w, wo = g[g[cond]], g[~g[cond]]
    if len(w) < min_cases or not len(wo):
        return None
    win_w, win_wo = (w["return"] > 0).mean(), (wo["return"] > 0).mean()
    avg_w, avg_wo = w["return"].mean(), wo["return"].mean()
    if win_w <= win_wo - gap and avg_w < avg_wo:
        return {"cases": int(len(w)), "win_with": float(win_w), "win_without": float(win_wo),
                "avg_with": float(avg_w), "avg_without": float(avg_wo)}
    return None


def learn(f: pd.DataFrame, until=None) -> dict:
    """{condition: stats} learned from trades that had finished before `until`."""
    g = f.dropna(subset=["return"])
    if until is not None:
        g = g[pd.DatetimeIndex(g["exit"]) < pd.Timestamp(until)]
    if len(g) < 2 * MIN_CASES:
        return {}
    mid = g["entry"].sort_values().iloc[len(g) // 2]
    old, new = g[g["entry"] < mid], g[g["entry"] >= mid]
    rules = {}
    for cond in CONDITIONS:
        if cond not in g:
            continue
        whole = _check(g, cond, MIN_CASES, MIN_GAP)
        if whole and _check(old, cond, MIN_CASES // 2, MIN_GAP / 2) and _check(new, cond, MIN_CASES // 2, MIN_GAP / 2):
            rules[cond] = whole
    return rules


def avoid(f: pd.DataFrame, rules: dict) -> pd.Series:
    out = pd.Series(False, index=f.index)
    for cond in rules:
        out |= f[cond].astype(bool)
    return out


def walk_forward(f: pd.DataFrame, first_year: int = 2014) -> dict:
    """Each year: learn from trades finished before it, skip that year's trades that
    match, and compare the year's results with and without the skipping."""
    rows = []
    f = f.dropna(subset=["return"])
    last = pd.DatetimeIndex(f["entry"]).max().year
    for y in range(first_year, last + 1):
        rules = learn(f, f"{y}-01-01")
        yr = f[pd.DatetimeIndex(f["entry"]).year == y]
        if not len(yr):
            continue
        kept = yr[~avoid(yr, rules)]
        rows.append({"year": y, "rules": ",".join(rules), "trades": len(yr), "kept": len(kept),
                     "win_all": float((yr["return"] > 0).mean()), "win_kept": float((kept["return"] > 0).mean()) if len(kept) else np.nan,
                     "avg_all": float(yr["return"].mean()), "avg_kept": float(kept["return"].mean()) if len(kept) else np.nan,
                     "sum_all": float(yr["return"].sum()), "sum_kept": float(kept["return"].sum())})
    t = pd.DataFrame(rows)
    out = {"years": t.to_dict("records")}
    for name, part in (("all_years", t), ("2022_on", t[t.year >= 2022])):
        if len(part):
            n, k = part.trades.sum(), part.kept.sum()
            out[name] = {"trades": int(n), "kept": int(k),
                         "win_all": float((part.win_all * part.trades).sum() / n),
                         "win_kept": float((part.win_kept.fillna(0) * part.kept).sum() / k) if k else np.nan,
                         "avg_all": float(part.sum_all.sum() / n), "avg_kept": float(part.sum_kept.sum() / k) if k else np.nan}
    a = out.get("all_years", {})
    # use the learned rules live only if learning helped the unseen years on both counts
    out["use_live"] = bool(a and a["kept"] and a["win_kept"] > a["win_all"] and a["avg_kept"] >= a["avg_all"])
    return out


# ------------------------------------------------------------ trade history
def rule_trades(px: dict, acc: dict, events: pd.DataFrame, eligible=None) -> dict[str, pd.DataFrame]:
    """All past trades of the email's dip-buy and EPS rules (every signal, no slot limit)."""
    from .accuracy_research import Exit, arrays, dip_entries, eps_entries, filtered, simulate
    from .daily_research import EpsConfig
    syms = list(px["close"].columns)
    arr = arrays(px, syms)
    out = {}
    d = acc["dip"]
    ex = Exit(**d["exit"])
    ent = filtered(px, dip_entries(px, d["rsi_max"], d.get("momentum_top"), eligible), d.get("spy_trend", False),
                   ex.hold if d.get("no_earnings") else None, events)
    out["dip"] = simulate(px, arr, ent, ex, syms)
    e = acc["eps"]
    ex = Exit(**e["exit"])
    r = e["rule"]
    cfg = EpsConfig(r["entry"], r["min_gap"], 1, False, r.get("min_surprise"))
    ent = filtered(px, eps_entries(px, events, cfg)[["symbol", "entry"]], e.get("spy_trend", False), None)
    out["eps"] = simulate(px, arr, ent, ex, syms)
    return out


def learned(px: dict, acc: dict, events: pd.DataFrame, vix: pd.Series, asia: pd.Series, eligible=None) -> dict:
    """{kind: {"rules": {...}, "walk_forward": {...}, "use_live": bool}} on all data to date."""
    out = {}
    for kind, tr in rule_trades(px, acc, events, eligible).items():
        f = features(px, vix, asia, tr, events, acc[kind]["exit"]["hold"])
        wf = walk_forward(f)
        out[kind] = {"rules": learn(f), "walk_forward": wf, "use_live": wf["use_live"], "trades": int(len(f))}
    return out


def matches(px: dict, vix: pd.Series, asia_today: float, symbol: str, entry, rules: dict, gap_today: float | None,
            upcoming_earnings: bool, hold: int) -> list[str]:
    """Which learned 'avoid' conditions today's candidate meets (entry = the plan day)."""
    if not rules:
        return []
    idx = px["close"].index
    fake = {k: v.copy() for k, v in px.items()}
    day = pd.Timestamp(entry)
    if day not in idx:
        for k in fake:
            fake[k].loc[day] = np.nan
        if gap_today is not None and np.isfinite(gap_today):
            fake["open"].loc[day, symbol] = float(px["close"][symbol].iloc[-1]) * (1 + gap_today)
    f = features(fake, vix, pd.Series({day: asia_today}), pd.DataFrame({"symbol": [symbol], "entry": [day]}), None, hold)
    if not len(f):
        return []
    f.loc[0, "earnings_soon"] = upcoming_earnings
    return [c for c in rules if bool(f.loc[0, c])]


# ------------------------------------------------------- live track record
def binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def size_factor(record: dict, backtest_win: float | None) -> tuple[float, str]:
    """(1.0 or 0.5, note): half size while the live record is significantly worse than
    the backtest."""
    if not record or not backtest_win or record.get("closed", 0) < 10:
        return 1.0, ""
    n, k = record["closed"], record["wins"]
    p = binom_cdf(k, n, backtest_win)
    if p < 0.05:
        return 0.5, (f"half size: live picks won {k} of {n} ({k / n:.0%}) vs {backtest_win:.0%} in the backtest - "
                     f"too far below to be luck (p={p:.2f}); full size again once the record recovers")
    return 1.0, ""


def lines_for(kind: str, info: dict, skipped: list[tuple[str, list[str]]]) -> list[str]:
    """Email lines about what was learned for one rule."""
    if not info:
        return []
    wf = info["walk_forward"].get("all_years", {})
    rules = info["rules"]
    out = []
    if rules:
        for cond, s in rules.items():
            out.append(f"  learned: {kind} trades bought when {CONDITIONS[cond]} won {s['win_with']:.0%} (avg "
                       f"{s['avg_with'] * 100:+.1f}%) vs {s['win_without']:.0%} ({s['avg_without'] * 100:+.1f}%) otherwise, "
                       f"{s['cases']} cases")
        if wf:
            verdict = "so those are skipped" if info["use_live"] else "but skipping them didn't help in years it hadn't seen, so they're NOT skipped"
            out.append(f"  tested year by year on unseen years: won {wf['win_all']:.0%} -> {wf['win_kept']:.0%}, avg "
                       f"{wf['avg_all'] * 100:+.2f}% -> {wf['avg_kept'] * 100:+.2f}% a trade, {verdict}")
    for sym, conds in skipped:
        out.append(f"  SKIPPED {sym}: " + "; ".join(CONDITIONS[c] for c in conds))
    return out


def main():
    from .accuracy_research import research_setup
    from .patterns import ASIA, SECTORS, asia_overnight
    acc = json.loads((RESULTS_DIR / "accuracy" / "summary.json").read_text())
    px, syms, eligible, eps, events, _ = research_setup(list(SECTORS) + [a for a in ASIA if (DATA_DIR / f"{a}.csv").exists()])
    vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")["close"]
    asia = asia_overnight(px)["asia"]
    res = learned(px, acc, events, vix, asia, eligible)
    OUT.mkdir(parents=True, exist_ok=True)
    events.to_csv(EVENTS, index=False, date_format="%Y-%m-%d", float_format="%.4f")
    (OUT / "summary.json").write_text(json.dumps(res, indent=2, default=str))
    for kind, info in res.items():
        print(kind, json.dumps({k: v for k, v in info.items() if k != "walk_forward"}, indent=1, default=str))
        print(json.dumps({k: v for k, v in info["walk_forward"].items() if k != "years"}, indent=1, default=str))
        print(pd.DataFrame(info["walk_forward"]["years"]).to_string())


if __name__ == "__main__":
    main()
