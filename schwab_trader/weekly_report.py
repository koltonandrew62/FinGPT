"""Weekly picks: the strategy's holdings for the coming week plus the earnings play
book, sent to your email and as a desktop notification.

    python -m schwab_trader.weekly_report                       # yfinance data, email + notify
    python -m schwab_trader.weekly_report --source schwab       # use your Schwab API data
    python -m schwab_trader.weekly_report --no-email --no-notify  # just print / save HTML

Run it after Friday's close (see README for cron / Task Scheduler); the trades it
lists are meant for Monday's open, which is exactly what the backtest assumes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os

import numpy as np
import pandas as pd

from .calendar_utils import first_trading_day_of_week, order_time
from .config import BENCHMARK, DEFAULT_CONFIG, SECTOR_NAMES, STATE_DIR
from .data import drop_unfinished_today, fetch_schwab, fetch_yfinance, load_prices
from .earnings import BULLISH, analyze, beat_probability, recent_filings, surprise_history
from .earnings_backtest import EarningsConfig
from .notify import desktop_notify, email_configured, send_email
from .strategy import compute_signal, split_factor, whole_share_targets

REPORT_DIR = STATE_DIR / "reports"
LAST_PICKS = STATE_DIR / "last_picks.json"


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
    return {col: pd.DataFrame({s: f[col] for s, f in frames.items()}).sort_index() for col in ("open", "close")}


def trade_dates(asof: dt.date, hold_weeks: int | None = None) -> dict:
    """Buy at the first session of next week (what the backtest does); sell either at
    the next weekly check (rotation) or after `hold_weeks` (earnings drift)."""
    buy = first_trading_day_of_week(asof + dt.timedelta(days=1))
    review = first_trading_day_of_week(buy + dt.timedelta(days=1))
    sell = first_trading_day_of_week(buy + dt.timedelta(weeks=hold_weeks)) if hold_weeks else None
    return {"buy": buy, "review": review, "sell": sell}


def strategy_section(closes: pd.DataFrame, account_value: float, previous: dict) -> dict:
    """`previous` maps symbols held last week to the date they were bought."""
    cfg = DEFAULT_CONFIG
    sig = compute_signal(closes, cfg)
    last = closes.iloc[-1]
    px = {s: float(last[s]) * split_factor(s, closes.index[-1]) for s in sig.weights}
    shares = whole_share_targets(sig.weights, account_value, px)
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
            "shares": shares, "prices": px, "sells": [s for s in previous if s not in picks],
            "cash_slots": sig.risk_off, "ranking": ranking, "since": since,
            "buy_at": order_time(when["buy"]), "buy_at_date": when["buy"].isoformat(),
            "review_at": order_time(when["review"])}


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


def _pct(x, digits=1):
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.{digits}f}%"


def render(strategy: dict, earnings: dict | None, account_value: float) -> tuple[str, str, str]:
    """Return (subject, plain text, HTML)."""
    picks = strategy["picks"]
    subject = f"Weekly picks {strategy['asof']}: " + (", ".join(picks) if picks else "all cash")
    lines = [f"WEEKLY PICKS - signals as of the {strategy['asof']} close (trade at Monday's open)",
             f"Strategy: {strategy['config']}   Account value used for sizing: ${account_value:,.2f}", ""]
    lines.append("HOLD THESE:" if picks else "HOLD: cash (no fund passed the trend filter)")
    for sym in picks:
        new = strategy["since"][sym] == strategy["buy_at_date"]
        buy = f"BUY {strategy['buy_at']}" if new else f"held since {strategy['since'][sym]}"
        lines.append(f"  {sym:5s} {SECTOR_NAMES.get(sym, sym):24s} {strategy['shares'][sym]:>4d} sh @ ~${strategy['prices'][sym]:,.2f}"
                     f"  | {buy} | SELL {strategy['review_at']} only if it drops out (next week's email confirms)")
    for sym in strategy["sells"]:
        lines.append(f"  SELL {sym} {strategy['buy_at']}")
    if strategy["cash_slots"]:
        lines.append("Kept in cash (downtrend): " + ", ".join(strategy["cash_slots"]))
    lines += ["", "RANKING (score / momentum / last week):"]
    for r in strategy["ranking"]:
        lines.append(f"  {r['symbol']:5s} {r['score']:+.3f}  {_pct(r['momentum'])}  {_pct(r['week'])}  {r['status']}")

    if earnings:
        lines += ["", "EARNINGS PLAY BOOK - new 10-Q/10-K filings, cash-flow-led score (-1..+1):"]
        for c in earnings["scored_filings"] or [{"symbol": "(none this week)"}]:
            if "score" in c:
                lines.append(f"  {c['symbol']:5s} {c['score']:+.2f} {c['outcome']:8s} FCF growth {_pct(c['fcf_growth'], 0)}, "
                             f"FCF margin chg {_pct(c['fcf_margin_change'])}, OCF/NI {c['cash_conversion'] or float('nan'):.2f}")
            else:
                lines.append(f"  {c['symbol']}")
        for c in earnings["buy_candidates"]:
            lines.append(f"  -> BUY {c['symbol']} {c['buy_at']} | SELL {c['sell_at']} (fixed hold)")
        lines += ["", "BEATS & MISSES (last 7 days):"]
        for r in earnings["reported"] or [{"symbol": "(none)"}]:
            if "result" in r:
                lines.append(f"  {r['symbol']:5s} {r['result']:6s} EPS {r['actual']:.2f} vs est {r['estimate']:.2f} "
                             f"({r['surprise_pct']:+.1f}%)  8-qtr beat rate {r['beat_rate_8q']:.0%}")
            else:
                lines.append(f"  {r['symbol']}")
        lines += ["", "REPORTING NEXT 7 DAYS:"]
        for r in earnings["upcoming"] or [{"symbol": "(none)"}]:
            if "p_beat" in r:
                lines.append(f"  {r['symbol']:5s} {r['date']}  P(beat) {r['p_beat']:.0%}  cash-flow score "
                             f"{r.get('score', float('nan')):+.2f} {r.get('outcome', '')}")
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
    ap.add_argument("--account-value", type=float, default=float(os.environ.get("ACCOUNT_VALUE", 1500)))
    ap.add_argument("--watchlist", default=",".join(EARNINGS_UNIVERSE),
                    help="comma-separated tickers for the earnings section")
    ap.add_argument("--skip-earnings", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()

    prices = recent_prices(args.source, list(DEFAULT_CONFIG.symbols) + [BENCHMARK])
    previous = json.loads(LAST_PICKS.read_text()).get("since", {}) if LAST_PICKS.exists() else {}
    strat = strategy_section(prices["close"], args.account_value, previous)
    earn = None if args.skip_earnings else earnings_section([s.strip().upper() for s in args.watchlist.split(",") if s.strip()])
    subject, text, page = render(strat, earn, args.account_value)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"weekly_{strat['asof']}.html"
    path.write_text(page)
    LAST_PICKS.write_text(json.dumps({"asof": strat["asof"], "picks": strat["picks"], "since": strat["since"]}))
    print(text)
    print(f"\nsaved {path}")
    if not args.no_email:
        if email_configured():
            send_email(subject, text, page)
            print("emailed", os.environ["EMAIL_TO"])
        else:
            print("email skipped: set SMTP_USER, SMTP_PASSWORD and EMAIL_TO (see notify.py)")
    if not args.no_notify:
        buys = [s for s in strat["picks"] if s not in previous]
        msg = f"Hold {', '.join(strat['picks']) or 'cash'}" + (f" | buy {', '.join(buys)}" if buys else "") + \
              (f" | sell {', '.join(strat['sells'])}" if strat["sells"] else "")
        if earn and earn["buy_candidates"]:
            msg += " | earnings: " + ", ".join(c["symbol"] for c in earn["buy_candidates"][:3])
        desktop_notify("Weekly stock picks", msg)


if __name__ == "__main__":
    main()
