"""Weekly picks email: the trend-checked momentum strategy (active_strategy.py) sized
for every account in ACCOUNTS (see sizing.py), the 0DTE condor plan for accounts that
trade options, and earnings beats/misses.

    python -m schwab_trader.weekly_report                  # yfinance data, email
    python -m schwab_trader.weekly_report --source schwab  # use your Schwab API data
    python -m schwab_trader.weekly_report --no-email       # just print / save HTML

Run it after Friday's close (see README for cron / Task Scheduler); the trades it
lists are meant for Monday's open, which is exactly what the backtests assume.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .active_strategy import LABEL, POOL, market_trend, momentum, target_weights
from .calendar_utils import first_trading_day_of_week, next_trading_day, order_time
from .config import BENCHMARK, DATA_DIR, STATE_DIR
from .data import COLUMNS, drop_unfinished_today, fetch_schwab, fetch_yfinance, load_prices
from .earnings import BULLISH, analyze, beat_probability, recent_filings, surprise_history
from .earnings_backtest import EarningsConfig
from .notify import email_configured, send_email
from .scorecard import components, weekly_table
from .sizing import Account, accounts_from_env, money, share_plan

REPORT_DIR = STATE_DIR / "reports"


def recent_prices(source: str, symbols) -> dict:
    if source == "local":
        return load_prices(symbols)
    start = (dt.date.today() - dt.timedelta(days=600)).isoformat()
    if source == "schwab":
        from .live_trader import make_client
        frames = fetch_schwab(make_client(), symbols, start)
    else:
        frames = fetch_yfinance(symbols, start)
    frames = {s: drop_unfinished_today(df) for s, df in frames.items()}
    return {col: pd.DataFrame({s: f[col] for s, f in frames.items()}).sort_index() for col in COLUMNS}


def trade_dates(asof: dt.date, hold_weeks: int | None = None, now: pd.Timestamp | None = None) -> dict:
    """Buy at the first session of next week (what the backtest does); sell either at
    the next weekly check (rotation) or after `hold_weeks` (earnings drift). If that
    buy time has already passed (a mid-week run), buy at the next session instead."""
    buy = first_trading_day_of_week(asof + dt.timedelta(days=1))
    now = now or pd.Timestamp.now(tz="America/New_York")
    if pd.Timestamp(f"{buy} 09:35", tz="America/New_York") < now:
        buy = next_trading_day(now.date() - dt.timedelta(days=1) if now.hour < 9 else now.date())
    review = first_trading_day_of_week(buy + dt.timedelta(days=1))
    sell = first_trading_day_of_week(buy + dt.timedelta(weeks=hold_weeks)) if hold_weeks else None
    return {"buy": buy, "review": review, "sell": sell}


def held_since(closes: pd.DataFrame, picker, max_weeks: int = 52) -> dict:
    """Holdings going into the current week and the date each was bought, rebuilt from
    prices alone (the strategy is deterministic), so scheduled runs need no saved state.
    `picker(closes_up_to_a_friday)` returns that week's picks."""
    fridays = closes.groupby(closes.index.to_period("W-FRI")).apply(lambda d: d.index[-1])
    fridays = list(fridays.iloc[:-1])[-max_weeks:]          # weeks before the current signal
    since: dict = {}
    for i, friday in enumerate(reversed(fridays)):
        picks = set(picker(closes.loc[:friday]))
        if i == 0:
            since = {s: friday for s in picks}
        else:
            for s in list(since):
                if s in picks:
                    since[s] = friday
            if not picks & set(since):
                break
    return {s: first_trading_day_of_week(f.date() + dt.timedelta(days=1)).isoformat() for s, f in since.items()}


