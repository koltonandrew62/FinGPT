"""Daily price data: load cached CSVs, or download them from Schwab or Yahoo.

All prices are split-adjusted but NOT dividend-adjusted, so every return in the
backtest (strategy and S&P 500 benchmark alike) is a price return.

    python -m schwab_trader.data --source yfinance            # free, no account needed
    python -m schwab_trader.data --source schwab              # uses your Schwab API app
    python -m schwab_trader.data --stocks                     # + earnings-strategy stocks & splits
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from .config import ALL_SYMBOLS, DATA_DIR

COLUMNS = ["open", "high", "low", "close", "volume"]


def load_prices(symbols, data_dir: Path = DATA_DIR) -> dict[str, pd.DataFrame]:
    """Return {"open": df, "close": df, ...}, each indexed by date with one column per symbol."""
    frames = {}
    for sym in symbols:
        path = Path(data_dir) / f"{sym}.csv"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing - run `python -m schwab_trader.data` first")
        frames[sym] = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return {col: pd.DataFrame({s: f[col] for s, f in frames.items()}).sort_index() for col in COLUMNS}


def save_symbol(sym: str, df: pd.DataFrame, data_dir: Path = DATA_DIR) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    df = df[COLUMNS].dropna(subset=["close"])
    df.index.name = "date"
    df.to_csv(Path(data_dir) / f"{sym}.csv")


def fetch_yfinance(symbols, start: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out = {}
    for sym in symbols:
        # auto_adjust=False -> Open/High/Low/Close are split-adjusted only.
        raw = yf.download(sym, start=start, auto_adjust=False, progress=False, multi_level_index=False)
        if raw.empty:
            raise RuntimeError(f"Yahoo returned no data for {sym}")
        df = raw.rename(columns=str.lower)[COLUMNS]
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        out[sym] = df
    return out


def fetch_yfinance_splits(symbols) -> dict:
    """{symbol: [(first date at the new price, ratio), ...]} for whole-share sizing."""
    import yfinance as yf

    out = {}
    for sym in symbols:
        s = yf.Ticker(sym).splits
        if len(s):
            out[sym] = [(pd.Timestamp(d).date().isoformat(), float(r)) for d, r in s.items()]
    return out


def fetch_schwab(client, symbols, start: str) -> dict[str, pd.DataFrame]:
    """Daily candles from the Schwab Market Data API (split-adjusted)."""
    out = {}
    start_dt = dt.datetime.fromisoformat(start)
    for sym in symbols:
        resp = client.get_price_history_every_day(sym, start_datetime=start_dt, end_datetime=dt.datetime.now())
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        if not candles:
            raise RuntimeError(f"Schwab returned no candles for {sym}")
        df = pd.DataFrame(candles)
        # Candle timestamps are epoch ms at midnight US/Eastern of the trading day.
        df["date"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
        out[sym] = df.set_index("date")[COLUMNS]
    return out


def drop_unfinished_today(df: pd.DataFrame, now: dt.datetime | None = None) -> pd.DataFrame:
    """Remove today's bar while the market is still open, so signals use completed closes only."""
    now = now or pd.Timestamp.now(tz="America/New_York")
    today = pd.Timestamp(now.date())
    if len(df) and df.index[-1] == today and (now.hour, now.minute) < (16, 15):
        return df.iloc[:-1]
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["yfinance", "schwab"], default="yfinance")
    ap.add_argument("--start", default="2004-01-01")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--stocks", action="store_true", help="also fetch the earnings-strategy stocks (yfinance)")
    args = ap.parse_args()

    symbols = list(ALL_SYMBOLS)
    if args.stocks:
        from .earnings_backtest import EARNINGS_UNIVERSE
        symbols += list(EARNINGS_UNIVERSE)
        Path(args.data_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.data_dir) / "splits.json").write_text(json.dumps(fetch_yfinance_splits(EARNINGS_UNIVERSE), indent=1))
    if args.source == "schwab":
        from .live_trader import make_client
        frames = fetch_schwab(make_client(), symbols, args.start)
    else:
        frames = fetch_yfinance(symbols, args.start)
    try:   # VIX for the options study (Yahoo symbol ^VIX)
        frames["VIX"] = fetch_yfinance(["^VIX"], args.start)["^VIX"].assign(volume=0)
    except Exception as exc:
        print(f"VIX download failed ({exc}); the options study needs data/VIX.csv")
    for sym, df in frames.items():
        save_symbol(sym, df, args.data_dir)
        print(f"{sym}: {len(df)} days {df.index[0].date()} .. {df.index[-1].date()}")


if __name__ == "__main__":
    main()
