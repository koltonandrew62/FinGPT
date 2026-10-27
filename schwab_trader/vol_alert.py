"""Morning 0DTE QQQ check: run at ~9:31 AM ET, right after the open.

Uses the volatile-day detector the other way round: on days it rates among the
calmest 30% it recommends SELLING a 0DTE QQQ iron condor (short strikes 1% from the
open, wings 1% further out) for accounts allowed to trade options (options_risk > 0 in
ACCOUNTS; a margin account is required). Backtest 2021-2026 on data it never saw: won
89-93% of trades under every pricing assumption; returned +0.4% to +21%/yr depending
on how options are priced (results/condor/RESULTS.md). On other days: no trade.

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
from .condor_research import condor_strikes

CALM_SHARE, CONDOR_K, CONDOR_W = 0.30, 0.01, 0.01   # the most accurate rule on 2005-2021 data

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


def train(qqq: pd.DataFrame, spy: pd.DataFrame, vix: pd.DataFrame, kind: str = "complex") -> dict:
    """Fit the detector on all completed days and fix its calm threshold."""
    X = features(qqq, vix, spy)
    move = target(qqq)
    y = (move >= move.quantile(BIG_MOVE_QUANTILE)).astype(float).where(move.notna())
    model, cols = fit(kind, X, y)
    # Threshold from walk-forward probabilities (each year scored by a model fit on earlier
    # years), as in the research: in-sample probabilities are overconfident.
    from .zero_dte_research import walk_forward
    oos = walk_forward(kind, X, y, last_year=int(qqq.index[-1].year))
    return {"model": model, "cols": cols, "threshold": float(oos.dropna().quantile(CALM_SHARE)),
            "trained_through": str(qqq.index[-1].date())}


def load_model(qqq, spy, vix) -> dict:
    if MODEL_PATH.exists():
        import joblib
        return joblib.load(MODEL_PATH)
    return train(qqq, spy, vix)


def condor_plan(spot: float, vix_open: float, accounts) -> list[str]:
    """Strikes, estimated credit and size per account for one calm-day condor."""
    from .options_backtest import bs_price, half_spread, implied_vol
    sc, lc, sp, lp = condor_strikes(spot, CONDOR_K, CONDOR_W)
    t, atm = 1 / 252, vix_open / 100 * 1.06          # mid-pricing multiplier from the research
    mid = {k: bs_price(spot, K, t, implied_vol(atm, spot, K, t), 0.04, c)
           for k, K, c in (("sc", sc, True), ("lc", lc, True), ("sp", sp, False), ("lp", lp, False))}
    credit = sum((-1 if k[0] == "l" else 1) * (v + (half_spread(v) if k[0] == "l" else -half_spread(v)))
                 for k, v in mid.items())
    width = max(lc - sc, sp - lp)
    max_loss = (width - credit + half_spread(width)) * 100 + 6 * 0.65
    lines = [f"  SELL QQQ {sc} call / BUY {lc} call, SELL {sp} put / BUY {lp} put, all expiring today",
             f"  est. credit ${credit:.2f}/share (${credit * 100:,.0f} per set), max loss {money(max_loss)} per set; "
             "use a limit order for the whole condor at the mid price or better"]
    for acct in accounts:
        if acct.options_risk <= 0:
            lines.append(f"  {acct.name}: no options in this account")
        else:
            n = int(acct.options_dollars // max_loss) if max_loss > 0 else 0
            lines.append(f"  {acct.name}: {n} set(s), max loss {money(n * max_loss)} "
                         f"({acct.options_risk:.2%} of {money(acct.value)}), est. credit {money(n * credit * 100)}")
    lines.append(f"  Close anything still in the money by {SELL_BY}; otherwise let it expire.")
    return lines


def monday_table(qqq: pd.DataFrame, spy: pd.DataFrame, vix: pd.DataFrame, accounts,
                 gaps=(-0.02, -0.01, -0.005, 0.0, 0.005, 0.01, 0.02)) -> list[str]:
    """For the weekend email: the decision for the next session at each possible opening gap."""
    fitted = load_model(qqq, spy, vix)
    from .calendar_utils import is_trading_day, next_trading_day
    nxt = pd.Timestamp(next_trading_day(qqq.index[-1].date()))
    now = pd.Timestamp.now(tz="America/New_York")
    if pd.Timestamp(f"{nxt.date()} 09:35", tz="America/New_York") < now:     # that session already started
        today = now.date()
        nxt = pd.Timestamp(today if (is_trading_day(today) and now.hour < 9) else next_trading_day(today))
    last_close, vix_close = float(qqq["close"].iloc[-1]), float(vix["close"].iloc[-1])
    lines = [f"0DTE QQQ PLAN for {nxt:%a %b %d} (options accounts only). Sell a 1%-wide iron condor at 9:35 AM ET "
             f"only if the detector rates the day calm (probability of a big move <= {fitted['threshold']:.0%}):"]
    for g in gaps:
        q2, s2, v2 = qqq.copy(), spy.copy(), vix.copy()
        q2.loc[nxt] = {"open": last_close * (1 + g), "high": np.nan, "low": np.nan, "close": np.nan, "volume": np.nan}
        s2.loc[nxt] = {c: np.nan for c in s2.columns}
        v2.loc[nxt] = {"open": vix_close, "close": np.nan}
        p = float(predict(fitted["model"], fitted["cols"], features(q2, v2, s2).iloc[[-1]]).iloc[0])
        verdict = "SELL CONDOR" if p <= fitted["threshold"] else "no trade"
        lines.append(f"  QQQ opens {g:+.1%} (~${last_close * (1 + g):,.2f}): big-move probability {p:.0%} -> {verdict}")
    lines += [f"  If it's a SELL day (estimate at a flat open, VIX {vix_close:.1f}):"] + condor_plan(last_close, vix_close, accounts)
    lines.append("  Backtest 2021-2026 (never seen in training): won 89-93% of trades; +0.4% to +21%/yr at a 2% "
                 "max loss per trade, depending on real option prices. Tuesday-Friday: same rule each morning.")
    return lines


def evaluate_today(qqq: pd.DataFrame, spy: pd.DataFrame, vix: pd.DataFrame) -> dict:
    """Probability that today (the last row, which must hold today's open) is a big-move day."""
    fitted = load_model(qqq.iloc[:-1], spy.iloc[:-1], vix.iloc[:-1])
    p_today = float(predict(fitted["model"], fitted["cols"], features(qqq, vix, spy).iloc[[-1]]).iloc[0])
    return {"prob": p_today, "threshold": fitted["threshold"], "calm": p_today <= fitted["threshold"]}


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
        fitted = train(qqq, spy, vix)
        joblib.dump(fitted, MODEL_PATH)
        print(f"saved {MODEL_PATH} (trained through {fitted['trained_through']}, calm threshold {fitted['threshold']:.3f})")
        return

    qqq, spy, vix = history_local() if args.source == "local" else history_yf()
    today = pd.Timestamp(pd.Timestamp.now(tz="America/New_York").date())
    if qqq.index[-1] != today:
        raise SystemExit("Today's QQQ open is not available yet - run after 9:30 AM ET on a trading day.")
    # Today's bar is still forming: keep only its open (the model never looks at today's close/high/low).
    for df in (qqq, spy):
        df.loc[today, ["high", "low", "close", "volume"]] = np.nan
    verdict = evaluate_today(qqq, spy, vix)
    spot, vix_open = float(qqq.at[today, "open"]), float(vix["open"].iloc[-1])
    lines = [f"0DTE QQQ CHECK {today.date()} - probability of a big move {verdict['prob']:.0%} "
             f"(calm if <= {verdict['threshold']:.0%})"]
    if verdict["calm"]:
        lines.append(f"CALM DAY - sell an iron condor (QQQ opened ${spot:,.2f}):")
        lines += condor_plan(spot, vix_open, accounts_from_env())
    else:
        lines.append("No trade today: not calm enough for a condor.")
    text = "\n".join(lines)
    print(text)
    if verdict["calm"]:
        if args.text_out:
            Path(args.text_out).write_text(f"Subject: 0DTE QQQ condor {today.date()}\n\n{text}")
        if not args.no_email and email_configured():
            send_email(f"0DTE QQQ condor {today.date()}", text)

if __name__ == "__main__":
    main()
