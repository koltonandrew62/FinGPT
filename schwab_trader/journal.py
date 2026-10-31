"""Every recommendation the daily email makes, and how it actually turned out.

The daily run appends the day's new BUY ideas (stock picks and options plays) to
results/journal/recommendations.csv; each later run fills in what happened using the
same rules the email gave (buy at the open of the plan day; limit sell at the target,
stop if any, else sell at the close of the last session; put spreads and condors
settled at expiry from the closing price). The 9:40 AM alert adds the real option
bid/ask at the open next to the model's estimate, and marks a spread "skipped" when its
contracts aren't listed or the real credit is under the backtest's floor (a skipped
spread never counts as open or in the track record).

The track record goes in the daily and weekly emails, and learning.py uses it to cut a
rule's size when its live results fall well short of its backtest.

    python -m schwab_trader.journal            # print the track record
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .config import RESULTS_DIR

JOURNAL = RESULTS_DIR / "journal" / "recommendations.csv"
COLUMNS = ["id", "plan_date", "kind", "symbol", "ref_price", "target", "stop", "hold", "legs", "model_credit",
           "max_loss", "rule", "note", "status", "entry_date", "entry_price", "exit_date", "exit_price", "return",
           "reason", "real_credit", "real_quotes"]
TEXT = ["id", "plan_date", "kind", "symbol", "legs", "rule", "note", "status", "entry_date", "exit_date", "reason",
        "real_quotes"]
SHARE_KINDS = ("dip", "eps", "top5_buy")
SHORT_KINDS = ("short_rally", "short_eps", "top5_short")
NAMES = {"top5_buy": "top-5 buys", "top5_short": "top-5 shorts", "long_call": "calls bought",
         "dip": "dip-buys", "eps": "EPS picks", "short_rally": "short sales (weak-stock bounces)",
         "short_eps": "short sales (earnings gap-downs)", "bear_call_spread": "bear call spreads", "long_put": "puts bought",
         "top5_put_spread": "put spreads on top-5 picks", "dip_put_spread": "put spreads on dip-buys",
         "eps_put_spread": "put spreads on EPS picks", "qqq_put_spread": "QQQ put spreads", "qqq_condor": "QQQ 0DTE condors"}
SLIP = 0.0005
# from this session on, top-5 picks are sold at the close before an earnings report (the rule
# chosen in top5_research); earlier picks were held through, as their emails said
EARNINGS_EXIT_FROM = "2026-11-02"
EARNINGS_EXIT_KINDS = ("top5_buy",)
# from the same session on: after the S&P 500 (SPY) falls 7%+ in 5 sessions, top-5 and EPS picks
# below their buy price at that close are sold at the next open (crash_research --exits)
CRASH_EXIT_FROM = EARNINGS_EXIT_FROM
CRASH_EXIT_KINDS = ("top5_buy", "eps")


def load(path: Path | None = None) -> pd.DataFrame:
    path = path or JOURNAL
    if not Path(path).exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, keep_default_na=False, na_values=[""])
    for c in COLUMNS:
        if c not in df:
            df[c] = np.nan
    for c in TEXT:
        df[c] = df[c].astype(object)
    for c in set(COLUMNS) - set(TEXT):                    # whole-number columns would refuse a later fractional value
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df[COLUMNS]


def save(df: pd.DataFrame, path: Path | None = None) -> None:
    path = path or JOURNAL
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df[COLUMNS].sort_values(["plan_date", "kind", "symbol"]).to_csv(path, index=False, float_format="%.6g")


def make_id(plan_date, kind, symbol, legs=None) -> str:
    return f"{plan_date}|{kind}|{symbol}|{json.dumps(legs, sort_keys=True) if legs else ''}"


def record(rows: list[dict], path: Path | None = None) -> int:
    """Append recommendations (dicts with plan_date, kind, symbol, ...); a recommendation
    already in the journal (same day, kind, symbol and legs) is not added twice."""
    df = load(path)
    have = set(df["id"])
    new = []
    for r in rows:
        r = {**r, "id": make_id(r["plan_date"], r["kind"], r["symbol"], r.get("legs")), "status": "open"}
        if isinstance(r.get("legs"), (dict, list)):
            r["legs"] = json.dumps(r["legs"], sort_keys=True)
        if r["id"] not in have:
            new.append({c: r.get(c, np.nan) for c in COLUMNS})
            have.add(r["id"])
    if new:
        df = pd.concat([df, pd.DataFrame(new, columns=COLUMNS)], ignore_index=True) if len(df) else pd.DataFrame(new, columns=COLUMNS)
        save(df, path)
    return len(new)


def earnings_sell_index(idx, days, i0: int, start: str = EARNINGS_EXIT_FROM) -> int | None:
    """The session (index into idx) at whose close a position entered at i0 is sold before a
    report: the one before the first reaction day after the entry day, if on or after `start`."""
    days = np.sort(np.asarray([d for d in days if d >= 0], dtype=int))
    k = np.searchsorted(days, i0 + 1)
    if k >= len(days):
        return None
    i = int(days[k]) - 1
    return i if idx[i] >= pd.Timestamp(start) else None


def _share_outcome(px: dict, r, sell_i: int | None = None, crash: np.ndarray | None = None) -> dict | None:
    """Same engine as the backtest: buy at the plan day's open; target / stop / time, or the
    close of session `sell_i` (the one before an earnings report), or the open after a
    `crash` session (True where the crash exit fired at that close) if below the buy price."""
    idx = px["close"].index
    s, day = r.symbol, pd.Timestamp(r.plan_date)
    if s not in px["close"] or day not in idx:
        return None
    i0 = idx.get_loc(day)
    o, h, l, c = (px[k][s].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    if not np.isfinite(o[i0]):
        return None
    entry = o[i0] * (1 + SLIP)
    hold = int(r.hold)
    tgt = entry * (1 + float(r.target)) if pd.notna(r.target) and r.target else math.inf
    stp = entry * (1 - float(r.stop)) if pd.notna(r.stop) and r.stop else -math.inf
    for k in range(hold):
        i = i0 + k
        if i >= len(idx):
            return {"entry_date": day.date().isoformat(), "entry_price": entry}          # still open
        if crash is not None and k and crash[i - 1] and c[i - 1] < entry and np.isfinite(o[i]):
            return {"entry_date": day.date().isoformat(), "entry_price": entry, "exit_date": idx[i].date().isoformat(),
                    "exit_price": o[i], "return": o[i] * (1 - SLIP) / entry - 1, "reason": "crash", "status": "closed"}
        if l[i] <= stp:
            px_out, why = (min(o[i], stp) if k else stp), "stop"
        elif h[i] >= tgt:
            px_out, why = (max(o[i], tgt) if k else tgt), "target"
        elif i == sell_i:
            px_out, why = c[i], "earnings"
        elif k == hold - 1:
            px_out, why = c[i], "time"
        else:
            continue
        return {"entry_date": day.date().isoformat(), "entry_price": entry, "exit_date": idx[i].date().isoformat(),
                "exit_price": px_out, "return": px_out * (1 - SLIP) / entry - 1, "reason": why, "status": "closed"}
    return None


def _short_outcome(px: dict, r) -> dict | None:
    """Short at the plan day's open; cover at -target (limit), +stop (buy-stop) or the
    close of the last session; borrow/dividend cost 2.5%/yr."""
    idx = px["close"].index
    s, day = r.symbol, pd.Timestamp(r.plan_date)
    if s not in px["close"] or day not in idx:
        return None
    i0 = idx.get_loc(day)
    o, h, l, c = (px[k][s].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    if not np.isfinite(o[i0]):
        return None
    entry = o[i0] * (1 - SLIP)
    hold = int(r.hold)
    tgt = entry * (1 - float(r.target)) if pd.notna(r.target) and r.target else -math.inf
    stp = entry * (1 + float(r.stop)) if pd.notna(r.stop) and r.stop else math.inf
    for k in range(hold):
        i = i0 + k
        if i >= len(idx):
            return {"entry_date": day.date().isoformat(), "entry_price": entry}
        if h[i] >= stp:
            px_out, why = (max(o[i], stp) if k else stp), "stop"
        elif l[i] <= tgt:
            px_out, why = (min(o[i], tgt) if k else tgt), "target"
        elif k == hold - 1:
            px_out, why = c[i], "time"
        else:
            continue
        return {"entry_date": day.date().isoformat(), "entry_price": entry, "exit_date": idx[i].date().isoformat(),
                "exit_price": px_out, "return": (entry - px_out * (1 + SLIP)) / entry - 0.025 * (k + 1) / 252,
                "reason": why, "status": "closed"}
    return None


def _put_outcome(px: dict, r) -> dict | None:
    """A put bought for `model_credit` (its cost), held to expiry."""
    legs = json.loads(r.legs) if isinstance(r.legs, str) and r.legs else {}
    exp = pd.Timestamp(legs.get("expiry", r.plan_date))
    if r.symbol not in px["close"] or exp > px["close"].index[-1]:
        return None
    s = float(px["close"][r.symbol].loc[:exp].iloc[-1])
    paid = float(r.real_credit) if pd.notna(r.real_credit) else float(r.model_credit)
    value = max(0.0, legs["put"] - s) if "put" in legs else max(0.0, s - legs["call"])
    return {"entry_date": str(r.plan_date), "entry_price": paid, "exit_date": exp.date().isoformat(), "exit_price": value,
            "return": value / paid - 1 if paid > 0 else np.nan, "reason": "expiry", "status": "closed"}


def _option_outcome(px: dict, r) -> dict | None:
    """Put spreads and condors held to expiry, settled at the expiry day's close."""
    legs = json.loads(r.legs) if isinstance(r.legs, str) and r.legs else {}
    exp = pd.Timestamp(legs.get("expiry", r.plan_date))
    sym = r.symbol
    if sym not in px["close"] or exp > px["close"].index[-1]:
        return None
    s = float(px["close"][sym].loc[:exp].iloc[-1])
    credit = float(r.real_credit) if pd.notna(r.real_credit) else float(r.model_credit)
    owed = 0.0
    if "short_put" in legs:
        owed += max(0.0, legs["short_put"] - s) - max(0.0, legs["long_put"] - s)
    if "short_call" in legs:
        owed += max(0.0, s - legs["short_call"]) - max(0.0, s - legs["long_call"])
    width = max(legs.get("short_put", 0) - legs.get("long_put", 0), legs.get("long_call", 0) - legs.get("short_call", 0))
    risk = max(width - credit, 0.01)
    return {"entry_date": str(r.plan_date), "entry_price": credit, "exit_date": exp.date().isoformat(), "exit_price": owed,
            "return": (credit - owed) / risk, "reason": "expiry", "status": "closed"}