def active_section(closes: pd.DataFrame, accounts: list[Account]) -> dict:
    """The momentum strategy's picks for each account (small accounts skip shares they
    can't afford, so picks can differ), plus the market trend and the ranking."""
    coverage = closes.notna().mean(axis=1)
    closes = closes.loc[:coverage[coverage >= 0.9].index[-1]]     # ignore a half-loaded latest day
    date = closes.index[-1]
    when = trade_dates(date.date())
    spy = closes[BENCHMARK].dropna()
    sma200 = float(spy.iloc[-200:].mean())

    def price_at(frame):
        last = frame.iloc[-1]
        return lambda s: float(last[s]) if s in last and np.isfinite(last[s]) else float("nan")

    per_account = {}
    for acct in accounts:
        if acct.strategy_dollars <= 0:
            continue
        weights = target_weights(closes, acct.strategy_dollars, price_at(closes))
        prev = held_since(closes, lambda c: target_weights(c, acct.strategy_dollars, price_at(c)))
        px = {s: price_at(closes)(s) for s in weights}
        per_account[acct.name] = {"plan": share_plan(weights, px, acct), "prices": px,
                                  "since": {s: prev.get(s, when["buy"].isoformat()) for s in weights},
                                  "sells": [s for s in prev if s not in weights]}
    ranking = momentum(closes).head(10)
    return {"asof": date.date().isoformat(), "trend": market_trend(closes), "spy": float(spy.iloc[-1]), "sma200": sma200,
            "accounts": per_account, "ranking": ranking, "prices": price_at(closes), "buy_at": order_time(when["buy"]),
            "buy_at_date": when["buy"].isoformat(), "review_at": order_time(when["review"])}


def scorecard_section(prices: dict, symbols, accounts: list[Account], top_n: int = 5,
                      cash_flow: pd.Series | None = None) -> dict:
    """0-100 scores, verdicts, the top buys sized per account and the put/avoid list."""
    close, volume = prices["close"][symbols], prices["volume"][symbols]
    coverage = close.notna().mean(axis=1)
    date = coverage[coverage >= 0.9].index[-1]       # ignore a half-loaded latest day
    close, volume = close.loc[:date], volume.loc[:date]
    comp = components(close, volume)
    table = weekly_table(comp, date, cash_flow=cash_flow)
    buys = table[table.verdict == "BUY"].head(top_n)
    px = {s: float(close[s].iloc[-1]) for s in buys.index}
    weights = {s: 1.0 / top_n for s in buys.index}
    when = trade_dates(date.date())
    return {"table": table, "buys": list(buys.index), "plans": {a.name: share_plan(weights, px, a) for a in accounts},
            "prices": px, "puts": list(table[table.verdict == "PUT/SHORT"].sort_values("score").index),
            "buy_at": order_time(when["buy"]), "review_at": order_time(when["review"])}


