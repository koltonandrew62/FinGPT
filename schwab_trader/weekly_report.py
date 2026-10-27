"""Weekly picks: ETF rotation holdings, the 0-100 stock scorecard and the earnings
play book, sized for every account in ACCOUNTS (see sizing.py), sent by email.

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

from .calendar_utils import first_trading_day_of_week, next_trading_day, order_time
from .config import BENCHMARK, DEFAULT_CONFIG, SECTOR_NAMES, STATE_DIR
from .data import COLUMNS, drop_unfinished_today, fetch_schwab, fetch_yfinance, load_prices
from .earnings import BULLISH, analyze, beat_probability, recent_filings, surprise_history
from .earnings_backtest import EarningsConfig
from .notify import email_configured, send_email
from .scorecard import components, weekly_table
from .sizing import Account, accounts_from_env, money, share_plan
from .strategy import compute_signal, split_factor

REPORT_DIR = STATE_DIR / "reports"
# Worst peak-to-trough drops seen in the backtests (results/RESULTS.md, results/scorecard).
WORST_DROP = {"etf": 0.41, "scorecard": 0.40}


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


def held_since(closes: pd.DataFrame, cfg=DEFAULT_CONFIG, max_weeks: int = 52) -> dict:
    """Last week's holdings and the date each was bought, rebuilt from prices alone
    (the signal is deterministic), so scheduled runs need no saved state."""
    fridays = closes.groupby(closes.index.to_period("W-FRI")).apply(lambda d: d.index[-1])
    fridays = list(fridays.iloc[:-1])[-max_weeks:]          # weeks before the current signal
    since: dict = {}
    for i, friday in enumerate(reversed(fridays)):
        picks = set(compute_signal(closes.loc[:friday], cfg).weights)
        if i == 0:
            since = {s: friday for s in picks}
        else:
            for s in list(since):
                if s in picks:
                    since[s] = friday
            if not picks & set(since):
                break
    return {s: first_trading_day_of_week(f.date() + dt.timedelta(days=1)).isoformat() for s, f in since.items()}


def strategy_section(closes: pd.DataFrame, accounts: list[Account], previous: dict) -> dict:
    """`previous` maps symbols held last week to the date they were bought."""
    cfg = DEFAULT_CONFIG
    sig = compute_signal(closes, cfg)
    last = closes.iloc[-1]
    px = {s: float(last[s]) * split_factor(s, closes.index[-1]) for s in sig.weights}
    picks = list(sig.weights)
    when = trade_dates(closes.index[-1].date())
    since = {s: previous.get(s, when["buy"].isoformat()) for s in picks}
    ranking = []
    for sym in sig.scores.index:
        status = "HOLD" if sym in picks and sym in previous else "BUY" if sym in picks else (
            "CASH (trend down)" if sym in sig.risk_off else "")
        ranking.append({"symbol": sym, "name": SECTOR_NAMES.get(sym, sym), "score": float(sig.scores[sym]),
                        "momentum": float(sig.momentum[sym]),
                        "week": float(closes[sym].iloc[-1] / closes[sym].iloc[-6] - 1), "status": status})
    return {"asof": closes.index[-1].date().isoformat(), "config": cfg.label(), "picks": picks,
            "plans": {a.name: share_plan(sig.weights, px, a) for a in accounts}, "prices": px,
            "sells": [s for s in previous if s not in picks], "cash_slots": sig.risk_off, "ranking": ranking,
            "since": since, "buy_at": order_time(when["buy"]), "buy_at_date": when["buy"].isoformat(),
            "review_at": order_time(when["review"])}


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


def _plan_line(plans: dict, sym: str) -> str:
    return "  ".join(f"{name}: {plan[sym][0]:,} sh ({money(plan[sym][1])})" for name, plan in plans.items())


def render(strategy: dict, earnings: dict | None, accounts: list[Account],
           scorecard: dict | None = None) -> tuple[str, str, str]:
    """Return (subject, plain text, HTML)."""
    picks = strategy["picks"]
    subject = f"Weekly picks {strategy['asof']}: " + (", ".join(picks) if picks else "all cash")
    lines = [f"WEEKLY PICKS - signals as of the {strategy['asof']} close (trade at Monday's open)",
             f"ETF strategy: {strategy['config']}", "ACCOUNTS: " + "; ".join(
                 f"{a.name} {money(a.value)} ({a.strategy_share:.0%} = {money(a.strategy_dollars)} in the strategies, "
                 f"options cap {a.options_risk:.2%} = {money(a.options_dollars)}/trade, "
                 f"{'margin: can short' if a.margin else 'cash account: no shorting'})" for a in accounts), ""]
    lines.append("ETF ROTATION - HOLD THESE:" if picks else "ETF ROTATION - HOLD: cash")
    for sym in picks:
        new = strategy["since"][sym] == strategy["buy_at_date"]
        buy = f"BUY {strategy['buy_at']}" if new else f"held since {strategy['since'][sym]}"
        lines.append(f"  {sym:5s} {SECTOR_NAMES.get(sym, sym):22s} ~${strategy['prices'][sym]:,.2f} | {buy} | "
                     f"SELL {strategy['review_at']} only if it drops out")
        lines.append(f"        how much: {_plan_line(strategy['plans'], sym)}")
    for sym in strategy["sells"]:
        lines.append(f"  SELL {sym} {strategy['buy_at']} (all shares)")
    lines.append("  Worst drop in the backtests: -41% -> " + ", ".join(
        f"{a.name} -{money(a.strategy_dollars * WORST_DROP['etf'])}" for a in accounts))
    lines += ["", "  ranking (score / momentum / last week):"]
    for r in strategy["ranking"][:8]:
        lines.append(f"  {r['symbol']:5s} {r['score']:+.3f}  {_pct(r['momentum'])}  {_pct(r['week'])}  {r['status']}")

    if scorecard:
        t = scorecard["table"]
        lines += ["", "STOCK SCORECARD (0-100; any component under 25 = REJECT):"]
        for sym, r in t.head(10).iterrows():
            why = f"  failed: {r.failed}" if r.failed else ""
            lines.append(f"  {sym:5s} {r.score:5.1f}  {r.verdict:9s}{why}")
        if scorecard["buys"]:
            reports = {r["symbol"]: r["date"] for r in (earnings or {}).get("upcoming", [])}
            lines.append(f"  BUY {scorecard['buy_at']}, review {scorecard['review_at']} (sell if it drops out):")
            for sym in scorecard["buys"]:
                warn = f"  ! reports earnings {reports[sym]}: expect a big move either way" if sym in reports else ""
                lines.append(f"    {sym:5s} ~${scorecard['prices'][sym]:,.2f}  {_plan_line(scorecard['plans'], sym)}{warn}")
            lines.append("  Worst drop in the backtest: -40% -> " + ", ".join(
                f"{a.name} -{money(a.strategy_dollars * WORST_DROP['scorecard'])}" for a in accounts))
        rejected = t[t.verdict == "REJECT"]
        if len(rejected):
            lines.append("  Rejected: " + ", ".join(f"{s} ({r.failed})" for s, r in rejected.head(8).iterrows()))
        lines.append("  PUT/AVOID list (weakest, trend and momentum both failing): " +
                     (", ".join(scorecard["puts"][:8]) or "none"))
        lines.append("  Backtest: these lagged other stocks in 60% of weeks, but buying 1-month puts on them lost "
                     "~4% per trade at typical option prices and shorting them made ~0%. Use it as a don't-own list.")

    if earnings:
        lines += ["", "EARNINGS PLAY BOOK - new 10-Q/10-K filings, cash-flow-led score (-1..+1):"]
        if earnings.get("source"):
            lines.append(f"  ({earnings['source']})")
        for c in earnings["scored_filings"] or [{"symbol": "(none this week)"}]:
            if "score" in c:
                lines.append(f"  {c['symbol']:5s} {c['score']:+.2f} {c['outcome']:8s} FCF growth {_pct(c['fcf_growth'], 0)}, "
                             f"FCF margin chg {_pct(c['fcf_margin_change'])}, OCF/NI {c['cash_conversion'] or float('nan'):.2f}")
            else:
                lines.append(f"  {c['symbol']}")
        for c in earnings["buy_candidates"]:
            lines.append(f"  -> BUY {c['symbol']} {c['buy_at']} | SELL {c['sell_at']} (fixed hold; not yet backtested)")
        lines += ["", "BEATS & MISSES (last 7 days):"]
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
                lines.append(f"  {r['symbol']:5s} {r['date']}  P(beat) {r['p_beat']:.0%}  cash-flow score "
                             f"{r.get('score', float('nan')):+.2f} {r.get('outcome', '')}")
            elif "estimate" in r:
                lines.append(f"  {r['symbol']:5s} {r['date']}  EPS estimate {r['estimate']:.2f}")
            else:
                lines.append(f"  {r['symbol']}")
        if earnings["errors"]:
            lines += ["", f"({len(earnings['errors'])} data lookups failed; first: {earnings['errors'][0]})"]
    lines += ["", "Not financial advice. Backtested results do not guarantee future returns."]
    text = "\n".join(lines)
    body = html.escape(text)
    page = (f"<html><body style=\"font-family:Menlo,Consolas,monospace;font-size:13px;color:#111;background:#fff\">"
            f"<pre style=\"white-space:pre-wrap\">{body}</pre></body></html>")
    return subject, text, page


def main():
    from .earnings_backtest import EARNINGS_UNIVERSE

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["yfinance", "schwab", "local"], default="yfinance")
    ap.add_argument("--accounts", default=None, help="override ACCOUNTS, e.g. 'You:1500:1:0.02:cash'")
    ap.add_argument("--watchlist", default=",".join(EARNINGS_UNIVERSE),
                    help="comma-separated tickers for the scorecard and earnings sections")
    ap.add_argument("--skip-earnings", action="store_true")
    ap.add_argument("--earnings-json", nargs=2, metavar=("PAST", "NEXT"),
                    help="saved Robinhood earnings-calendar results (days=-7, days=7) instead of Yahoo/SEC")
    ap.add_argument("--skip-scorecard", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--text-out", default=None, help="also write the plain-text email to this file")
    args = ap.parse_args()
    accounts = accounts_from_env(args.accounts)
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]

    prices = recent_prices(args.source, list(DEFAULT_CONFIG.symbols) + [BENCHMARK])
    previous = held_since(prices["close"])
    strat = strategy_section(prices["close"], accounts, previous)
    card = None
    if not args.skip_scorecard:
        stock_prices = recent_prices(args.source, watchlist)
        card = scorecard_section(stock_prices, [s for s in watchlist if s in stock_prices["close"]], accounts)
    if args.earnings_json:
        earn = earnings_from_calendar(*args.earnings_json, watchlist)
    else:
        earn = None if args.skip_earnings else earnings_section(watchlist)
    subject, text, page = render(strat, earn, accounts, card)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"weekly_{strat['asof']}.html"
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