def evaluate(px: dict, path: Path | None = None, events: pd.DataFrame | None = None) -> pd.DataFrame:
    """Fill in outcomes of open recommendations from the daily bars in `px`; `events`
    (symbol, day = earnings reaction day) for the sell-before-earnings rule."""
    df = load(path)
    if not len(df):
        return df
    idx = px["close"].index
    react = {s: idx.get_indexer(pd.DatetimeIndex(g.day)) for s, g in events.groupby("symbol")} if events is not None else {}
    crash = None
    if "SPY" in px["close"]:
        from .crash_research import fast_drop
        crash = (fast_drop(px["close"]["SPY"]) & (idx >= pd.Timestamp(CRASH_EXIT_FROM))).to_numpy(dtype=bool)
    for i, r in df[df.status == "open"].iterrows():
        sell_i = None
        if r.kind in EARNINGS_EXIT_KINDS and r.symbol in react and pd.Timestamp(r.plan_date) in idx:
            sell_i = earnings_sell_index(idx, react[r.symbol], idx.get_loc(pd.Timestamp(r.plan_date)))
        out = (_share_outcome(px, r, sell_i, crash if r.kind in CRASH_EXIT_KINDS else None) if r.kind in SHARE_KINDS
               else _short_outcome(px, r) if r.kind in SHORT_KINDS
               else _put_outcome(px, r) if r.kind in ("long_put", "long_call") else _option_outcome(px, r))
        if out:
            for k, v in out.items():
                df.at[i, k] = v
    save(df, path)
    return df