def earnings_section(watchlist, days_back: int = 7, days_ahead: int = 7,
                     cfg: EarningsConfig = EarningsConfig()) -> dict:
    """New 10-Q/10-K filings scored by the play book, plus EPS beats/misses."""
    import yfinance as yf

    now = pd.Timestamp.now(tz="America/New_York")
    when = trade_dates(now.date() - dt.timedelta(days=1) if now.hour < 16 else now.date(), cfg.hold_weeks)
    candidates, reported, upcoming, errors = [], [], [], []
    for sym in watchlist:
        try:
            if len(recent_filings(sym, days_back)):
                a = analyze(sym)
                f = a["features"]
                candidates.append({"symbol": sym, "score": a["score"], "outcome": a["outcome"],
                                   "quarter_end": a["quarter_end"], "filed": a["filed"],
                                   "fcf_growth": f.get("fcf_growth"), "fcf_margin_change": f.get("fcf_margin_change"),
                                   "cash_conversion": f.get("cash_conversion"), "eps_surprise": f.get("eps_surprise"),
                                   "buy_at": order_time(when["buy"]), "sell_at": order_time(when["sell"])})
        except Exception as exc:
            errors.append(f"{sym} SEC: {exc}")
        try:
            dates = yf.Ticker(sym.replace(".", "-")).get_earnings_dates(limit=12)
            hist = surprise_history(dates)
            p_beat = beat_probability(hist)
            for _, r in hist[hist.date >= now - pd.Timedelta(days=days_back)].iterrows():
                reported.append({"symbol": sym, "date": r.date.date().isoformat(), "estimate": r.estimate,
                                 "actual": r.actual, "surprise_pct": r.surprise_pct, "result": r.result,
                                 "beat_rate_8q": p_beat})
            if dates is not None and len(dates):
                idx = pd.to_datetime(dates.index)
                nxt = [d for d in idx if now < d <= now + pd.Timedelta(days=days_ahead)]
                if nxt:
                    upcoming.append({"symbol": sym, "date": min(nxt).strftime("%a %b %d"), "p_beat": p_beat})
        except Exception as exc:
            errors.append(f"{sym} earnings dates: {exc}")
    for row in upcoming:   # add the cash-flow score so the outlook isn't just the beat history
        try:
            a = analyze(row["symbol"])
            row.update(score=a["score"], outcome=a["outcome"])
        except Exception:
            row.update(score=np.nan, outcome="INSUFFICIENT DATA")
    candidates.sort(key=lambda r: -(r["score"] if np.isfinite(r["score"]) else -9))
    return {"buy_candidates": [c for c in candidates if np.isfinite(c["score"]) and c["score"] >= BULLISH],
            "scored_filings": candidates, "reported": reported, "upcoming": upcoming, "errors": errors}


def earnings_from_calendar(past_file: str, future_file: str, watchlist) -> dict:
    """Beats/misses and upcoming reports from saved Robinhood get_earnings_calendar
    results (days=-7 and days=7), for when Yahoo/SEC are unreachable."""
    import json as _json

    def load(path):
        text = Path(path).read_text()
        return _json.loads(text[text.index("{"):])["data"]["results"]

    wanted = set(watchlist)
    reported, upcoming = [], []
    for r in load(past_file):
        est, act = r["eps"].get("estimate"), r["eps"].get("actual")
        if r["symbol"] in wanted and est is not None and act is not None:
            est, act = float(est), float(act)
            reported.append({"symbol": r["symbol"], "date": r["report"]["date"], "estimate": est, "actual": act,
                             "surprise_pct": (act - est) / abs(est) * 100 if est else float("nan"),
                             "result": "BEAT" if act > est else "MISS" if act < est else "INLINE"})
    for r in load(future_file):
        if r["symbol"] in wanted and r["eps"].get("actual") is None:
            est = r["eps"].get("estimate")
            upcoming.append({"symbol": r["symbol"], "date": f"{r['report']['date']} {r['report'].get('timing') or ''}".strip(),
                             "estimate": float(est) if est is not None else float("nan")})
    reported = list({(r["symbol"], r["date"]): r for r in reported}.values())     # the feed repeats some rows
    upcoming = list({(r["symbol"], r["date"]): r for r in upcoming}.values())
    reported.sort(key=lambda r: r["date"])
    upcoming.sort(key=lambda r: r["date"])
    return {"buy_candidates": [], "scored_filings": [], "reported": reported, "upcoming": upcoming, "errors": [],
            "source": "Robinhood earnings calendar (cash-flow scores need SEC access)"}


def _pct(x, digits=1):
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.{digits}f}%"


