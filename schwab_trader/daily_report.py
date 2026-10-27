"""Daily trading plan email (Sunday-Friday, 8:30 AM ET; sized for a $1.5M margin account):
market trends and patterns, Asia overnight, after-hours and pre-market moves, EPS
picks, regular stock picks (dip-buys), options plays on those picks, QQQ options and
the earnings calendar. Rules and win rates come from daily_research.py
(results/daily/summary.json).

Inputs, saved by the scheduled run from the Robinhood connector (read-only):
  data/*.csv               daily bars (import_robinhood)
  --extended FILE ...      hourly 24-hour bars, get_equity_historicals interval=hour bounds=24_5
  --earnings-json PAST NEXT  get_earnings_calendar days=-31 and days=7

    python -m schwab_trader.daily_report --extended e1.txt e2.txt ... --earnings-json past.txt next.txt --text-out /tmp/daily.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar_utils import is_trading_day, next_trading_day, order_time
from .config import DATA_DIR, RESULTS_DIR, SPLITS
from .daily_research import (DIP, STOCKS, EpsConfig, OptConfig, calm_vol, opt_half_spread, reaction_day, surprise)
from .data import load_prices
from .options_backtest import COMMISSION, MULTIPLIER, bs_price, implied_vol
from .patterns import (ASIA, ASIA_ADRS, SECTORS, analog_forecast, asia_impact, asia_overnight, forward_returns,
                       market_features, market_outcomes, pattern_frames, pattern_stats, trend_label)
from .pullback import POOLS, indicators, rsi, run_pullback, signals
from .sizing import Account, money

SUMMARY = RESULTS_DIR / "daily" / "summary.json"
ACCURACY = RESULTS_DIR / "accuracy" / "summary.json"      # accuracy_research.py: the rules in this email
EPS_HISTORY = RESULTS_DIR / "daily" / "eps_history.json"
ACCOUNT = 1_500_000.0
DIP_SHARE, EPS_SHARE = 0.5, 0.5       # the two share strategies split the account
EPS_SLOTS = 5
STOCK_OPT_RISK = 0.01                 # most one stock options play may lose (1% = $15,000)
QQQ_OPT_RISK = 0.01
CONDOR_RISK = 0.02                    # calm-day 0DTE condor, as researched
ET = "America/New_York"


def pct(x, d=1):
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.{d}f}%"


# ------------------------------------------------------------- data inputs
def load_extended(paths) -> dict[str, pd.DataFrame]:
    out = {}
    for p in paths or []:
        text = Path(p).read_text()
        for r in json.loads(text[text.index("{"):])["data"]["results"]:
            bars = [b for b in r.get("bars", []) if not b.get("interpolated")]
            if bars:
                df = pd.DataFrame({"time": pd.to_datetime([b["begins_at"] for b in bars], utc=True),
                                   **{k: [float(b[f"{k}_price"]) for b in bars] for k in ("open", "high", "low", "close")},
                                   "volume": [float(b.get("volume") or 0) for b in bars],
                                   "session": [b.get("session", "") for b in bars]}).set_index("time")
                out[r["symbol"]] = df.sort_index()
    return out


def load_calendar(path) -> list[dict]:
    text = Path(path).read_text()
    rows = json.loads(text[text.index("{"):])["data"]["results"]
    out = {}
    for r in rows:
        eps, rep = r.get("eps") or {}, r.get("report") or {}
        num = lambda x: float(x) if x not in (None, "") else float("nan")
        out[(r["symbol"], rep.get("date"))] = {"symbol": r["symbol"], "date": rep.get("date"),
                                               "timing": rep.get("timing") or "am", "estimate": num(eps.get("estimate")),
                                               "actual": num(eps.get("actual"))}
    return sorted(out.values(), key=lambda r: (r["date"] or "", r["symbol"]))


def complete_daily(px: dict, ext: dict, now: pd.Timestamp) -> dict:
    """Robinhood's daily bar for a session appears only the next day, so build any
    finished session missing from data/ out of its regular-hours 30-minute bars."""
    last = px["close"].index[-1]
    rows = {}
    for s, df in ext.items():
        if s not in px["close"]:
            continue
        reg = df[df.session == "reg"]
        days = reg.index.tz_convert(ET).normalize().tz_localize(None)
        # these bars carry only part of the consolidated volume: scale by the usual ratio
        intraday = reg.volume.groupby(days).sum()
        both = intraday[intraday.index.isin(px["volume"].index) & (intraday > 0)]
        scale = float((px["volume"][s].reindex(both.index) / both).median()) if len(both) else 1.0
        scale = scale if np.isfinite(scale) and scale > 0 else 1.0
        for day, bars in reg.groupby(days):
            if day <= last or pd.Timestamp(f"{day.date()} 16:00", tz=ET) > now or len(bars) < 6:
                continue
            rows.setdefault(day, {})[s] = {"open": bars.open.iloc[0], "high": bars.high.max(), "low": bars.low.min(),
                                           "close": bars.close.iloc[-1], "volume": bars.volume.sum() * scale}
    if not rows:
        return px
    out = {}
    for col, frame in px.items():
        add = pd.DataFrame({d: {s: v[col] for s, v in r.items()} for d, r in rows.items()}).T
        out[col] = pd.concat([frame, add.reindex(columns=frame.columns)]).sort_index()
    return out


def overnight_moves(ext: dict, last_close: pd.Series | None = None, last_day: pd.Timestamp | None = None) -> dict:
    """Per symbol: after-hours, overnight (Asian hours) and pre-market moves since its
    latest regular-session close (taken from the 24-hour bars themselves, so a daily
    file that is a day behind doesn't matter), and the latest price."""
    out = {}
    for s, df in ext.items():
        reg = df[df.session == "reg"]
        if reg.empty:
            continue
        closed = reg.index[-1].tz_convert(ET)
        now = pd.Timestamp.now(tz=ET)
        if closed.date() == now.date() and now.hour < 16:
            continue                                   # today's session is still trading
        after = df[df.index > reg.index[-1]]
        ref = float(reg.close.iloc[-1])
        if after.empty:
            after = reg.iloc[[-1]]
        px = {}
        for sess in ("post", "overnight", "pre"):
            seg = after[after.session == sess]
            if len(seg):
                px[sess] = float(seg.close.iloc[-1])
        post = px.get("post", ref)
        night = px.get("overnight", post)
        pre = px.get("pre", night)
        latest = float(after.close.iloc[-1])
        out[s] = {"post": post / ref - 1, "overnight": night / post - 1, "pre": pre / night - 1,
                  "total": latest / ref - 1, "price": latest, "as_of": after.index[-1].tz_convert(ET),
                  "opened": "reg" in px, "close": ref, "close_date": closed.date(), "sessions": set(px)}
    return out


def session_after(day: dt.date) -> dt.date:
    return next_trading_day(day)


def trading_days_after(day: dt.date, n: int) -> list[dt.date]:
    out, d = [], day
    while len(out) < n:
        d = next_trading_day(d)
        out.append(d)
    return out


def earnings_between(sym: str, start: dt.date, end: dt.date, upcoming: list[dict]) -> str:
    """'  ! reports earnings <date am/pm>' when `sym` reports on or after `start` and by `end`."""
    for r in upcoming:
        if r["symbol"] == sym and r["date"] and start.isoformat() <= r["date"] <= end.isoformat():
            return f"  ! reports earnings {r['date']} {r['timing']}"
    return ""


def strike(x: float) -> float:
    step = 1.0 if x < 100 else 2.5 if x < 250 else 5.0
    return round(x / step) * step


def friday_expiry(start: dt.date, sessions: int) -> dt.date:
    """The Friday nearest to `sessions` trading days after `start` (weekly expiries)."""
    target = trading_days_after(start, sessions)[-1]
    fri = target + dt.timedelta(days=(4 - target.weekday()) % 7)
    return fri if (fri - target).days <= 3 else fri - dt.timedelta(days=7)


# -------------------------------------------------------------- sections
def market_section(px, vix, asia_live, nxt, summary, acc) -> list[str]:
    lines = ["MARKET TRENDS"]
    for sym, name in (("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq 100 (QQQ)")):
        c = px["close"][sym].dropna()
        t = trend_label(c)
        sma200 = c.iloc[-200:].mean()
        lines.append(f"  {name} ${c.iloc[-1]:,.2f}: short-term {t['short']}, medium {t['medium']}, long {t['long']} "
                     f"({pct(c.iloc[-1] / sma200 - 1)} vs 200-day avg)")
    lines.append(f"  VIX {vix.iloc[-1]:.1f}")
    ups = [SECTORS[s] for s in SECTORS if s in px["close"] and trend_label(px["close"][s])["medium"] == "up"]
    downs = [SECTORS[s] for s in SECTORS if s in px["close"] and trend_label(px["close"][s])["medium"] == "down"]
    lines.append(f"  Sectors trending up: {', '.join(ups) or 'none'}")
    lines.append(f"  Sectors trending down: {', '.join(downs) or 'none'}")

    # most similar past mornings
    asia = asia_overnight(px)["asia"]
    px2 = {k: v.copy() for k, v in px.items()}
    for k in px2:
        px2[k].loc[pd.Timestamp(nxt)] = np.nan
    asia2 = asia.copy()
    asia2.loc[pd.Timestamp(nxt)] = asia_live if asia_live is not None else 0.0
    X = market_features(px2, vix, asia2)
    Y = market_outcomes(px)
    mk = acc["market"]
    h = mk["horizon"]
    spy_o, spy_c = px["open"]["SPY"], px["close"]["SPY"]
    y = spy_c.shift(-(h - 1)) / spy_o - 1
    f = analog_forecast(X, y, pd.Timestamp(nxt), mk["k"] or 100, gap=h)
    lines.append(f"  PATTERN MEMORY - the {mk['k']} most similar past mornings since 2010 (trend, momentum, VIX, Asia overnight):")
    if np.isfinite(f["p_up"]):
        cut = mk["cut"] or 0.65
        tag = "leans UP" if f["p_up"] >= cut else "leans DOWN" if f["p_up"] <= 1 - cut else "no clear lean"
        lines.append(f"    SPY over the next {h} sessions: rose on {f['p_up']:.0%} of them (avg {pct(f['avg'], 2)}) -> {tag}. "
                     f"Track record when it leans: right {mk['test_accuracy']:.0%} of {mk['test_days']} days in 2021-2026 "
                     f"({mk['design_accuracy']:.0%} in 2011-2021); always guessing 'up' was right {mk['test_base_rate']:.0%}.")
        lines.append(f"    closest matches: {', '.join(f['dates'])}")
    sma50, sma200 = spy_c.rolling(50).mean(), spy_c.rolling(200).mean()
    trend_up = spy_c.iloc[-1] > sma200.iloc[-1] and sma50.iloc[-1] > sma50.iloc[-11]
    lines.append(f"    Trend check: SPY is {'above' if spy_c.iloc[-1] > sma200.iloc[-1] else 'below'} its 200-day average and its "
                 f"50-day average is {'rising' if sma50.iloc[-1] > sma50.iloc[-11] else 'falling'}"
                 + (" - in that state SPY was higher 20 sessions later 66% of the time (2021-2026; any day 64%)."
                    if trend_up else "."))
    return lines


def overnight_section(px, moves, summary, acc) -> tuple[list[str], float | None]:
    lines = ["", "OVERNIGHT: ASIA, AFTER-HOURS AND PRE-MARKET (latest price vs yesterday's close)"]
    asia = [(s, ASIA[s], moves[s]) for s in ASIA if s in moves]
    composite = float(np.mean([m["total"] for _, _, m in asia])) if asia else None
    if asia and not any("overnight" in m["sessions"] for _, _, m in asia):
        lines.append("  Asia: hasn't traded since the US close yet (Tokyo opens 8 PM ET); the 8:30 AM emails include it.")
        asia, composite = [], None
    if asia:
        lines.append("  Asia (US-listed funds trading overnight): " +
                     ", ".join(f"{name} {pct(m['total'])}" for _, name, m in asia) + f"; average {pct(composite)}")
        adrs = [f"{ASIA_ADRS[s]} {pct(moves[s]['total'])}" for s in ASIA_ADRS if s in moves]
        if adrs:
            lines.append("  Asian stocks listed in the US: " + ", ".join(adrs))
        hist = asia_overnight(px)
        recent = hist.index[-500:]
        beta = asia_impact(px, list(SECTORS) + ["SPY", "QQQ"] + list(STOCKS), hist["asia"],
                           recent[0], recent[-1])
        exp = (beta["beta"] * composite).sort_values()
        sec = exp[[s for s in SECTORS if s in exp.index]]
        lines.append("  Expected effect at the US open (each one's typical move with Asia, last 2 years):")
        lines.append("    sectors most affected: " + ", ".join(f"{SECTORS[s]} {pct(v)}"
                     for s, v in sec.reindex(sec.abs().sort_values(ascending=False).index).head(5).items()))
        stk = exp[[s for s in STOCKS if s in exp.index]]
        lines.append("    stocks most affected: " + ", ".join(f"{s} {pct(v)}"
                     for s, v in stk.reindex(stk.abs().sort_values(ascending=False).index).head(8).items()))
        ac = acc["asia"]
        th = ac["threshold"]
        good = {k: v for k, v in ac["by_symbol"].items() if v["test"] >= 0.70}
        if abs(composite) >= th:
            way = "UP" if composite > 0 else "DOWN"
            names = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", **SECTORS}
            calls = sorted(good.items(), key=lambda kv: -kv[1]["test"])
            lines.append(f"    OPENING CALL - Asia moved {pct(composite)} (more than {th:.0%}), so these should open {way}: " +
                         ", ".join(f"{names[k]} (right {v['test']:.0%})" for k, v in calls) +
                         " - track record on such days in 2021-2026.")
        else:
            lines.append(f"    No opening call: Asia moved less than {th:.0%}. (When it moves {th:.0%}+, the S&P 500 opened the same "
                         f"way {ac['by_symbol']['SPY']['test']:.0%} of the time and the Nasdaq 100 {ac['by_symbol']['QQQ']['test']:.0%}.)")
        lines.append("    Asia sets the open, not the rest of the day: after big Asian moves the US day kept going Asia's way "
                     "only about half the time.")
    for s in ("SPY", "QQQ"):
        if s in moves:
            m = moves[s]
            lines.append(f"  {s}: after-hours {pct(m['post'], 2)}, overnight {pct(m['overnight'], 2)}, "
                         f"pre-market {pct(m['pre'], 2)} -> {pct(m['total'], 2)} (${m['price']:,.2f} as of {m['as_of']:%a %I:%M %p} ET)")
    movers = sorted(((s, moves[s]) for s in STOCKS if s in moves), key=lambda x: -abs(x[1]["total"]))[:10]
    if movers:
        lines.append("  Biggest moves in the 59 stocks:")
        for s, m in movers:
            lines.append(f"    {s:5s} {pct(m['total']):>7s} (after-hours {pct(m['post'])}, overnight {pct(m['overnight'])}, "
                         f"pre-market {pct(m['pre'])}) ${m['price']:,.2f}")
        gf = {(r["gap"], r["window"]): r["fill_rate"] for r in acc.get("gap_fill", [])}
        big = gf.get(("2.0%-4.0%", "test"))
        if big is not None:
            lines.append(f"    Gaps of 2-4% without earnings news stayed open (didn't trade back to yesterday's close) "
                         f"{1 - big:.0%} of the time in 2021-2026; gaps under 1% closed {gf.get(('0.5%-1.0%', 'test'), 0):.0%} of the time.")
    return lines, composite


def replay(px, entries: list[dict], target: float, hold: int, slots: int) -> list[dict]:
    """Replay past entries (dicts with symbol, entry, priority) with a +target limit sell
    and a `hold`-session limit, keeping at most `slots` open. Returns the positions still
    open after the last close, each with its entry price and time-limit session index."""
    idx = px["close"].index
    o, h = px["open"], px["high"]
    active = []
    for e in sorted(entries, key=lambda e: (e["entry"], e.get("priority", 0))):
        i0 = idx.get_loc(e["entry"])
        active = [a for a in active if a["exit_i"] is None or a["exit_i"] >= i0]
        if len(active) >= slots or any(a["symbol"] == e["symbol"] for a in active):
            continue
        px0 = o.at[e["entry"], e["symbol"]]
        if not np.isfinite(px0):
            continue
        tgt = px0 * (1 + target)
        exit_i = None
        for i in range(i0, min(i0 + hold, len(idx))):
            if h.iat[i, h.columns.get_loc(e["symbol"])] >= tgt:
                exit_i = i
                break
        if exit_i is None and i0 + hold - 1 <= len(idx) - 1:
            exit_i = i0 + hold - 1
        active.append({**e, "px0": px0, "target_px": tgt, "i0": i0, "exit_i": exit_i})
    return [a for a in active if a["exit_i"] is None]


def position_lines(kind: str, pos: list[dict], target: float, hold: int, nxt: dt.date, idx, upcoming) -> tuple[list[str], list[str]]:
    lines, sells = [], []
    for a in pos:
        held = len(idx) - a["i0"]                              # sessions held through the last close
        last_day = trading_days_after(idx[-1].date(), max(hold - held, 1))[-1]
        warn = earnings_between(a["symbol"], nxt, last_day, upcoming)
        if hold - held <= 1:
            lines.append(f"  SELL {a['symbol']:5s} at the close {order_time(nxt).replace('9:35 AM', '3:55 PM')} if you own it "
                         f"and the +{target:.0%} limit (${a['target_px']:,.2f}) hasn't filled ({kind} bought {a['entry'].date()})")
            sells.append(a["symbol"])
        else:
            lines.append(f"  HOLD {a['symbol']:5s} if you own it ({kind} bought {a['entry'].date()} at ~${a['px0']:,.2f}): keep the "
                         f"limit sell at ${a['target_px']:,.2f} (+{target:.0%}); otherwise sell at the close on {last_day:%a %b %d}. "
                         f"If you don't own it, skip it - its entry has passed.{warn}")
    return lines, sells


def eps_section(px, calendar, moves, nxt, acc, cash, upcoming=()) -> tuple[list[str], list[dict], list[str]]:
    pick = acc["eps"]
    rule, ex = pick["rule"], pick["exit"]
    target, hold = ex["target"], ex["hold"]
    c, o = px["close"], px["open"]
    idx = c.index
    last = idx[-1]
    future = [pd.Timestamp(d) for d in trading_days_after(last.date(), 30)]
    cal_idx = idx.append(pd.DatetimeIndex(future))
    slot = cash / EPS_SLOTS
    lines = ["", f"EPS PICKS - after a company reports, buy at the next open if the stock opened at least "
             f"{rule['min_gap']:.0%} higher and closed above its open; sell with a limit order at +{target:.0%} or at the "
             f"close {hold} sessions later",
             f"  backtest: won {pick['design']['win_rate']:.0%} of {pick['design']['trades']} trades 2011-2021, "
             f"{pick['test']['win_rate']:.0%} of {pick['test']['trades']} in 2021-2026 (avg {pct(pick['test']['avg'])} a trade, "
             f"worst {pct(pick['test']['worst'], 0)})"]
    past, buys, report_rows = [], [], []
    for r in calendar:
        s = r["symbol"]
        if s not in STOCKS or not np.isfinite(r["actual"]) or s not in c:
            continue
        rd = reaction_day(cal_idx, r["date"], r["timing"])
        if rd is None:
            continue
        surp = surprise(r["actual"], r["estimate"])
        beat = "BEAT" if r["actual"] > r["estimate"] else "MISS" if r["actual"] < r["estimate"] else "INLINE"
        i = cal_idx.get_loc(rd)
        if rd <= last:
            gap = o.at[rd, s] / c[s].iloc[i - 1] - 1
            held_up = c.at[rd, s] > o.at[rd, s]
        else:
            gap = moves.get(s, {}).get("total", float("nan")) if rd == pd.Timestamp(nxt) else float("nan")
            held_up = None
        eps_ok = rule.get("min_surprise") is None or (np.isfinite(surp) and surp >= rule["min_surprise"])
        qualifies = bool(eps_ok and np.isfinite(gap) and gap >= rule["min_gap"] and held_up)
        entry = cal_idx[i + 1]
        if rd >= pd.Timestamp(nxt) - pd.Timedelta(days=4) or rd == last:
            report_rows.append({"symbol": s, "report": f"{r['date']} {r['timing']}", "beat": beat, "surprise": surp,
                                "gap": gap, "pending": rd > last})
        if qualifies and entry <= last:
            past.append({"symbol": s, "entry": entry, "priority": -gap})
        elif qualifies and entry == pd.Timestamp(nxt):
            buys.append({"symbol": s, "gap": gap})
    if report_rows:
        lines.append("  Latest reports (59 stocks):")
        for r in sorted(report_rows, key=lambda r: r["report"]):
            g = f"{'pre-market' if r['pending'] else 'opened'} {pct(r['gap'])}"
            lines.append(f"    {r['symbol']:5s} {r['report']:13s} {r['beat']:6s} EPS surprise {pct(r['surprise'])}, {g}")
    held = replay(px, past, target, hold, EPS_SLOTS)
    pl, sells = position_lines("EPS pick", held, target, hold, nxt, idx, upcoming)
    plays = []
    for b in sorted(buys, key=lambda b: -b["gap"])[:max(EPS_SLOTS - len(held), 0)]:
        price = moves.get(b["symbol"], {}).get("price", float(c[b["symbol"]].iloc[-1]))
        n = int(slot // price)
        sell_by = trading_days_after(nxt, hold - 1)[-1] if hold > 1 else nxt
        lines.append(f"  BUY  {b['symbol']:5s} {n:,} sh ~${price:,.2f} = {money(n * price)} at {order_time(nxt)} (opened "
                     f"{pct(b['gap'])} after its report) | then a good-till-canceled limit SELL at +{target:.0%} above your fill "
                     f"(~${price * (1 + target):,.2f}); if it hasn't filled, sell at the close on {sell_by:%a %b %d}")
        plays.append({"symbol": b["symbol"], "price": price, "signal": "eps", "entry": nxt})
    lines += pl
    if not (plays or held):
        lines.append("  No EPS trades today.")
    return lines, plays, sells


def dip_section(px, moves, nxt, acc, cash, upcoming=()) -> tuple[list[str], list[dict], list[str]]:
    pick = acc["dip"]
    target, hold, rsi_max = pick["exit"]["target"], pick["exit"]["hold"], pick["rsi_max"]
    syms = list(POOLS[DIP.pool])
    close = px["close"][syms]
    ind = indicators(close)
    cfg = DIP.__class__(**{**DIP.__dict__, "entry_rsi": rsi_max})
    entry, _ = signals(close, ind, cfg)
    fresh = entry & ~entry.shift(1, fill_value=False)
    idx = close.index
    past = []
    for i in range(max(1, len(idx) - 60), len(idx) - 1):
        for s in fresh.columns[fresh.iloc[i].to_numpy()]:
            past.append({"symbol": s, "entry": idx[i + 1], "priority": ind["rsi2"].iat[i, ind["rsi2"].columns.get_loc(s)]})
    held = replay(px, past, target, hold, DIP.max_positions)
    lines = ["", f"REGULAR STOCK PICKS - buy sharp 1-3 day dips (2-day RSI under {rsi_max:.0f}) in the 10 strongest stocks/ETFs "
             f"above their 200-day average; sell with a limit order at +{target:.0%} or at the close {hold} sessions later",
             f"  backtest: won {pick['design']['win_rate']:.0%} of {pick['design']['trades']} trades 2011-2021, "
             f"{pick['test']['win_rate']:.0%} of {pick['test']['trades']} in 2021-2026 (avg {pct(pick['test']['avg'])} a trade, "
             f"worst {pct(pick['test']['worst'], 0)}; {pct(pick['test_portfolio']['cagr'])}/yr on its own money)"]
    pl, sells = position_lines("dip", held, target, hold, nxt, idx, upcoming)
    last = idx[-1]
    cands = fresh.loc[last]
    cands = [s for s in ind["rsi2"].loc[last, cands[cands].index].sort_values().index if s not in {a["symbol"] for a in held}]
    slot = cash / DIP.max_positions
    plays = []
    for s in cands[:max(DIP.max_positions - len(held), 0)]:
        price = moves.get(s, {}).get("price", float(px["close"][s].iloc[-1]))
        n = int(slot // price)
        sell_by = trading_days_after(nxt, hold - 1)[-1]
        lines.append(f"  BUY  {s:5s} {n:,} sh ~${price:,.2f} = {money(n * price)} at {order_time(nxt)} (2-day RSI "
                     f"{ind['rsi2'].at[last, s]:.0f}) | then a good-till-canceled limit SELL at +{target:.0%} above your fill "
                     f"(~${price * (1 + target):,.2f}); if it hasn't filled, sell at the close on {sell_by:%a %b %d}"
                     + earnings_between(s, nxt, sell_by, upcoming))
        plays.append({"symbol": s, "price": price, "signal": "dip", "entry": nxt})
    lines += pl
    if not (plays or held):
        lines.append(f"  No dip-buys today: none of the 10 strongest names has dipped hard enough (2-day RSI under {rsi_max:.0f}).")
    return lines, plays, sells


def spread_lines(sym, price, vol, pick, nxt, mult, risk_dollars, upcoming=()) -> list[str]:
    cfg = OptConfig(**pick["config"])
    exp = friday_expiry(nxt, cfg.dte)
    warn = earnings_between(sym, nxt, exp, upcoming)
    warn = f"{warn} before this expires - the backtest included such trades, but the risk is higher" if warn else ""
    days = max(1, np.busday_count(nxt, exp) + 1)
    t = days / 252
    iv = vol * mult
    if cfg.structure == "put_spread":
        ks, kl = strike(price * (1 - cfg.otm)), strike(price * (1 - cfg.otm - cfg.width))
        if kl >= ks:
            kl = ks - (1.0 if price < 100 else 2.5 if price < 250 else 5.0)
        ps, pl = (bs_price(price, k, t, implied_vol(iv, price, k, t), 0.04, False) for k in (ks, kl))
        credit = (ps - opt_half_spread(ps)) - (pl + opt_half_spread(pl))
        max_loss = (ks - kl - credit) * MULTIPLIER + 4 * COMMISSION
        n = int(risk_dollars // max_loss) if max_loss > 0 else 0
        tp = (f"; buy it back when it costs {1 - cfg.take_profit:.0%} of the credit (~${credit * (1 - cfg.take_profit):.2f})"
              if cfg.take_profit else "; hold to expiry")
        return [f"  {sym}: SELL {exp:%b %d} ${ks:g} put / BUY ${kl:g} put (bull put spread) at 9:35 AM ET, "
                f"est. credit ${credit:.2f}/share, max loss {money(max_loss)} per spread -> {n} spreads "
                f"(max loss {money(n * max_loss)}, credit ~{money(n * credit * 100)}){tp}. Wins if {sym} stays above ${ks:g}.{warn}"]
    k = strike(price * (1 + cfg.otm))
    mid = bs_price(price, k, t, implied_vol(iv, price, k, t), 0.04, True)
    cost = (mid + opt_half_spread(mid)) * MULTIPLIER + COMMISSION
    n = int(risk_dollars // cost)
    return [f"  {sym}: BUY {exp:%b %d} ${k:g} call at 9:35 AM ET, est. ${cost / 100:.2f}/share -> {n} contracts "
            f"({money(n * cost)}){warn}"]


def options_section(px, plays, nxt, summary, moves, upcoming=()) -> list[str]:
    picks = summary.get("options_pick", {})
    mults = summary.get("option_pricing", {})
    lines = ["", "STOCK OPTIONS PLAYS (on today's picks; margin account; strikes rounded - use the nearest listed strike)"]
    vol = calm_vol(px["close"][[p["symbol"] for p in plays] or ["SPY"]]).iloc[-1]
    shown = False
    for p in plays:
        pick = picks.get(p["signal"])
        if not pick or p["symbol"] not in STOCKS:
            continue
        m = mults["fair"][pick["config"]["signal"]] * (1 + mults["premium"]) / 2
        lines += spread_lines(p["symbol"], p["price"], float(vol[p["symbol"]]), pick, p["entry"], m, ACCOUNT * STOCK_OPT_RISK,
                              upcoming)
        shown = True
    for sig in ("dip", "eps"):
        pick = picks.get(sig)
        if pick:
            lines.append(f"  rule for {sig} picks: {pick['label']} - won {pick['design_mid']['win_rate']:.0%} of "
                         f"{pick['design_mid']['trades']} trades 2011-2021 and {pick['test_mid']['win_rate']:.0%} of "
                         f"{pick['test_mid']['trades']} in 2021-2026 (fair pricing: {pick['test_fair']['win_rate']:.0%}); "
                         f"avg {pct(pick['test_mid']['avg'])} of the max loss per trade, worst {pct(pick['test_mid']['worst_trade'], 0)}")
    if not shown:
        lines.insert(2, "  No new stock picks today, so no new options plays.")
    return lines


def qqq_section(px, vix_frame, moves, nxt, summary) -> list[str]:
    from .vol_alert import monday_table
    lines = ["", "QQQ OPTIONS"]
    qqq = pd.DataFrame({k: px[k]["QQQ"] for k in ("open", "high", "low", "close", "volume")}).dropna(subset=["close"])
    spy = pd.DataFrame({k: px[k]["SPY"] for k in ("open", "high", "low", "close", "volume")}).dropna(subset=["close"])
    end = min(qqq.index[-1], spy.index[-1], vix_frame.index[-1])
    gap = moves.get("QQQ", {}).get("total")
    base = {-0.01, -0.005, 0.0, 0.005, 0.01}
    near = gap is not None and np.isfinite(gap) and min(abs(gap - g) for g in base) < 0.001
    gaps = sorted(base | ({round(gap, 4)} if gap is not None and np.isfinite(gap) and not near else set()))
    acct = [Account("Size", ACCOUNT, 1.0, CONDOR_RISK, True)]
    table = [l.replace(" Tuesday-Friday: same rule each morning.", "")
              .replace(f"({CONDOR_RISK:.2%} of {money(ACCOUNT)})", f"({CONDOR_RISK:.0%} of the account)")
             for l in monday_table(qqq.loc[:end], spy.loc[:end], vix_frame.loc[:end], acct, gaps=gaps)]
    if gap is not None and np.isfinite(gap):
        row = min(gaps, key=lambda g: abs(g - gap))
        table.insert(1, f"  QQQ is now {pct(gap, 2)} vs yesterday's close -> use the {pct(row, 1)} row")
    lines += ["  1) " + table[0]] + table[1:]
    pick = summary.get("options_pick", {}).get("qqq")
    if pick:
        cfg = OptConfig(**pick["config"])
        q = px["close"]["QQQ"].dropna()
        up = q.iloc[-1] > q.iloc[-200:].mean()
        r2 = rsi(q.to_frame())["QQQ"]
        fresh_dip = up and r2.iloc[-1] < 10 and not (r2.iloc[-2] < 10 and q.iloc[-2] > q.iloc[-201:-1].mean())
        first_of_week = pd.Timestamp(nxt).to_period("W") != q.index[-1].to_period("W")
        fires = (cfg.signal == "qqq_dip" and fresh_dip) or (cfg.signal == "qqq_weekly" and up and first_of_week)
        lines.append(f"  2) QQQ {cfg.label().split(': ', 1)[1]} ({'dips' if cfg.signal == 'qqq_dip' else 'every week'} while QQQ is "
                     f"above its 200-day average): won {pick['design_mid']['win_rate']:.0%} of trades 2011-2021, "
                     f"{pick['test_mid']['win_rate']:.0%} of {pick['test_mid']['trades']} in 2021-2026")
        if fires:
            price = moves.get("QQQ", {}).get("price", float(q.iloc[-1]))
            vol = float(calm_vol(px["close"][["QQQ"]]).iloc[-1]["QQQ"])
            mults = summary["option_pricing"]
            m = mults["fair"][cfg.signal] * (1 + mults["premium"]) / 2
            lines += spread_lines("QQQ", price, vol, pick, nxt, m, ACCOUNT * QQQ_OPT_RISK)
        else:
            lines.append("     no signal today")
    return lines


def patterns_section(px, acc, top: int = 12) -> list[str]:
    stocks = [s for s in STOCKS if s in px["close"]]
    frames = pattern_frames(px, stocks)
    rules = acc["patterns"]
    last = px["close"].index[-1]
    lines = ["", f"PATTERNS SPOTTED at the {last.date()} close - each as a trade: buy at the next open, sell with a limit "
             "order at the target or at the close when time runs out (track record across the 59 stocks):"]
    hits = []
    for name, f in frames.items():
        r = rules.get(name)
        if not r or r["design_win_rate"] < 0.70:
            continue
        on = f.loc[last]
        for sym in on[on].index:
            hits.append((r["test_win_rate"], sym, name, r))
    for _, sym, name, r in sorted(hits, key=lambda x: -x[0])[:top]:
        flat = " (about break-even on average)" if r["test_avg"] <= 0.001 else ""
        lines.append(f"  {sym:5s} {name}: {r['label']} -> won {r['test_win_rate']:.0%} of {r['test_trades']:,} trades in 2021-2026 "
                     f"({r['design_win_rate']:.0%} in 2011-2021), avg {pct(r['test_avg'])}{flat}")
    if not hits:
        lines.append("  none of the tested patterns today")
    return lines


def beat_record(past: list[dict]) -> dict:
    """{symbol: list of 1/0 beats, oldest first} from the saved history plus recent reports."""
    rows = {}
    if EPS_HISTORY.exists():
        for r in json.loads(EPS_HISTORY.read_text()):
            e, a = (r.get("eps") or {}).get("estimate"), (r.get("eps") or {}).get("actual")
            if e not in (None, "") and a not in (None, ""):
                rows[(r["symbol"], r["report"]["date"])] = float(a) > float(e)
    for r in past:
        if np.isfinite(r["actual"]) and np.isfinite(r["estimate"]) and r["date"]:
            rows[(r["symbol"], r["date"])] = r["actual"] > r["estimate"]
    out = {}
    for (sym, d), b in sorted(rows.items(), key=lambda kv: kv[0][1]):
        out.setdefault(sym, []).append(int(b))
    return out


def calendar_section(calendar, nxt, acc=None, past=()) -> list[str]:
    lines = ["", "EARNINGS COMING UP (59 stocks)"]
    bt = (acc or {}).get("beats")
    record = beat_record(list(past)) if bt else {}
    rows = [r for r in calendar if r["symbol"] in STOCKS and not np.isfinite(r["actual"]) and r["date"]
            and r["date"] >= dt.date.today().isoformat()]
    for r in rows[:20]:
        call = ""
        hist = record.get(r["symbol"], [])[-bt["n"]:] if bt else []
        if bt and len(hist) == bt["n"]:
            k = sum(hist)
            call = (f" | beat {k} of last {bt['n']} -> likely BEAT (right {bt['test_accuracy']:.0%} in 2021-2026)"
                    if k / bt["n"] >= bt["min_rate"] else f" | beat {k} of last {bt['n']}")
        lines.append(f"  {r['symbol']:5s} {r['date']} {r['timing']:3s} EPS estimate {r['estimate']:.2f}{call}")
    if not rows:
        lines.append("  none in the next 7 days")
    elif bt:
        lines.append(f"  (All reports beat their estimate {bt['test_base_rate']:.0%} of the time in 2021-2026.)")
    return lines


# --------------------------------------------------------------------- main
def build(ext_paths, past_json, next_json, now: pd.Timestamp | None = None) -> tuple[str, str]:
    summary = json.loads(SUMMARY.read_text())
    acc = json.loads(ACCURACY.read_text())
    syms = list(dict.fromkeys(list(POOLS["stocks+etfs"]) + ["SPY", "QQQ"] + list(SECTORS)
                              + [s for s in list(ASIA) + list(ASIA_ADRS) if (DATA_DIR / f"{s}.csv").exists()]))
    px = load_prices(syms)
    now = now or pd.Timestamp.now(tz=ET)
    ext = load_extended(ext_paths)
    px = complete_daily(px, ext, now)
    cov = px["close"][list(STOCKS)].notna().mean(axis=1)
    px = {k: v.loc[:cov[cov >= 0.9].index[-1]] for k, v in px.items()}       # drop a half-loaded day
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    vix_frame = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")[["open", "close"]]
    vix = vix_frame["close"]
    last = px["close"].index[-1]
    nxt = next_trading_day(last.date())
    if pd.Timestamp(f"{nxt} 09:35", tz=ET) < now:
        nxt = next_trading_day(now.date() - dt.timedelta(days=1)) if now.hour < 9 else next_trading_day(now.date())
    moves = overnight_moves(ext)
    past = load_calendar(past_json) if past_json else []
    upcoming = load_calendar(next_json) if next_json else []

    head = [f"DAILY TRADING PLAN for {nxt:%a %b %d, %Y} (orders at 9:35 AM ET unless noted)",
            f"Data: daily closes through {last:%a %b %d}; after-hours/pre-market as of "
            + (max(m["as_of"] for m in moves.values()).strftime("%a %I:%M %p ET") if moves else "n/a")
            + ("" if is_trading_day(now.date()) else f". Markets are closed today ({now:%A}); this plan is for {nxt:%A}."),
            "Sizing: half the account in EPS picks (5 equal slots), half in dip-buys (3 equal slots); "
            "each options play risks at most 1% of the account; 0DTE QQQ condors at most 2%.", ""]
    over, asia_live = overnight_section(px, moves, summary, acc)
    mkt = market_section(px, vix, asia_live, nxt, summary, acc)
    eps_lines, eps_plays, eps_sells = eps_section(px, past, moves, nxt, acc, ACCOUNT * EPS_SHARE, upcoming)
    dip_lines, dip_plays, dip_sells = dip_section(px, moves, nxt, acc, ACCOUNT * DIP_SHARE, upcoming)
    opt = options_section(px, eps_plays + dip_plays, nxt, summary, moves, upcoming)
    qqq = qqq_section(px, vix_frame, moves, nxt, summary)
    pats = patterns_section(px, acc)
    cal = calendar_section(upcoming, nxt, acc, past)
    text = "\n".join(head + mkt + over + eps_lines + dip_lines + opt + qqq + pats + cal)   # ends like the email as sent
    buys = [p["symbol"] for p in eps_plays + dip_plays]
    sells = eps_sells + dip_sells
    actions = ([f"BUY {', '.join(buys)}"] if buys else []) + ([f"SELL {', '.join(sells)}"] if sells else [])
    subject = f"Daily plan {nxt:%a %b %d}: " + (" | ".join(actions) or "no new stock trades")
    return subject, text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extended", nargs="*", default=[], help="saved 24-hour hourly bars")
    ap.add_argument("--earnings-json", nargs=2, metavar=("PAST", "NEXT"), default=(None, None))
    ap.add_argument("--text-out", default=None)
    args = ap.parse_args()
    subject, text = build(args.extended, *args.earnings_json)
    if args.text_out:
        Path(args.text_out).write_text(f"Subject: {subject}\n\n{text}")
    print(f"Subject: {subject}\n\n{text}")


if __name__ == "__main__":
    main()