def track_record(df: pd.DataFrame | None = None) -> dict:
    """{kind: {closed, wins, win_rate, avg, open, first}} over the journal."""
    df = load() if df is None else df
    out = {}
    for kind, g in df.groupby("kind"):
        done = g[g.status == "closed"]
        ret = pd.to_numeric(done["return"], errors="coerce").dropna()
        out[kind] = {"closed": int(len(ret)), "wins": int((ret > 0).sum()),
                     "win_rate": float((ret > 0).mean()) if len(ret) else float("nan"),
                     "avg": float(ret.mean()) if len(ret) else float("nan"),
                     "open": int((g.status == "open").sum()), "skipped": int((g.status == "skipped").sum()),
                     "first": str(g.plan_date.min())}
    return out


def record_lines(acc: dict | None = None, df: pd.DataFrame | None = None) -> list[str]:
    tr = track_record(df)
    if not tr:
        return ["  No picks closed yet - the journal started with this email."]
    first = min(v["first"] for v in tr.values())
    bt = {"dip": (acc or {}).get("dip", {}).get("test", {}), "eps": (acc or {}).get("eps", {}).get("test", {})}
    t5 = RESULTS_DIR / "top5" / "summary.json"
    if t5.exists():
        t5s = json.loads(t5.read_text())
        bt |= {"top5_buy": t5s.get("buy", {}).get("test", {}), "top5_short": t5s.get("short", {}).get("test", {})}
    lines = [f"  Picks made by this email since {first} and how they did (closed trades):"]
    for kind in NAMES:
        if kind not in tr:
            continue
        v = tr[kind]
        res = (f"{v['wins']} of {v['closed']} won ({v['win_rate']:.0%}), avg {v['avg'] * 100:+.1f}%"
               if v["closed"] else "none closed yet")
        unit = "" if kind in SHARE_KINDS + SHORT_KINDS else " of the cost" if kind in ("long_put", "long_call") else " of the max loss"
        ref = f" | backtest {bt[kind]['win_rate']:.0%}" if kind in bt and bt[kind].get("win_rate") else ""
        skip = f"; {v['skipped']} not placed (not listed or credit too low at the open)" if v.get("skipped") else ""
        lines.append(f"    {NAMES[kind]:26s} {res}{unit if v['closed'] else ''}; {v['open']} still open{skip}{ref}")
    return lines


if __name__ == "__main__":
    print("\n".join(record_lines()))