def render(active: dict, earnings: dict | None, accounts: list[Account], scorecard: dict | None = None,
           options_plan: list | None = None) -> tuple[str, str, str]:
    """Return (subject, plain text, HTML)."""
    first = next(iter(active["accounts"].values()), {"plan": {}})
    subject = f"Weekly picks {active['asof']}: " + (", ".join(first["plan"]) or "all cash")
    gap = active["spy"] / active["sma200"] - 1
    lines = [f"WEEKLY PICKS - signals as of the {active['asof']} close (trade at the next open)",
             f"MARKET TREND: {active['trend'].upper()} - S&P 500 (SPY) ${active['spy']:,.2f} is {abs(gap):.1%} "
             f"{'above' if gap >= 0 else 'below'} its 200-day average (${active['sma200']:,.2f})"
             + ("" if active["trend"] == "uptrend" else "; only stocks still rising over 12 months are bought"),
             f"Strategy: {LABEL}",
             "ACCOUNTS: " + "; ".join(f"{a.name} {money(a.value)} ({money(a.strategy_dollars)} in shares"
                                      + (f", options max loss {money(a.options_dollars)}/trade" if a.options_risk > 0
                                         else ", no options") + ")" for a in accounts), ""]
    for name, acct in active["accounts"].items():
        lines.append(f"{name.upper()} - HOLD THESE:" if acct["plan"] else f"{name.upper()} - HOLD: cash")
        reports = {r["symbol"]: r["date"] for r in (earnings or {}).get("upcoming", [])}
        for sym, (n, dollars) in acct["plan"].items():
            new = acct["since"][sym] == active["buy_at_date"]
            when = (f"BUY {active['buy_at']}" if new else
                    f"HOLD (in the strategy since {acct['since'][sym]}; not owned yet? BUY {active['buy_at']})")
            warn = f"  ! reports earnings {reports[sym]}" if sym in reports else ""
            lines.append(f"  {sym:5s} {n:>6,} sh ~${acct['prices'][sym]:,.2f} = {money(dollars):>9s} | {when} | "
                         f"SELL {active['review_at']} only if it drops out{warn}")
        for sym in acct["sells"]:
            lines.append(f"  SELL all {sym} {active['buy_at']}")
    lines.append("  Worst drop in the backtests: -38% at $1,500, -45% at $150,000 -> " + ", ".join(
        f"{a.name} about -{money(a.strategy_dollars * (0.38 if a.strategy_dollars < 10_000 else 0.45))}"
        for a in accounts if a.strategy_dollars > 0))
    lines += ["", "  momentum ranking (12-1 month return, price):"]
    for sym, mom in active["ranking"].items():
        lines.append(f"  {sym:5s} {mom * 100:+7.1f}%  ${active['prices'](sym):,.2f}")
    lines.append("  Backtest 2021-2026: $1,500 -> $6,398 (+33.6%/yr) vs S&P $2,508; beat the S&P in uptrends "
                 "(+27%/yr vs +10%) and downtrends (+58%/yr vs +14%). About half of positions close at a profit;"
                 " the gains come from the big winners.")

    if options_plan:
        lines += [""] + options_plan

    if scorecard:
        t = scorecard["table"]
        lines += ["", "STOCK SCORECARD (0-100; any component under 25 = REJECT):"]
        for sym, r in t.head(10).iterrows():
            lines.append(f"  {sym:5s} {r.score:5.1f}  {r.verdict:9s}" + (f"  failed: {r.failed}" if r.failed else ""))
        lines.append("  Avoid (weakest): " + (", ".join(scorecard["puts"][:8]) or "none"))

    if earnings:
        if earnings["scored_filings"]:
            lines += ["", "EARNINGS PLAY BOOK - new 10-Q/10-K filings, cash-flow-led score (-1..+1):"]
            for c in earnings["scored_filings"]:
                lines.append(f"  {c['symbol']:5s} {c['score']:+.2f} {c['outcome']:8s} FCF growth {_pct(c['fcf_growth'], 0)}")
        lines += ["", "EARNINGS BEATS & MISSES (last 7 days):"]
        for r in earnings["reported"] or [{"symbol": "(none)"}]:
            if "result" in r:
                rate = f"  8-qtr beat rate {r['beat_rate_8q']:.0%}" if "beat_rate_8q" in r else ""
                lines.append(f"  {r['symbol']:5s} {r['date']}  {r['result']:6s} EPS {r['actual']:.2f} vs est "
                             f"{r['estimate']:.2f} ({r['surprise_pct']:+.1f}%){rate}")
            else:
                lines.append(f"  {r['symbol']}")
        lines += ["", "REPORTING NEXT 7 DAYS:"]
        for r in earnings["upcoming"] or [{"symbol": "(none)"}]:
            if "p_beat" in r:
                lines.append(f"  {r['symbol']:5s} {r['date']}  P(beat) {r['p_beat']:.0%}")
            elif "estimate" in r:
                lines.append(f"  {r['symbol']:5s} {r['date']}  EPS estimate {r['estimate']:.2f}")
            else:
                lines.append(f"  {r['symbol']}")
    lines += ["", "Not financial advice. Backtested results do not guarantee future returns."]
    text = "\n".join(lines)
    page = (f"<html><body style=\"font-family:Menlo,Consolas,monospace;font-size:13px;color:#111;background:#fff\">"
            f"<pre style=\"white-space:pre-wrap\">{html.escape(text)}</pre></body></html>")
    return subject, text, page


