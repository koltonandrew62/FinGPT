"""Daily trading plan email (Sunday-Friday, 8:30 AM ET; sized for a $1.5M margin account):
market trends and patterns, Asia overnight, after-hours and pre-market moves, EPS
picks, regular stock picks (dip-buys), options plays on those picks, QQQ options and
the earnings calendar. Rules and win rates come from daily_research.py
(results/daily/summary.json).

Inputs, saved by the scheduled run from the Robinhood connector (read-only):
  data/*.csv               daily bars (import_robinhood)
  --extended FILE ...      24-hour 30-minute bars, get_equity_historicals interval=30minute bounds=24_5
  --earnings-json PAST NEXT  get_earnings_calendar days=-31 and days=31
  --holdings FILE          optional: shares actually held, one 'SYMBOL SHARES' per line
  --journal                record today's recommendations (results/journal/recommendations.csv)

The stock list is every S&P 500, Nasdaq-100 and Dow 30 company (universe.py).

    python -m schwab_trader.daily_report --extended e1.txt e2.txt ... --earnings-json past.txt next.txt --text-out /tmp/daily.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar_utils import is_trading_day, next_trading_day, order_time
from .config import DATA_DIR, RESULTS_DIR
from .daily_research import (DIP, STOCKS, OptConfig, calm_vol, opt_half_spread, reaction_day, surprise)
from .data import load_prices
from .options_backtest import COMMISSION, MULTIPLIER, bs_price, implied_vol
from .patterns import (ASIA, ASIA_ADRS, SECTORS, analog_forecast, asia_impact, asia_overnight, market_features,
                       market_outcomes, trend_label)
from .pullback import POOLS, indicators, rsi, signals
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
    lines = ["", "MARKET TRENDS (background only - the trade calls are in WHAT TO DO above)"]
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
        lines.append("    stocks most affected (expected opening move only, not a trade call): " + ", ".join(f"{s} {pct(v)}"
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
        lines.append("  Biggest moves among the S&P 500 / Nasdaq-100 / Dow stocks:")
        for s, m in movers:
            lines.append(f"    {s:5s} {pct(m['total']):>7s} (after-hours {pct(m['post'])}, overnight {pct(m['overnight'])}, "
                         f"pre-market {pct(m['pre'])}) ${m['price']:,.2f}")
        gf = {(r["gap"], r["window"]): r["fill_rate"] for r in acc.get("gap_fill", [])}
        big = gf.get(("2.0%-4.0%", "test"))
        if big is not None:
            lines.append(f"    Gaps of 2-4% without earnings news stayed open (didn't trade back to yesterday's close) "
                         f"{1 - big:.0%} of the time in 2021-2026; gaps under 1% closed {gf.get(('0.5%-1.0%', 'test'), 0):.0%} of the time.")
    return lines, composite


def replay(px, entries: list[dict], target: float, hold: int, slots: int, stop: float | None = None,
           short: bool = False) -> list[dict]:
    """Replay past entries (dicts with symbol, entry, priority) with a +target limit sell,
    an optional -stop stop order and a `hold`-session limit, keeping at most `slots` open.
    Returns the positions still open after the last close, each with its entry price and
    time-limit session index."""
    idx = px["close"].index
    o, h, lo = px["open"], px["high"], px["low"]
    active = []
    for e in sorted(entries, key=lambda e: (e["entry"], e.get("priority", 0))):
        i0 = idx.get_loc(e["entry"])
        active = [a for a in active if a["exit_i"] is None or a["exit_i"] >= i0]
        if len(active) >= slots or any(a["symbol"] == e["symbol"] for a in active):
            continue
        px0 = o.at[e["entry"], e["symbol"]]
        if not np.isfinite(px0):
            continue
        sgn = -1 if short else 1
        tgt = px0 * (1 + sgn * target) if target else (-np.inf if short else np.inf)
        stp = px0 * (1 - sgn * stop) if stop else (np.inf if short else -np.inf)
        exit_i = None
        for i in range(i0, min(i0 + hold, len(idx))):
            j = h.columns.get_loc(e["symbol"])
            hit = (lo.iat[i, j] <= tgt or h.iat[i, j] >= stp) if short else (h.iat[i, j] >= tgt or lo.iat[i, j] <= stp)
            if hit:
                exit_i = i
                break
        if exit_i is None and i0 + hold - 1 <= len(idx) - 1:
            exit_i = i0 + hold - 1
        active.append({**e, "px0": px0, "target_px": tgt, "stop_px": stp if stop else None, "i0": i0, "exit_i": exit_i})
    return [a for a in active if a["exit_i"] is None]


def position_lines(kind: str, pos: list[dict], target: float, hold: int, nxt: dt.date, idx, upcoming,
                   holdings: dict | None = None, short: bool = False) -> tuple[list[str], list[str], list[str]]:
    """HOLD / SELL lines for the positions the rules opened earlier. With `holdings`
    (shares actually owned, from the holdings email) the lines say exactly what is owned
    and positions not owned are left out."""
    lines, sells, holds = [], [], []
    act_close, act_hold, have = ("COVER", "STAY SHORT", "if you're short") if short else ("SELL", "HOLD", "if you own it")
    for a in pos:
        s = a["symbol"]
        own = None if holdings is None else holdings.get(s, 0)
        if own == 0 and not short:
            continue
        mine = have if own is None or short else f"(you hold {own:,.0f} sh)"
        held = len(idx) - a["i0"]                              # sessions held through the last close
        last_day = trading_days_after(idx[-1].date(), max(hold - held, 1))[-1]
        warn = earnings_between(s, nxt, last_day, upcoming)
        stop = (f", and the {'buy-' if short else ''}stop at ${a['stop_px']:,.2f}"
                if a.get("stop_px") is not None and np.isfinite(a["stop_px"]) else "")
        sign = "-" if short else "+"
        if hold - held <= 1:
            lines.append(f"  {act_close} {s:5s} at the close {order_time(nxt).replace('9:35 AM', '3:55 PM')} {mine} "
                         f"if the {sign}{target:.0%} limit (${a['target_px']:,.2f}) hasn't filled ({kind} from {a['entry'].date()})")
            sells.append(s)
        else:
            skip = " If you don't have it, skip it - its entry has passed." if own is None or short else ""
            lines.append(f"  {act_hold} {s:5s} {mine} ({kind} from {a['entry'].date()} at ~${a['px0']:,.2f}): keep the "
                         f"limit order at ${a['target_px']:,.2f} ({sign}{target:.0%}){stop}; otherwise "
                         f"{'cover' if short else 'sell'} at the close on {last_day:%a %b %d}.{skip}{warn}")
            holds.append(s)
    return lines, sells, holds


def recommended(kinds) -> set:
    """(symbol, entry day) of every recommendation an email actually made (journal)."""
    from .journal import load
    j = load()
    j = j[j.kind.isin(list(kinds))]
    return {(r.symbol, pd.Timestamp(r.plan_date)) for r in j.itertuples()}


def only_recommended(entries: list[dict], kinds) -> list[dict]:
    """Keep the positions an email told him to open: no HOLD/SELL lines for trades he was never told about."""
    rec = recommended(kinds)
    return [e for e in entries if (e["symbol"], pd.Timestamp(e["entry"])) in rec]


def earnings_within(sym: str, start: dt.date, sessions: int, upcoming: list[dict]) -> bool:
    end = trading_days_after(start, max(sessions - 1, 1))[-1] if sessions > 1 else start
    return bool(earnings_between(sym, start, end, upcoming))


def screen(sym, nxt, kind, rule, px, vix, asia_live, gap, upcoming, learned, hold) -> list[str]:
    """Reasons to skip a candidate: the rule's own filters, then the learned conditions."""
    why = []
    spy = px["close"]["SPY"].dropna()
    if rule.get("spy_trend") and not spy.iloc[-1] > spy.iloc[-200:].mean():
        why.append("the S&P 500 is below its 200-day average (this rule only buys in an uptrend)")
    if rule.get("no_earnings") and earnings_within(sym, nxt, hold, upcoming):
        why.append("it reports earnings before the trade would end (this rule skips those)")
    info = (learned or {}).get(kind) or {}
    if info.get("use_live") and info.get("rules"):
        from .learning import CONDITIONS, matches
        hit = matches(px, vix, asia_live if asia_live is not None else np.nan, sym, nxt, info["rules"], gap,
                      earnings_within(sym, nxt, hold, upcoming), hold)
        why += [f"learned: {CONDITIONS[c]}" for c in hit]
    return why


