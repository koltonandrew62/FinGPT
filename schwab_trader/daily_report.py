"""Dad's daily plan email (Sunday-Friday, 8:30 AM ET) for his $1.5M margin account:
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
                  "opened": "reg" in px, "close": ref, "close_date": closed.date()}
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
def market_section(px, vix, asia_live, nxt, summary) -> list[str]:
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
    an = summary.get("analog_forecaster", {})
    lines.append(f"  PATTERN MEMORY - the most similar past mornings since 2010 (trend, momentum, VIX, Asia overnight):")
    for target, label in (("spy_5d", "SPY over the next 5 days"),):
        k = an.get(target, {}).get("k", 50)
        f = analog_forecast(X, Y[target], pd.Timestamp(nxt), k)
        if not np.isfinite(f["p_up"]):
            continue
        rec = an.get(target, {})
        conf = rec.get("cut", 0.6)
        tag = ("leans UP" if f["p_up"] >= conf else "leans DOWN" if f["p_up"] <= 1 - conf else "no clear lean")
        hist = (f" (when it leans this hard it was right {rec['test_accuracy_confident']:.0%} of {rec['test_confident_days']} "
                f"days in 2021-2026; just guessing 'up' was right {rec['test_base_rate_up']:.0%})"
                if rec.get("test_confident_days") else "")
        lines.append(f"    {label}: rose on {f['p_up']:.0%} of the {k} closest days, avg {pct(f['avg'], 2)} -> {tag}{hist}")
    lines.append(f"    closest matches: {', '.join(f['dates'])}")
    same = [an[t]["test_accuracy_confident"] for t in ("spy_oc", "qqq_oc") if t in an]
    if same:
        lines.append(f"    (For today's open-to-close direction of SPY/QQQ the same method had no edge - right only "
                     f"{min(same):.0%}-{max(same):.0%} of its confident days in 2021-2026 - so it isn't used for that.)")
    return lines


def overnight_section(px, moves, summary) -> tuple[list[str], float | None]:
    lines = ["", "OVERNIGHT: ASIA, AFTER-HOURS AND PRE-MARKET (latest price vs yesterday's close)"]
    asia = [(s, ASIA[s], moves[s]) for s in ASIA if s in moves]
    composite = float(np.mean([m["total"] for _, _, m in asia])) if asia else None
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
        cont = summary.get("asia_continuation_test")
        if cont:
            lines.append(f"    after big Asian moves (1%+), the US day kept going Asia's way only {cont:.0%} of the "
                         "time (2021-2026): Asia moves the open, not the rest of the day.")
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
    return lines, composite


def eps_section(px, calendar, moves, nxt, summary, cash) -> tuple[list[str], list[dict]]:
    pick = summary["eps_pick"]
    cfg = EpsConfig(**pick["config"])
    c, o = px["close"], px["open"]
    idx = c.index
    last = idx[-1]
    sma50 = c.rolling(50).mean()
    future = [pd.Timestamp(d) for d in trading_days_after(last.date(), 30)]
    cal_idx = idx.append(pd.DatetimeIndex(future))
    slot = cash / EPS_SLOTS
    lines = ["", f"EPS PICKS - rule: {pick['label']} (buy at 9:35 AM ET, sell at 9:35 AM ET {cfg.hold} sessions later)",
             f"  backtest: won {pick['design_win_rate']:.0%} of {pick['design_trades']} trades 2011-2021, "
             f"{pick['test_win_rate']:.0%} of {pick['test_trades']} in 2021-2026 (avg {pct(pick['test_avg'])} a trade)"]
    holds, buys, sells, report_rows = [], [], [], []
    for r in calendar:
        s = r["symbol"]
        if s not in STOCKS or not np.isfinite(r["actual"]) or s not in c:
            continue
        rd = reaction_day(cal_idx, r["date"], r["timing"])
        if rd is None:
            continue
        surp = surprise(r["actual"], r["estimate"])
        beat = "BEAT" if r["actual"] > r["estimate"] else "MISS" if r["actual"] < r["estimate"] else "INLINE"
        prev_i = cal_idx.get_loc(rd) - 1
        prev_close = c[s].iloc[prev_i] if prev_i < len(idx) else np.nan
        trend_ok = (not cfg.trend) or (prev_i < len(idx) and c[s].iloc[prev_i] > sma50[s].iloc[prev_i])
        if rd <= last:
            gap = o.at[rd, s] / prev_close - 1
            held_up = c.at[rd, s] > o.at[rd, s]
        else:
            gap = moves.get(s, {}).get("total", float("nan")) if rd == pd.Timestamp(nxt) else float("nan")
            held_up = None
        entry = rd if cfg.entry == "gap_open" else (cal_idx[cal_idx.get_loc(rd) + 1])
        eps_ok = cfg.min_surprise is None or (np.isfinite(surp) and surp >= cfg.min_surprise)
        trend_ok = trend_ok and eps_ok
        qualifies = trend_ok and np.isfinite(gap) and gap >= cfg.min_gap and (cfg.entry == "gap_open" or held_up)
        exit_day = cal_idx[cal_idx.get_loc(entry) + cfg.hold]
        row = {"symbol": s, "report": f"{r['date']} {r['timing']}", "beat": beat, "surprise": surp,
               "gap": gap, "entry": entry, "exit": exit_day}
        if rd >= pd.Timestamp(nxt) - pd.Timedelta(days=4) or rd == last:
            report_rows.append(row | {"qualifies": qualifies, "pending": rd > last})
        if entry == pd.Timestamp(nxt) and (qualifies or (cfg.entry == "gap_open" and rd == pd.Timestamp(nxt) and trend_ok)):
            buys.append(row | {"conditional": cfg.entry == "gap_open"})
        elif qualifies and entry <= last < exit_day:
            (sells if exit_day == pd.Timestamp(nxt) else holds).append(row)
    if report_rows:
        lines.append("  Latest reports (59 stocks):")
        for r in sorted(report_rows, key=lambda r: r["report"]):
            g = f"{'pre-market' if r['pending'] else 'opened'} {pct(r['gap'])}"
            lines.append(f"    {r['symbol']:5s} {r['report']:13s} {r['beat']:6s} EPS surprise {pct(r['surprise'])}, {g}")
    free = EPS_SLOTS - len(holds)
    buys = sorted(buys, key=lambda b: -np.nan_to_num(b["gap"], nan=-1))[:max(free, 0)]
    plays = []
    for b in buys:
        price = moves.get(b["symbol"], {}).get("price", float(c[b["symbol"]].iloc[-1]))
        n = int(slot // price)
        cond = (f" only if it opens at least {cfg.min_gap:.0%} above yesterday's close (now {pct(b['gap'])})"
                if b["conditional"] and cfg.min_gap > -1 else "")
        lines.append(f"  BUY  {b['symbol']:5s} {n:,} sh ~${price:,.2f} = {money(n * price)} at {order_time(b['entry'].date())}{cond}"
                     f" | SELL {order_time(b['exit'].date())}")
        plays.append({"symbol": b["symbol"], "price": price, "signal": "eps", "entry": b["entry"].date()})
    for h in sells:
        lines.append(f"  SELL {h['symbol']:5s} at {order_time(h['exit'].date())} (EPS pick from {h['entry'].date()})")
    for h in holds:
        lines.append(f"  HOLD {h['symbol']:5s} (bought {h['entry'].date()}) until {order_time(h['exit'].date())}")
    if not (buys or sells or holds):
        lines.append("  No EPS trades today.")
    return lines, plays


def dip_section(px, moves, nxt, summary, cash, splits, upcoming=()) -> tuple[list[str], list[dict]]:
    syms = list(POOLS[DIP.pool])
    close = px["close"][syms]
    eq, trips = run_pullback(px, DIP, close.index[-520], close.index[-1], cash, splits)
    held = eq.attrs["held"]
    ind = indicators(close)
    entry, exit_ = signals(close, ind, DIP, px["close"].get("SPY"))
    last = close.index[-1]
    n_last = len(close.index) - 1
    stats = summary.get("dip_pick", {})
    lines = ["", "REGULAR STOCK PICKS - buy sharp 1-3 day dips in the 10 strongest stocks/ETFs (above their 200-day average, "
             "2-day RSI under 10); sell when RSI rebounds over 70 or after 20 sessions",
             f"  backtest: won {stats.get('design_win_rate', 0.70):.0%} of trades 2011-2021, {stats.get('test_win_rate', 0.66):.0%} in "
             f"2021-2026 ({pct(stats.get('test_cagr', 0.137))}/yr on its own money)"]
    plays = []
    sells = [s for s, h in held.items() if exit_.at[last, s] or (n_last + 1 - h["i"]) >= DIP.max_hold]
    keep = [s for s in held if s not in sells]
    cands = entry.loc[last]
    cands = [s for s in ind["rsi2"].loc[last, cands[cands].index.difference(keep)].sort_values().index]
    slots = DIP.max_positions - len(keep)
    slot = cash / DIP.max_positions
    for s in sells:
        lines.append(f"  SELL {s:5s} at {order_time(nxt)} (dip bought {held[s]['since'].date()})")
    for s in cands[:max(slots, 0)]:
        price = moves.get(s, {}).get("price", float(px["close"][s].iloc[-1]))
        n = int(slot // price)
        lines.append(f"  BUY  {s:5s} {n:,} sh ~${price:,.2f} = {money(n * price)} at {order_time(nxt)} "
                     f"(2-day RSI {ind['rsi2'].at[last, s]:.0f}) | SELL when the 2-day RSI closes above 70, by "
                     f"{order_time(trading_days_after(nxt, DIP.max_hold)[-1])} at the latest"
                     + earnings_between(s, nxt, nxt + dt.timedelta(days=10), upcoming))
        plays.append({"symbol": s, "price": price, "signal": "dip", "entry": nxt})
    for s in keep:
        lines.append(f"  HOLD {s:5s} (bought {held[s]['since'].date()}; 2-day RSI {ind['rsi2'].at[last, s]:.0f})")
    if not (sells or keep or cands[:max(slots, 0)]):
        lines.append("  No dip-buys today: none of the 10 strongest names has dipped hard enough.")
    return lines, plays


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
        lines.insert(1, "  No new stock picks today, so no new options plays.")
    return lines


def qqq_section(px, vix_frame, moves, nxt, summary) -> list[str]:
    from .vol_alert import monday_table
    lines = ["", "QQQ OPTIONS"]
    qqq = pd.DataFrame({k: px[k]["QQQ"] for k in ("open", "high", "low", "close", "volume")}).dropna(subset=["close"])
    spy = pd.DataFrame({k: px[k]["SPY"] for k in ("open", "high", "low", "close", "volume")}).dropna(subset=["close"])
    end = min(qqq.index[-1], spy.index[-1], vix_frame.index[-1])
    gap = moves.get("QQQ", {}).get("total")
    gaps = sorted({-0.01, -0.005, 0.0, 0.005, 0.01} | ({round(gap, 4)} if gap is not None and np.isfinite(gap) else set()))
    acct = [Account("Dad", ACCOUNT, 1.0, CONDOR_RISK, True)]
    table = [l.replace(" Tuesday-Friday: same rule each morning.", "")
             for l in monday_table(qqq.loc[:end], spy.loc[:end], vix_frame.loc[:end], acct, gaps=gaps)]
    if gap is not None and np.isfinite(gap):
        table.insert(1, f"  QQQ is {pct(gap, 2)} vs yesterday's close in pre-market -> use the {pct(round(gap, 4), 1)} row")
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


def patterns_section(px, top: int = 12) -> list[str]:
    stocks = [s for s in STOCKS if s in px["close"]]
    frames = pattern_frames(px, stocks)
    fwd = {5: forward_returns(px, stocks, 5), 20: forward_returns(px, stocks, 20)}
    stats = pattern_stats(frames, fwd)            # all history up to yesterday = the program's memory
    base = stats.loc["(every stock, every day)"]
    last = px["close"].index[-1]
    lines = ["", f"PATTERNS SPOTTED at the {last.date()} close (what each pattern did next across 59 stocks since 2010;"
             f" any stock rose over 5 days {base.win_5d:.0%} of the time):"]
    hits = []
    for name, f in frames.items():
        on = f.loc[last]
        for s in on[on].index:
            st = stats.loc[name]
            hits.append((abs(st.win_5d - base.win_5d), s, name, st))
    for _, s, name, st in sorted(hits, key=lambda x: -x[0])[:top]:
        lean = "bullish" if st.win_5d > base.win_5d + 0.02 else "bearish" if st.win_5d < base.win_5d - 0.02 else "neutral"
        lines.append(f"  {s:5s} {name}: rose over 5 days {st.win_5d:.0%} of {int(st.n_5d):,} times (avg {pct(st.avg_5d)}), "
                     f"20 days {st.win_20d:.0%} -> {lean}")
    if not hits:
        lines.append("  none")
    return lines


def calendar_section(calendar, nxt) -> list[str]:
    lines = ["", "EARNINGS COMING UP (59 stocks)"]
    rows = [r for r in calendar if r["symbol"] in STOCKS and not np.isfinite(r["actual"]) and r["date"]
            and r["date"] >= dt.date.today().isoformat()]
    for r in rows[:20]:
        lines.append(f"  {r['symbol']:5s} {r['date']} {r['timing']:3s} EPS estimate {r['estimate']:.2f}")
    if not rows:
        lines.append("  none in the next 7 days")
    return lines


# --------------------------------------------------------------------- main
def build(ext_paths, past_json, next_json, now: pd.Timestamp | None = None) -> tuple[str, str]:
    summary = json.loads(SUMMARY.read_text())
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

    head = [f"DAD'S DAILY PLAN for {nxt:%a %b %d, %Y} - $1.5M account (orders at 9:35 AM ET unless noted)",
            f"Data: daily closes through {last:%a %b %d}; after-hours/pre-market as of "
            + (max(m["as_of"] for m in moves.values()).strftime("%a %I:%M %p ET") if moves else "n/a")
            + ("" if is_trading_day(now.date()) else f". Markets are closed today ({now:%A}); this plan is for {nxt:%A}."),
            "Account split: half in EPS picks (5 slots of $150,000), half in dip-buys (3 slots of $250,000); "
            "each options play risks at most 1% ($15,000); 0DTE QQQ condors at most 2%.", ""]
    over, asia_live = overnight_section(px, moves, summary)
    mkt = market_section(px, vix, asia_live, nxt, summary)
    eps_lines, eps_plays = eps_section(px, past, moves, pd.Timestamp(nxt), summary, ACCOUNT * EPS_SHARE)
    dip_lines, dip_plays = dip_section(px, moves, nxt, summary, ACCOUNT * DIP_SHARE, splits, upcoming)
    opt = options_section(px, eps_plays + dip_plays, nxt, summary, moves, upcoming)
    qqq = qqq_section(px, vix_frame, moves, nxt, summary)
    pats = patterns_section(px)
    cal = calendar_section(upcoming, nxt)
    combo = summary.get("combined")
    tail = [""]
    if combo:
        tail.append(f"WHOLE PLAN BACKTEST 2021-2026 ($1.5M, all of the above together): {money(combo['test_final'])} "
                    f"({pct(combo['test_cagr'])}/yr) vs S&P {money(combo['spy_final'])}; worst drop {pct(combo['test_maxdd'], 0)}; "
                    f"{combo['test_win_rate']:.0%} of {combo['test_trades']:,} trades won.")
    tail.append("Not financial advice. Backtests use modelled option prices and the largest stocks of 2021; real results will differ.")
    text = "\n".join(head + mkt + over + eps_lines + dip_lines + opt + qqq + pats + cal + tail)
    buys = [p["symbol"] for p in eps_plays + dip_plays]
    subject = f"Daily plan {nxt:%a %b %d}: " + (f"BUY {', '.join(buys)}" if buys else "no new stock buys")
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