def main():
    from .earnings_backtest import EARNINGS_UNIVERSE

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["yfinance", "schwab", "local"], default="yfinance")
    ap.add_argument("--accounts", default=None, help="override ACCOUNTS, e.g. 'You:1500:1:0:cash'")
    ap.add_argument("--watchlist", default=",".join(EARNINGS_UNIVERSE), help="tickers for the earnings section")
    ap.add_argument("--skip-earnings", action="store_true")
    ap.add_argument("--earnings-json", nargs=2, metavar=("PAST", "NEXT"),
                    help="saved Robinhood earnings-calendar results (days=-7, days=7) instead of Yahoo/SEC")
    ap.add_argument("--with-scorecard", action="store_true", help="also include the 0-100 stock scorecard")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--text-out", default=None, help="also write the plain-text email to this file")
    args = ap.parse_args()
    accounts = accounts_from_env(args.accounts)
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]

    prices = recent_prices(args.source, list(dict.fromkeys(list(POOL) + [BENCHMARK])))
    active = active_section(prices["close"], accounts)
    options_plan = None
    if any(a.options_risk > 0 for a in accounts):
        try:
            from .vol_alert import monday_table
            vix = (pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date") if args.source == "local"
                   else fetch_yfinance(["^VIX"], (dt.date.today() - dt.timedelta(days=900)).isoformat())["^VIX"])
            qqq = pd.DataFrame({c: prices[c]["QQQ"] for c in COLUMNS}).dropna(subset=["close"])
            spy = pd.DataFrame({c: prices[c][BENCHMARK] for c in COLUMNS}).dropna(subset=["close"])
            end = min(qqq.index[-1], spy.index[-1], vix.index[-1])
            options_plan = monday_table(qqq.loc[:end], spy.loc[:end], vix.loc[:end], accounts)
        except Exception as exc:
            options_plan = [f"0DTE QQQ PLAN unavailable this week ({exc})."]
    card = None
    if args.with_scorecard:
        card = scorecard_section(prices, [s for s in watchlist if s in prices["close"]], accounts)
    if args.earnings_json:
        earn = earnings_from_calendar(*args.earnings_json, watchlist)
    else:
        earn = None if args.skip_earnings else earnings_section(watchlist)
    subject, text, page = render(active, earn, accounts, card, options_plan)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"weekly_{active['asof']}.html"
    path.write_text(page)
    if args.text_out:
        Path(args.text_out).write_text(f"Subject: {subject}\n\n{text}")
    print(text)
    print(f"\nsaved {path}")
    if not args.no_email:
        if email_configured():
            send_email(subject, text, page)
            print("emailed", os.environ["EMAIL_TO"])
        else:
            print("email skipped: set SMTP_USER, SMTP_PASSWORD and EMAIL_TO (see notify.py)")

if __name__ == "__main__":
    main()