def exit_text(target, stop, hold) -> str:
    st = f", a stop at -{stop:.0%}" if stop else ""
    return f"sell with a limit order at +{target:.0%}{st} or at the close {hold} sessions later"


def buy_line(s, n, price, nxt, target, stop, hold, why) -> str:
    sell_by = trading_days_after(nxt, hold - 1)[-1] if hold > 1 else nxt
    st = f" and a stop at ~${price * (1 - stop):,.2f} (-{stop:.0%})" if stop else ""
    return (f"  BUY  {s:9s} {n:,} sh ~${price:,.2f} = {money(n * price)} at {order_time(nxt)} ({why}) | then a "
            f"good-till-canceled limit SELL at +{target:.0%} above your fill (~${price * (1 + target):,.2f}){st}; if neither "
            f"fills, sell at the close on {sell_by:%a %b %d}")


def eps_section(px, calendar, moves, nxt, acc, cash, upcoming=(), ctx=None) -> tuple[list[str], list[dict], list[str], list[str]]:
    ctx = ctx or {}
    pick = acc["eps"]
    rule, ex = pick["rule"], pick["exit"]
    target, stop, hold = ex["target"], ex.get("stop"), ex["hold"]
    c, o = px["close"], px["open"]
    idx = c.index
    last = idx[-1]
    future = [pd.Timestamp(d) for d in trading_days_after(last.date(), 30)]
    cal_idx = idx.append(pd.DatetimeIndex(future))
    size, size_note = ctx.get("size", {}).get("eps", (1.0, ""))
    slot = cash / EPS_SLOTS * size
    trend = " Only while the S&P 500 is above its 200-day average." if pick.get("spy_trend") else ""
    lines = ["", f"EPS PICKS - after a company reports, buy at the next open if the stock opened at least "
             f"{rule['min_gap']:.0%} higher and closed above its open; {exit_text(target, stop, hold)}.{trend}",
             f"  backtest: won {pick['design']['win_rate']:.0%} of {pick['design']['trades']} trades 2011-2021, "
             f"{pick['test']['win_rate']:.0%} of {pick['test']['trades']} in 2021-2026 (avg {pct(pick['test']['avg'])} a trade, "
             f"worst {pct(pick['test']['worst'], 0)})"]
    if size_note:
        lines.append(f"  ! {size_note}")
    past, buys, report_rows = [], [], []
    spy = c["SPY"]
    up = (spy > spy.rolling(200).mean())
    for r in calendar:
        s = r["symbol"]
        if s not in STOCKS or not np.isfinite(r["actual"]) or s not in c:
            continue
        rd = reaction_day(cal_idx, r["date"], r["timing"])
        if rd is None:
            continue
        surp = surprise(r["actual"], r["estimate"])
        beat = "B" if r["actual"] > r["estimate"] else "M" if r["actual"] < r["estimate"] else "="
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
        if qualifies and entry <= last and (not pick.get("spy_trend") or bool(up.get(rd, True))):
            past.append({"symbol": s, "entry": entry, "priority": -gap})
        elif qualifies and entry == pd.Timestamp(nxt):
            buys.append({"symbol": s, "gap": gap, "beat": beat})
    if report_rows:
        beats = sum(r["beat"] == "B" for r in report_rows)
        lines.append(f"  Latest reports: {len(report_rows)} companies, {beats} beat their EPS estimate (B = beat, M = missed, "
                     f"= in line). Those moving {min(rule['min_gap'], 0.03):.0%}+ (the ones that can qualify):")
        big = [r for r in report_rows if np.isfinite(r["gap"]) and abs(r["gap"]) >= min(rule["min_gap"], 0.03)]
        for r in sorted(big, key=lambda r: -r["gap"])[:15]:
            g = f"{'pre-market' if r['pending'] else 'opened'} {pct(r['gap'])}"
            lines.append(f"    {r['symbol']:5s} [{r['beat']}] {r['report']:13s} EPS surprise {pct(r['surprise'])}, {g}")
        if not big:
            lines.append("    none")
    held = replay(px, only_recommended(past, ["eps"]), target, hold, EPS_SLOTS, stop)
    pl, sells, holds = position_lines("EPS pick", held, target, hold, nxt, idx, upcoming, ctx.get("holdings"))
    plays = []
    for b in sorted(buys, key=lambda b: -b["gap"]):
        if len(plays) >= max(EPS_SLOTS - len(held), 0):
            break
        sym = b["symbol"]
        why = screen(sym, nxt, "eps", pick, px, ctx.get("vix"), ctx.get("asia"), moves.get(sym, {}).get("total"), upcoming,
                     ctx.get("learned"), hold)
        if why:
            lines.append(f"  SKIP {sym:5s} (qualified, but " + "; ".join(why) + ")")
            continue
        price = moves.get(sym, {}).get("price", float(c[sym].iloc[-1]))
        n = int(slot // price)
        lines.append(buy_line(f"{sym} [{b['beat']}]", n, price, nxt, target, stop, hold, f"opened {pct(b['gap'])} after its report"))
        plays.append({"symbol": sym, "price": price, "signal": "eps", "entry": nxt, "shares": n, "tag": b["beat"],
                      "target": target, "stop": stop, "hold": hold, "rule": pick["label"]})
    lines += pl
    if not (plays or held):
        lines.append("  No EPS trades today.")
    return lines, plays, sells, holds


def dip_section(px, moves, nxt, acc, cash, upcoming=(), ctx=None, new_buys: bool = True) -> tuple[list[str], list[dict], list[str], list[str]]:
    ctx = ctx or {}
    pick = acc["dip"]
    target, stop, hold, rsi_max = pick["exit"]["target"], pick["exit"].get("stop"), pick["exit"]["hold"], pick["rsi_max"]
    top = pick.get("momentum_top", DIP.momentum_top)
    syms = [s for s in POOLS[DIP.pool] if s in px["close"]]
    close = px["close"][syms]
    ind = indicators(close)
    cfg = DIP.__class__(**{**DIP.__dict__, "entry_rsi": rsi_max, "momentum_top": top})
    entry, _ = signals(close, ind, cfg)
    fresh = entry & ~entry.shift(1, fill_value=False)
    idx = close.index
    spy = px["close"]["SPY"]
    up = (spy > spy.rolling(200).mean())
    cal = list(ctx.get("past_calendar", [])) + list(upcoming)
    past = []
    for i in range(max(1, len(idx) - 60), len(idx) - 1):
        if pick.get("spy_trend") and not up.iloc[i]:
            continue
        for s in fresh.columns[fresh.iloc[i].to_numpy()]:
            if pick.get("no_earnings") and earnings_within(s, idx[i + 1].date(), hold, cal):
                continue
            past.append({"symbol": s, "entry": idx[i + 1], "priority": ind["rsi2"].iat[i, ind["rsi2"].columns.get_loc(s)]})
    held = replay(px, only_recommended(past, ["dip"]), target, hold, DIP.max_positions, stop)
    filt = (" Skips stocks that report earnings during the trade." if pick.get("no_earnings") else "") + \
           (" Only while the S&P 500 is above its 200-day average." if pick.get("spy_trend") else "")
    lines = ["", f"REGULAR STOCK PICKS - buy sharp 1-3 day dips (2-day RSI under {rsi_max:.0f}) in the {top} strongest of "
             f"the S&P 500, Nasdaq-100 and Dow stocks (and 17 ETFs) above their 200-day average; "
             f"{exit_text(target, stop, hold)}.{filt}",
             f"  backtest: won {pick['design']['win_rate']:.0%} of {pick['design']['trades']} trades 2011-2021, "
             f"{pick['test']['win_rate']:.0%} of {pick['test']['trades']} in 2021-2026 (avg {pct(pick['test']['avg'])} a trade, "
             f"worst {pct(pick['test']['worst'], 0)}; {pct(pick['test_portfolio']['cagr'])}/yr on its own money)"]
    size, size_note = ctx.get("size", {}).get("dip", (1.0, ""))
    if size_note:
        lines.append(f"  ! {size_note}")
    pl, sells, holds = position_lines("dip", held, target, hold, nxt, idx, upcoming, ctx.get("holdings"))
    if not new_buys:
        if not pl:
            return [], [], [], []
        return (["", "EARLIER DIP-BUYS STILL OPEN (the dip-buy list was replaced by the top 5 below; manage these to the end)"]
                + pl, [], sells, holds)
    last = idx[-1]
    cands = fresh.loc[last]
    cands = [s for s in ind["rsi2"].loc[last, cands[cands].index].sort_values().index if s not in {a["symbol"] for a in held}]
    slot = cash / DIP.max_positions * size
    plays = []
    for s in cands:
        if len(plays) >= max(DIP.max_positions - len(held), 0):
            break
        why = screen(s, nxt, "dip", pick, px, ctx.get("vix"), ctx.get("asia"), moves.get(s, {}).get("total"), upcoming,
                     ctx.get("learned"), hold)
        if why:
            lines.append(f"  SKIP {s:5s} (2-day RSI {ind['rsi2'].at[last, s]:.0f}, but " + "; ".join(why) + ")")
            continue
        price = moves.get(s, {}).get("price", float(px["close"][s].iloc[-1]))
        n = int(slot // price)
        lines.append(buy_line(s, n, price, nxt, target, stop, hold, f"2-day RSI {ind['rsi2'].at[last, s]:.0f}")
                     + earnings_between(s, nxt, trading_days_after(nxt, hold - 1)[-1], upcoming))
        plays.append({"symbol": s, "price": price, "signal": "dip", "entry": nxt, "shares": n,
                      "target": target, "stop": stop, "hold": hold, "rule": pick["label"]})
    lines += pl
    if not (plays or held):
        lines.append(f"  No dip-buys today: none of the {top} strongest names has dipped hard enough (2-day RSI under {rsi_max:.0f}).")
    return lines, plays, sells, holds


TOP5_DIR = RESULTS_DIR / "top5"
SHORTS_DIR = RESULTS_DIR / "shorts"
TOP5_SHARE, TOP5_SLOTS, TOP5_N = 0.5, 10, 5   # half the account, 10 equal slots ($75,000); 5 picks a day
OPTION_BUY_RISK = 0.01                # calls/puts bought: at most 1% of the account each


def option_buy_line(sym, price, vol, mult, cfg, nxt, side: str, risk_dollars) -> tuple[str, dict]:
    """Price a call (buys) or put (shorts) for the email and the journal."""
    call = side == "buy"
    k = strike(price * (1 + cfg["otm"])) if call else strike(price * (1 - cfg["otm"]))
    exp = friday_expiry(nxt, int(cfg["dte"]))
    days = max(1, np.busday_count(nxt, exp) + 1)
    t = days / 252
    mid = bs_price(price, k, t, implied_vol(vol * mult, price, k, t), 0.04, call)
    cost = mid + opt_half_spread(mid)
    n = int(risk_dollars // (cost * MULTIPLIER + COMMISSION)) if cost > 0 else 0
    tp = f"; sell it when it's up {cfg['take_profit']:.0%}" if cfg.get("take_profit") else ""
    hold = f", or after {int(cfg['hold'])} sessions" if cfg.get("hold") else ", or at expiry"
    text = (f"buy {n} {exp:%b %d} ${k:g} {'calls' if call else 'puts'} at ~${cost:.2f} ({money(n * cost * 100)}, "
            f"the most you can lose){tp}{hold}")
    return text, {"expiry": exp.isoformat(), ("call" if call else "put"): k, "contracts": n, "cost": cost}


def top5_section(px, moves, nxt, cash, upcoming, ctx, journal_rows, exclude=()) -> tuple[list[str], dict]:
    """The day's five stocks to buy (best-tested signals first), earlier picks still open,
    and calls yes or no. `exclude`: stocks already bought or held today."""
    from .top5_research import candidates, pick_daily
    out = {"buys": [], "shorts": [], "sells": [], "covers": [], "holds": [], "short_holds": [], "options": []}
    if not (TOP5_DIR / "summary.json").exists():
        return [], out
    sm = json.loads((TOP5_DIR / "summary.json").read_text())
    acc = ctx["acc"]
    idx = px["close"].index
    last = idx[-1]
    picks = pick_daily(candidates(px, acc, None, ctx["events"], days=45, no_earnings=sm.get("no_earnings", False)), acc, idx,
                       exclude_today=set(exclude))
    b = sm["buy"]
    lines = ["", "TOP 5 STOCKS TO BUY TODAY - the day's five best signals from the rules that tested best (each rule's own "
             "limit sell and time limit, plus a -15% stop)",
             f"  record of this list: won {b['design']['win_rate']:.0%} of {b['design']['trades']:,} picks in 2011-2021 and "
             f"{b['test']['win_rate']:.0%} of {b['test']['trades']:,} in 2021-2026 (avg {pct(b['test']['avg'])} a pick, worst "
             f"{pct(b['test']['worst'], 0)}; {pct(b['test_portfolio']['cagr'])}/yr on its own money)",
             "  No shorts here: no short-sale rule made money in testing (see SHORT IDEAS)."]
    past = picks[picks.date < last]
    for (t, st, h), g in past.groupby(["x_target", "x_stop", "x_hold"], dropna=False):
        ent = [{"symbol": r.symbol, "entry": idx[idx.get_loc(r.date) + 1], "priority": -r.avg} for r in g.itertuples()]
        held = replay(px, only_recommended(ent, ["top5_buy"]), None if pd.isna(t) else float(t), int(h), TOP5_SLOTS, float(st))
        pl, closes, holds = position_lines("top-5 pick", held, 0.0 if pd.isna(t) else float(t), int(h), nxt, idx, upcoming,
                                           ctx.get("holdings"))
        lines += pl
        out["sells"] += closes
        out["holds"] += holds
    today = picks[picks.date == last]
    slot = cash / TOP5_SLOTS
    vol = calm_vol(px["close"][[s for s in STOCKS if s in px["close"]]].iloc[-120:]).iloc[-1]
    o = sm.get("options", {}).get("call")
    mults = sm.get("option_pricing", {})
    from .journal import load as load_journal
    jj = load_journal()
    open_spreads = int(((jj.kind == "top5_put_spread") & (jj.status == "open")).sum()) if len(jj) else 0
    if sm.get("put_spread"):
        ps = sm["put_spread"]["mid"]
        lines.append(f"  Put spread on each pick (sell a put 8% below, buy one 5% further down, ~1 week, buy it back at half "
                     f"the credit; 1% of the account at risk, at most 5 open): won {ps['design']['win_rate']:.0%} in 2011-2021 "
                     f"and {ps['test']['win_rate']:.0%} of {ps['test']['trades']:,} in 2021-2026")
    if o and not o["use"]:
        lines.append(f"  Calls: NO - buying calls on picks like these lost money at real-world option prices (best call "
                     f"rule: {pct(o['design_avg_mid'], 0)} of the cost a trade in 2011-2021, {pct(o['test_avg_mid'], 0)} in "
                     "2021-2026). Buy the shares; the put spreads are the options play.")
    lines.append(f"  Today ({money(slot)} each):")
    for p in today.itertuples():
        price = moves.get(p.symbol, {}).get("price", float(px["close"][p.symbol].iloc[-1]))
        n = int(slot // price)
        tgt = None if pd.isna(p.x_target) else float(p.x_target)
        rec = sm.get("by_rule", {}).get(p.rule, {}).get("test", {})
        why = f"{p.rule}; this setup won {rec['win_rate']:.0%} in 2021-2026" if rec.get("trades") else p.rule
        lines.append(buy_line(p.symbol, n, price, nxt, tgt or 0.0, float(p.x_stop), int(p.x_hold), why)
                     + earnings_between(p.symbol, nxt, trading_days_after(nxt, int(p.x_hold) - 1)[-1], upcoming))
        out["buys"].append(f"{p.symbol} {n:,} sh")
        journal_rows.append({"plan_date": nxt.isoformat(), "kind": "top5_buy", "symbol": p.symbol, "ref_price": price,
                             "target": tgt, "stop": float(p.x_stop), "hold": int(p.x_hold), "rule": p.rule})
        psp = sm.get("put_spread")
        if psp and p.symbol in vol and np.isfinite(vol[p.symbol]):
            if open_spreads >= 5:
                lines.append("        put spread: skip - 5 are already open (the most at once)")
            else:
                f = mults["fair"]["top5"]
                lines += ["      " + l.strip() for l in spread_lines(
                    p.symbol, price, float(vol[p.symbol]), psp, nxt, f * (1 + mults["premium"]) / 2, ACCOUNT * STOCK_OPT_RISK,
                    upcoming, journal_rows, "top5_put_spread", f)]
                open_spreads += 1
                out["options"].append(f"{p.symbol} put spread")
        if not o or not o["use"]:
            continue
        if p.avg < o["min_rule_avg"]:
            lines.append(f"        calls: NO - calls only paid on the strongest setups (2011-2021 average {pct(o['min_rule_avg'])}+)")
        elif p.symbol in vol and np.isfinite(vol[p.symbol]):
            m = mults["fair"]["top5"] * (1 + mults["premium"]) / 2
            cfg = {"otm": o["otm"], "dte": o["dte"], "take_profit": o["take_profit"], "hold": o["hold"]}
            text, legs = option_buy_line(p.symbol, price, float(vol[p.symbol]), m, cfg, nxt, "buy", ACCOUNT * OPTION_BUY_RISK)
            lines.append(f"        calls: YES - {text}. Backtest: won {o['test_win_rate_mid']:.0%} of the time in 2021-2026, "
                         f"avg {pct(o['test_avg_mid'])} of the cost")
            out["options"].append(f"{p.symbol} calls")
            journal_rows.append({"plan_date": nxt.isoformat(), "kind": "long_call", "symbol": p.symbol, "ref_price": price,
                                 "legs": legs, "model_credit": legs["cost"], "rule": o["label"]})
    if not len(today):
        lines.append("  no qualifying signals today")
    return lines, out


def short_ideas_section(px, nxt, upcoming, ctx) -> tuple[list[str], list[str]]:
    """The tested short-sale rules' signals today, with their honest record."""
    if not (SHORTS_DIR / "summary.json").exists():
        return [], []
    from .short_research import rally_entries
    sm = json.loads((SHORTS_DIR / "summary.json").read_text())
    r = sm["rally"]
    ent = rally_entries(px, r["rsi_min"], r["bottom"], None)
    idx = px["close"].index
    spy = px["close"]["SPY"]
    today = ent[ent.entry > idx[-1]] if len(ent) else ent
    names = []
    lines = ["", "SHORT IDEAS (the other side) - weak stocks (below their 200-day average, among the "
             f"{r['bottom']} weakest) that just bounced hard",
             f"  record: won {r['test']['win_rate']:.0%} of {r['test']['trades']} short sales in 2021-2026 but averaged "
             f"{pct(r['test']['avg'], 2)} a trade (2011-2021: {r['design']['win_rate']:.0%}, {pct(r['design']['avg'], 2)}): "
             "shorts mostly win small and lose big in a rising market. Puts and bear call spreads on these signals "
             "also lost money in 2021-2026, so none are suggested."]
    if r.get("spy_down") and not spy.iloc[-1] < spy.iloc[-200:].mean():
        lines.append("  The rule only shorts while the S&P 500 is below its 200-day average - it's above it now, so no shorts today.")
        return lines, names
    ex = r["exit"]
    for s in sorted(set(today.symbol)) if len(today) else []:
        if r.get("no_earnings") and earnings_within(s, nxt, ex["hold"], upcoming):
            continue
        p = float(px["close"][s].iloc[-1])
        lines.append(f"  SHORT {s:5s} ~${p:,.2f}: buy-to-cover limit at ~${p * (1 - ex['target']):,.2f} (-{ex['target']:.0%}), "
                     f"buy-stop at ~${p * (1 + ex['stop']):,.2f} (+{ex['stop']:.0%}), else cover after {ex['hold']} sessions")
        names.append(s)
    if not names:
        lines.append("  none today")
    return lines, names


def actions_section(eps_plays, top, dip_sells, eps_sells, eps_holds, dip_holds, short_names, options, nxt) -> list[str]:
    """One clear list of what to do at the open."""
    tag = {p["symbol"]: p.get("tag") for p in eps_plays}
    buys = [f"{p['symbol']} [{tag[p['symbol']]}] {p['shares']:,} sh (EPS pick)" for p in eps_plays]
    buys += [f"{s} (top 5)" for s in top["buys"]]
    lines = ["", f"WHAT TO DO AT THE OPEN ({nxt:%a %b %d}) - details in each section below"]
    lines.append("  BUY:        " + (", ".join(buys) or "nothing"))
    lines.append("  SHORT:      " + (", ".join([f"{s} (top 5)" for s in top["shorts"]] + [f"{s} (short idea)" for s in short_names])
                                     or "nothing"))
    sells = [f"{s} (top 5)" for s in top["sells"]] + [f"{s} (EPS pick)" for s in eps_sells] + [f"{s} (dip-buy)" for s in dip_sells]
    lines.append("  SELL:       " + (", ".join(sells) + " - at the close if the limit order hasn't filled" if sells else "nothing"))
    if top["covers"]:
        lines.append("  COVER:      " + ", ".join(top["covers"]) + " - at the close if the limit order hasn't filled")
    holds = top["holds"] + eps_holds + dip_holds
    lines.append("  HOLD:       " + (", ".join(holds) + " (if you own them; keep their limit and stop orders)" if holds else "nothing"))
    if top["short_holds"]:
        lines.append("  STAY SHORT: " + ", ".join(top["short_holds"]) + " (keep their orders)")
    lines.append("  OPTIONS:    " + (", ".join(options) or "none") + "; QQQ 0DTE condor only if the 9:40 AM check says so")
    lines.append("  Legend: [B] beat its EPS estimate, [M] missed, [=] in line.")
    return lines


def spread_lines(sym, price, vol, pick, nxt, mult, risk_dollars, upcoming=(), journal_rows=None, kind="",
                 fair_mult: float | None = None) -> list[str]:
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
        floor = None
        if fair_mult:                       # the credit at "fair" pricing, where the backtest still made money
            fs, fl = (bs_price(price, k, t, implied_vol(vol * fair_mult, price, k, t), 0.04, False) for k in (ks, kl))
            floor = round(max((fs - opt_half_spread(fs)) - (fl + opt_half_spread(fl)), 0.01), 2)
            tp += f". Skip it if the credit at 9:35 is under ${floor:.2f}"
        if journal_rows is not None and n > 0:
            journal_rows.append({"plan_date": nxt.isoformat(), "kind": kind, "symbol": sym, "ref_price": price,
                                 "legs": {"expiry": exp.isoformat(), "short_put": ks, "long_put": kl, "contracts": n,
                                          "min_credit": floor},
                                 "model_credit": credit, "max_loss": max_loss, "rule": pick["label"]})
        return [f"  {sym}: SELL {exp:%b %d} ${ks:g} put / BUY ${kl:g} put (bull put spread) at 9:35 AM ET, "
                f"est. credit ${credit:.2f}/share, max loss {money(max_loss)} per spread -> {n} spreads "
                f"(max loss {money(n * max_loss)}, credit ~{money(n * credit * 100)}){tp}. Wins if {sym} stays above ${ks:g}.{warn}"]
    k = strike(price * (1 + cfg.otm))
    mid = bs_price(price, k, t, implied_vol(iv, price, k, t), 0.04, True)
    cost = (mid + opt_half_spread(mid)) * MULTIPLIER + COMMISSION
    n = int(risk_dollars // cost)
    return [f"  {sym}: BUY {exp:%b %d} ${k:g} call at 9:35 AM ET, est. ${cost / 100:.2f}/share -> {n} contracts "
            f"({money(n * cost)}){warn}"]


def options_section(px, plays, nxt, summary, moves, upcoming=(), journal_rows=None) -> list[str]:
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
                              upcoming, journal_rows, f"{p['signal']}_put_spread", mults["fair"][pick["config"]["signal"]])
        shown = True
    for sig in sorted({p["signal"] for p in plays}):
        pick = picks.get(sig)
        if pick:
            lines.append(f"  rule for {sig} picks: {pick['label']} - won {pick['design_mid']['win_rate']:.0%} of "
                         f"{pick['design_mid']['trades']} trades 2011-2021 and {pick['test_mid']['win_rate']:.0%} of "
                         f"{pick['test_mid']['trades']} in 2021-2026 (fair pricing: {pick['test_fair']['win_rate']:.0%}); "
                         f"avg {pct(pick['test_mid']['avg'])} of the max loss per trade, worst {pct(pick['test_mid']['worst_trade'], 0)}")
    if not shown:
        lines.insert(2, "  No new stock picks today, so no new options plays.")
    return lines


def qqq_section(px, vix_frame, moves, nxt, summary, journal_rows=None) -> list[str]:
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
            lines += spread_lines("QQQ", price, vol, pick, nxt, m, ACCOUNT * QQQ_OPT_RISK, (), journal_rows, "qqq_put_spread",
                                  mults["fair"][cfg.signal])
        else:
            lines.append("     no signal today")
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


def calendar_section(calendar, nxt, acc=None, past=(), days: int = 3, show: int = 30) -> list[str]:
    lines = ["", f"EARNINGS COMING UP (next {days} days)"]
    bt = (acc or {}).get("beats")
    record = beat_record(list(past)) if bt else {}
    end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    rows = [r for r in calendar if r["symbol"] in STOCKS and not np.isfinite(r["actual"]) and r["date"]
            and dt.date.today().isoformat() <= r["date"] <= end]
    if len(rows) > show:
        lines.append(f"  {len(rows)} reports; the first {show}:")
    for r in rows[:show]:
        call = ""
        hist = record.get(r["symbol"], [])[-bt["n"]:] if bt else []
        if bt and len(hist) == bt["n"]:
            k = sum(hist)
            call = (f" | beat {k} of last {bt['n']} -> likely BEAT (right {bt['test_accuracy']:.0%} in 2021-2026)"
                    if k / bt["n"] >= bt["min_rate"] else f" | beat {k} of last {bt['n']}")
        est = f"{r['estimate']:.2f}" if np.isfinite(r["estimate"]) else "n/a"
        lines.append(f"  {r['symbol']:5s} {r['date']} {r['timing']:3s} EPS estimate {est}{call}")
    if not rows:
        lines.append(f"  none in the next {days} days")
    elif bt:
        lines.append(f"  (All reports beat their estimate {bt['test_base_rate']:.0%} of the time in 2021-2026.)")
    return lines


def learning_section(learned: dict, size: dict, acc: dict, journal_df) -> list[str]:
    from .journal import record_lines
    from .learning import lines_for
    lines = ["", "TRACK RECORD AND LEARNING FROM MISTAKES"]
    lines += record_lines(acc, journal_df)
    for kind, name in (("dip", "dip-buy"), ("eps", "EPS")):
        lines += lines_for(name, (learned or {}).get(kind), [])
        if size.get(kind, (1.0, ""))[1]:
            lines.append(f"  {name}: {size[kind][1]}")
    if not any((learned or {}).get(k, {}).get("rules") for k in ("dip", "eps")):
        lines.append("  No losing-trade condition has been strong and steady enough to learn yet (needs a 7+ point lower "
                     "win rate in both the older and newer half of the history).")
    return lines


def holdings_from(path) -> dict | None:
    """{symbol: shares} from a holdings file: one 'SYMBOL SHARES' per line (or JSON)."""
    if not path:
        return None
    text = Path(path).read_text().strip()
    if text.startswith("{"):
        return {k.upper(): float(v) for k, v in json.loads(text).items()}
    import re
    from .universe import stocks
    known = set(stocks(with_data=False)) | set(POOLS["etfs"]) | {"SPY", "QQQ"}
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z.]{0,5})\s*[:=\s-]\s*([\d,]*\.?\d+)", line)
        if m and m.group(1).upper() in known:
            out[m.group(1).upper()] = float(m.group(2).replace(",", ""))
    return out


def live_learning(px, acc, vix, asia, past_calendar) -> dict:
    """Re-learn the losing-trade conditions on all data to date (saved earnings days plus
    the latest reports)."""
    from .learning import EVENTS, learned
    if not EVENTS.exists():
        return {}
    ev = pd.read_csv(EVENTS, parse_dates=["day"])
    idx = px["close"].index
    extra = []
    for r in past_calendar:
        if r["symbol"] in px["close"] and r["date"]:
            d = reaction_day(idx, r["date"], r["timing"])
            if d is not None:
                extra.append({"symbol": r["symbol"], "day": d, "known": True, "surprise": surprise(r["actual"], r["estimate"])})
    if extra:
        ev = pd.concat([ev, pd.DataFrame(extra)]).drop_duplicates(["symbol", "day"]).sort_values(["day", "symbol"])
    ev = ev[ev.symbol.isin(px["close"].columns)].reset_index(drop=True)
    return learned(px, acc, ev, vix, asia)


# --------------------------------------------------------------------- main
def build(ext_paths, past_json, next_json, now: pd.Timestamp | None = None, holdings_path=None,
          learn: bool = True) -> tuple[str, str, list[dict]]:
    """(subject, text, journal rows for today's new recommendations)."""
    from . import journal
    from .learning import size_factor
    summary = json.loads(SUMMARY.read_text())
    acc = json.loads(ACCURACY.read_text())
    syms = list(dict.fromkeys(list(POOLS[DIP.pool]) + ["SPY", "QQQ"] + list(SECTORS)
                              + [s for s in list(ASIA) + list(ASIA_ADRS) if (DATA_DIR / f"{s}.csv").exists()]))
    px = load_prices([s for s in syms if (DATA_DIR / f"{s}.csv").exists()])
    now = now or pd.Timestamp.now(tz=ET)
    ext = load_extended(ext_paths)
    px = complete_daily(px, ext, now)
    cov = px["close"][[s for s in STOCKS if s in px["close"]]].notna().mean(axis=1)
    px = {k: v.loc[:cov[cov >= 0.9].index[-1]] for k, v in px.items()}       # drop a half-loaded day
    vix_frame = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")[["open", "close"]]
    vix = vix_frame["close"]
    last = px["close"].index[-1]
    nxt = next_trading_day(last.date())
    if pd.Timestamp(f"{nxt} 09:35", tz=ET) < now:
        nxt = next_trading_day(now.date() - dt.timedelta(days=1)) if now.hour < 9 else next_trading_day(now.date())
    moves = overnight_moves(ext)
    past = load_calendar(past_json) if past_json else []
    upcoming = load_calendar(next_json) if next_json else []
    jdf = journal.evaluate(px)
    record = journal.track_record(jdf)
    size = {k: size_factor(record.get(k, {}), acc[k]["test"]["win_rate"]) for k in ("dip", "eps")}
    learned = {}
    if learn:
        try:
            learned = live_learning(px, acc, vix, asia_overnight(px)["asia"], past)
        except Exception as exc:                       # learning must never stop the email
            print(f"learning skipped: {exc!r}")

    head = [f"DAILY TRADING PLAN for {nxt:%a %b %d, %Y} (orders at 9:35 AM ET unless noted)",
            f"Data: daily closes through {last:%a %b %d}; after-hours/pre-market as of "
            + (max(m["as_of"] for m in moves.values()).strftime("%a %I:%M %p ET") if moves else "n/a")
            + ("" if is_trading_day(now.date()) else f". Markets are closed today ({now:%A}); this plan is for {nxt:%A}."),
            "Stocks covered: every S&P 500, Nasdaq-100 and Dow 30 company.",
            "Sizing: half the account in the top-5 list (10 slots of $75,000), half in EPS picks (5 slots of $150,000); "
            "each options play risks at most 1% of the account; 0DTE QQQ condors at most 2%.",
            "Every stock gets one call: nothing is both bought and shorted, and HOLD/SELL lines only cover trades an "
            "earlier email told you to make."]
    t5 = json.loads((TOP5_DIR / "summary.json").read_text()) if (TOP5_DIR / "summary.json").exists() else {}
    cb = t5.get("combined", {}).get("test")
    if cb:
        head.append(f"Whole plan, backtested on 2021-2026 (rules chosen on 2011-2021): {money(ACCOUNT)} -> {money(cb['final'])} "
                    f"({pct(cb['cagr'])}/yr vs the S&P's {pct(cb['spy_cagr'])}), worst drop {pct(cb['max_drawdown'], 0)}, "
                    f"{cb['win_rate']:.0%} of {cb['trades']:,} trades won.")
    over, asia_live = overnight_section(px, moves, summary, acc)
    events = learning_events(px, past)
    ctx = {"vix": vix, "asia": asia_live, "learned": learned, "holdings": holdings_from(holdings_path), "size": size,
           "past_calendar": past, "events": events, "acc": acc}
    if ctx["holdings"] is not None:
        head.append(f"Your holdings: from the holdings email ({len(ctx['holdings'])} positions).")
    mkt = market_section(px, vix, asia_live, nxt, summary, acc)
    eps_lines, eps_plays, eps_sells, eps_holds = eps_section(px, past, moves, nxt, acc, ACCOUNT * EPS_SHARE, upcoming, ctx)
    dip_lines, _, dip_sells, dip_holds = dip_section(px, moves, nxt, acc, ACCOUNT * DIP_SHARE, upcoming, ctx, new_buys=False)
    rows = []
    taken = {p["symbol"] for p in eps_plays} | set(eps_holds) | set(dip_holds) | set(eps_sells) | set(dip_sells)
    top_lines, top = top5_section(px, moves, nxt, ACCOUNT * TOP5_SHARE, upcoming, ctx, rows, exclude=taken)
    taken |= {b.split()[0] for b in top["buys"]} | set(top["holds"]) | set(top["sells"])
    short_lines, short_names = short_ideas_section(px, nxt, upcoming, ctx)
    short_names = [x for x in short_names if x not in taken and x not in top["shorts"]]  # never short what's bought or held
    short_lines = [l for l in short_lines if not (l.startswith("  SHORT ") and l.split()[1] in taken)]
    opt = options_section(px, eps_plays, nxt, summary, moves, upcoming, rows)
    qqq = qqq_section(px, vix_frame, moves, nxt, summary, rows)
    learn_lines = learning_section(learned, size, acc, jdf)
    cal = calendar_section(upcoming, nxt, acc, past)
    spreads = [f"{r['symbol']} put spread" for r in rows if r["kind"].endswith("put_spread")]
    actions = actions_section(eps_plays, top, dip_sells, eps_sells, eps_holds, dip_holds, short_names,
                              list(dict.fromkeys(spreads + top["options"])), nxt)
    text = "\n".join(head + actions + top_lines + eps_lines + dip_lines + short_lines + opt + qqq + mkt + over
                     + learn_lines + cal)
    for p in eps_plays:
        rows.append({"plan_date": nxt.isoformat(), "kind": p["signal"], "symbol": p["symbol"], "ref_price": p["price"],
                     "target": p["target"], "stop": p["stop"], "hold": p["hold"], "rule": p["rule"]})
    buys = [p["symbol"] for p in eps_plays] + [b.split()[0] for b in top["buys"]]
    shorts = top["shorts"] + short_names
    sells = eps_sells + dip_sells + top["sells"] + top["covers"]
    parts = ([f"BUY {', '.join(buys)}"] if buys else []) + ([f"SHORT {', '.join(shorts)}"] if shorts else []) \
        + ([f"SELL/COVER {', '.join(sells)}"] if sells else [])
    subject = f"Daily plan {nxt:%a %b %d}: " + (" | ".join(parts) or "no new stock trades")
    return subject, text, rows


def learning_events(px, past_calendar) -> pd.DataFrame:
    """Saved earnings reaction days plus the latest reports."""
    from .learning import EVENTS
    ev = pd.read_csv(EVENTS, parse_dates=["day"]) if EVENTS.exists() else pd.DataFrame(columns=["symbol", "day", "known", "surprise"])
    idx = px["close"].index
    extra = []
    for r in past_calendar:
        if r["symbol"] in px["close"] and r["date"]:
            d = reaction_day(idx, r["date"], r["timing"])
            if d is not None:
                extra.append({"symbol": r["symbol"], "day": d, "known": True, "surprise": surprise(r["actual"], r["estimate"])})
    if extra:
        ev = pd.concat([ev, pd.DataFrame(extra)]).drop_duplicates(["symbol", "day"], keep="last").sort_values(["day", "symbol"])
    return ev[ev.symbol.isin(px["close"].columns)].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extended", nargs="*", default=[], help="saved 24-hour 30-minute bars")
    ap.add_argument("--earnings-json", nargs=2, metavar=("PAST", "NEXT"), default=(None, None))
    ap.add_argument("--holdings", default=None, help="file with the shares actually held: one 'SYMBOL SHARES' per line")
    ap.add_argument("--journal", action="store_true", help="add today's recommendations to results/journal")
    ap.add_argument("--no-learning", action="store_true")
    ap.add_argument("--text-out", default=None)
    args = ap.parse_args()
    subject, text, rows = build(args.extended, *args.earnings_json, holdings_path=args.holdings, learn=not args.no_learning)
    if args.journal:
        from .journal import record
        print(f"journal: {record(rows)} new recommendation(s)")
    if args.text_out:
        Path(args.text_out).write_text(f"Subject: {subject}\n\n{text}")
    print(f"Subject: {subject}\n\n{text}")


if __name__ == "__main__":
    main()
