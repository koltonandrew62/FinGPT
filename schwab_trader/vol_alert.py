"""Morning 0DTE QQQ alert: run at ~9:31 AM ET, right after the open.

Uses the detector and trading rule that zero_dte_research.py selected on 2005-2021
data (results/zero_dte/summary.json), retrained on all history up to yesterday.
If today is flagged as likely volatile it prints and emails the exact QQQ
contracts expiring today, their live bid/ask, how many to buy for each account
(see sizing.py) and when to sell. Nothing is sent on quiet days.

    python -m schwab_trader.vol_alert                 # yfinance history and chain
    python -m schwab_trader.vol_alert --source schwab # Schwab option chain
    python -m schwab_trader.vol_alert --source local  # data/QQQ.csv, SPY.csv, VIX.csv (today's
                                                      # bar must hold today's open); no chain
The model saved by `--save-model` (results/zero_dte/model.joblib) is used when
present, so only the last ~300 days of data are needed each morning.

Read results/zero_dte/RESULTS.md first: 0DTE options lost money in every
realistic backtest here. Keep the options budget small (sizing.py caps it).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_DIR, RESULTS_DIR
from .notify import email_configured, send_email
from .sizing import accounts_from_env, contracts, money
from .vol_model import BIG_MOVE_QUANTILE, features, fit, predict, target
from .zero_dte_research import legs

SELL_BY = "3:50 PM ET"   # close well before the 4:00 PM expiry; in-the-money SPY/QQQ options become shares
MODEL_PATH = RESULTS_DIR / "zero_dte" / "model.joblib"


def history_yf(days: int = 900) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    import yfinance as yf

    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for sym in ("QQQ", "SPY", "^VIX"):
        df = yf.download(sym, start=start, auto_adjust=False, progress=False, multi_level_index=False)
        df = df.rename(columns=str.lower)
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        out.append(df)
    return tuple(out)


def history_local() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    def read(sym):
        return pd.read_csv(DATA_DIR / f"{sym}.csv", parse_dates=["date"], index_col="date")
    return read("QQQ"), read("SPY"), read("VIX")


def chain_yf(expiry: str) -> pd.DataFrame:
    import yfinance as yf

    ch = yf.Ticker("QQQ").option_chain(expiry)
    calls = ch.calls.assign(call=True)
    puts = ch.puts.assign(call=False)
    return pd.concat([calls, puts])[["contractSymbol", "strike", "bid", "ask", "call"]]


def chain_schwab(expiry: dt.date) -> pd.DataFrame:
    from .live_trader import make_client

    client = make_client()
    resp = client.get_option_chain("QQQ", from_date=expiry, to_date=expiry, strike_count=40)
    resp.raise_for_status()
    rows = []
    for key, is_call in (("callExpDateMap", True), ("putExpDateMap", False)):
        for strikes in resp.json().get(key, {}).values():
            for contracts_ in strikes.values():
                for c in contracts_:
                    rows.append({"contractSymbol": c["symbol"], "strike": c["strikePrice"], "bid": c["bid"],
                                 "ask": c["ask"], "call": is_call})
    return pd.DataFrame(rows)


def todays_rule() -> dict:
    path = RESULTS_DIR / "zero_dte" / "summary.json"
    if not path.exists():
        raise SystemExit("Run `python -m schwab_trader.zero_dte_research` first.")
    return json.loads(path.read_text())


def train(qqq: pd.DataFrame, spy: pd.DataFrame, vix: pd.DataFrame, rule: dict) -> dict:
    """Fit the selected detector on all completed days and fix its flag threshold."""
    pick = rule["design"]["selected"]
    X = features(qqq, vix, spy)
    move = target(qqq)
    y = (move >= move.quantile(BIG_MOVE_QUANTILE)).astype(float).where(move.notna())
    model, cols = fit(pick["model"], X, y)
    # Threshold from walk-forward probabilities (each year scored by a model fit on earlier
    # years), as in the research: in-sample probabilities are overconfident.
    from .zero_dte_research import walk_forward
    oos = walk_forward(pick["model"], X, y, last_year=int(qqq.index[-1].year))
    threshold = float(oos.dropna().quantile(1 - pick["flag_rate"]))
    return {"model": model, "cols": cols, "threshold": threshold, "trained_through": str(qqq.index[-1].date())}


def evaluate_today(qqq: pd.DataFrame, spy: pd.DataFrame, vix: pd.DataFrame, rule: dict) -> dict:
    """Probability that today (the last row, which must hold today's open) is a big-move day."""
    pick = rule["design"]["selected"]
    if MODEL_PATH.exists():
        import joblib
        fitted = joblib.load(MODEL_PATH)
    else:
        fitted = train(qqq.iloc[:-1], spy.iloc[:-1], vix.iloc[:-1], rule)
    X = features(qqq, vix, spy)
    p_today = float(predict(fitted["model"], fitted["cols"], X.iloc[[-1]]).iloc[0])
    return {"prob": p_today, "threshold": fitted["threshold"], "flagged": p_today >= fitted["threshold"], "pick": pick}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["yfinance", "schwab", "local"], default="yfinance")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--text-out", default=None, help="write the alert text here (only when flagged)")
    ap.add_argument("--save-model", action="store_true", help="train on all local history and save the model")
    args = ap.parse_args()

    if args.save_model:
        import joblib
        qqq, spy, vix = history_local()
        fitted = train(qqq, spy, vix, todays_rule())
        joblib.dump(fitted, MODEL_PATH)
        print(f"saved {MODEL_PATH} (trained through {fitted['trained_through']}, threshold {fitted['threshold']:.3f})")
        return

    qqq, spy, vix = history_local() if args.source == "local" else history_yf()
    today = pd.Timestamp(pd.Timestamp.now(tz="America/New_York").date())
    if qqq.index[-1] != today:
        raise SystemExit("Today's QQQ open is not available yet - run after 9:30 AM ET on a trading day.")
    # Today's bar is still forming: keep only its open (the model never looks at today's close/high/low).
    for df in (qqq, spy):
        df.loc[today, ["high", "low", "close", "volume"]] = np.nan
    rule = todays_rule()
    verdict = evaluate_today(qqq, spy, vix, rule)
    pick = verdict["pick"]
    lines = [f"0DTE QQQ CHECK {today.date()} - probability of a big move {verdict['prob']:.0%} "
             f"(trade threshold {verdict['threshold']:.0%})"]
    if not verdict["flagged"]:
        lines.append("No trade today: not flagged as volatile.")
    else:
        spot = float(qqq.at[today, "open"])
        expiry = today.date()
        wanted = legs(pick["structure"], spot, spot >= float(qqq["close"].iloc[-2]))
        if args.source == "local":      # no live chain: name the contracts, estimate nothing
            chain = pd.DataFrame([{"contractSymbol": f"QQQ {expiry:%y%m%d} {'C' if c else 'P'} {k}", "strike": k,
                                   "bid": float("nan"), "ask": float("nan"), "call": c} for c, k in wanted])
        else:
            chain = chain_schwab(expiry) if args.source == "schwab" else chain_yf(expiry.isoformat())
        rows = []
        for is_call, k in wanted:
            side = chain[(chain.call == is_call)]
            best = side.iloc[(side.strike - k).abs().argsort()[:1]]
            rows.append(best.iloc[0])
        unit = sum(r.ask * 100 + 0.65 for r in rows)
        lines.append(f"VOLATILE DAY FLAGGED - structure: {pick['structure']} (QQQ opened ${spot:,.2f})")
        for r in rows:
            lines.append(f"  BUY {r.contractSymbol}  {'CALL' if r.call else 'PUT'} ${r.strike:g} exp {expiry}  "
                         f"bid ${r.bid:.2f} / ask ${r.ask:.2f}")
        if np.isfinite(unit):
            lines.append(f"  Cost per set: {money(unit)} incl. $0.65/contract fees")
            for acct in accounts_from_env():
                n = contracts(unit, acct)
                lines.append(f"  {acct.name} ({money(acct.value)}): {n} set(s) = {money(n * unit)} "
                             f"(cap {acct.options_risk:.2%} of account)" + ("" if n else " - can't afford one set"))
        else:
            for acct in accounts_from_env():
                lines.append(f"  {acct.name} ({money(acct.value)}): spend at most {money(acct.options_dollars)} "
                             f"({acct.options_risk:.2%} of the account); contracts = that / (ask x 100 + $0.65)")
        lines.append(f"  BUY now (by 9:45 AM ET) with limit orders at the ask; SELL by {SELL_BY} today, "
                     "never hold to expiry.")
    t = rule["test"].get("informed", {})
    lines.append(f"Reality check: this rule turned $1,500 into ${t.get('final_per_1500', float('nan')):,.0f} over "
                 "2021-2026 at informed pricing (see results/zero_dte/RESULTS.md). Not financial advice.")
    text = "\n".join(lines)
    print(text)
    if verdict["flagged"]:
        if args.text_out:
            Path(args.text_out).write_text(f"Subject: 0DTE QQQ alert {today.date()}\n\n{text}")
        if not args.no_email and email_configured():
            send_email(f"0DTE QQQ alert {today.date()}", text)


if __name__ == "__main__":
    main()
