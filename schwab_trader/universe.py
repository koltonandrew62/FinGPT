"""The stock list for the daily plan: every stock in the S&P 500, the Nasdaq-100 and
the Dow 30 (the Dow's 30 are all in the S&P 500), about 520 companies.

Backtests use **point-in-time S&P 500 membership**: a stock can only be picked on days
it was actually in the index (dates from github.com/fja05680/sp500 and the S&P list at
github.com/datasets/s-and-p-500-companies), so a stock added after a big run-up (Tesla
in December 2020, for example) can't be bought before it joined. Nasdaq-100 stocks
that aren't in the S&P 500 (about 15) are only used live, because their membership
history isn't available. Companies that left the index and no longer trade (bought
out, bankrupt) have no price history here, so some survivorship bias remains.

    python -m schwab_trader.universe --build SP500_CONSTITUENTS.csv SP500_TICKER_START_END.csv NASDAQ100.csv
"""
from __future__ import annotations

import csv
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_DIR, PACKAGE_DIR

LISTS_DIR = PACKAGE_DIR / "index_lists"
DOW30 = ("AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "GS", "HD", "HON", "IBM", "JNJ",
         "JPM", "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT")
# removed from the S&P 500 in September/October 2026 (after the membership history ends);
# their exact removal day isn't in either list
LATE_2026_REMOVAL = "2026-09-21"
DROPPED = {"BLDR": ("Builders FirstSource", "Industrials"), "CTVA": ("Corteva", "Materials"),
           "TAP": ("Molson Coors", "Consumer Staples"), "TTD": ("The Trade Desk", "Information Technology"),
           "PSKY": ("Paramount Skydance", "Communication Services"), "WBD": ("Warner Bros. Discovery", "Communication Services")}
NDX_SECTORS = {"Technology": "Information Technology", "Telecommunications": "Communication Services"}


@lru_cache(maxsize=None)
def _companies() -> pd.DataFrame:
    return pd.read_csv(LISTS_DIR / "companies.csv", keep_default_na=False)


@lru_cache(maxsize=None)
def _membership() -> pd.DataFrame:
    m = pd.read_csv(LISTS_DIR / "sp500_membership.csv", parse_dates=["start", "end"])
    m["end"] = m["end"].fillna(pd.Timestamp("2100-01-01"))
    return m


def stocks(with_data: bool = True) -> tuple:
    """Every S&P 500 / Nasdaq-100 / Dow 30 stock (that has a price file, by default)."""
    syms = _companies().symbol.tolist()
    if with_data:
        syms = [s for s in syms if (DATA_DIR / f"{s}.csv").exists()]
    return tuple(syms)


def sector(symbol: str) -> str:
    c = _companies()
    row = c[c.symbol == symbol]
    return row.sector.iloc[0] if len(row) else ""


def name(symbol: str) -> str:
    c = _companies()
    row = c[c.symbol == symbol]
    return row.name.iloc[0] if len(row) else symbol


def sp500_mask(index: pd.DatetimeIndex, symbols) -> pd.DataFrame:
    """True where the stock was in the S&P 500 on that day. Symbols that aren't stocks
    from the lists (ETFs, ...) are always True."""
    m = _membership()
    listed = set(_companies().symbol)
    out = pd.DataFrame(True, index=index, columns=list(symbols))
    for s in out.columns:
        if s not in listed:
            continue
        rows = m[m.symbol == s]
        col = np.zeros(len(index), dtype=bool)
        for r in rows.itertuples():
            col |= (index >= r.start) & (index < r.end)
        out[s] = col
    return out


def was_member(symbols, days) -> np.ndarray:
    """Row-wise S&P membership for (symbol, day) pairs (non-list symbols -> True)."""
    m = _membership()
    listed = set(_companies().symbol)
    by = {s: g for s, g in m.groupby("symbol")}
    out = []
    for s, d in zip(symbols, pd.DatetimeIndex(days)):
        if s not in listed:
            out.append(True)
            continue
        g = by.get(s)
        out.append(g is not None and bool(((g.start <= d) & (d < g.end)).any()))
    return np.array(out, dtype=bool)


# ------------------------------------------------------------------ build
def build(sp_path: str, hist_path: str, ndx_path: str) -> None:
    """Write index_lists/companies.csv and sp500_membership.csv from the downloaded lists."""
    sp = list(csv.DictReader(open(sp_path)))
    hist = list(csv.DictReader(open(hist_path)))
    ndx = list(csv.DictReader(open(ndx_path)))
    current = {r["Symbol"] for r in sp}
    hist_current = {r["ticker"] for r in hist if not r["end_date"]}
    companies = {r["Symbol"]: {"name": r["Security"], "sector": r["GICS Sector"], "sp500": 1} for r in sp}
    for t in sorted(hist_current - current):                  # dropped after the history file's last update
        nm, sec = DROPPED.get(t, (t, ""))
        companies[t] = {"name": nm, "sector": sec, "sp500": 0}
    for r in ndx:   # this file's two GICS columns are swapped: the 4th holds the sector
        sec = NDX_SECTORS.get(r["GICS_Sub_Industry"], r["GICS_Sub_Industry"])
        companies.setdefault(r["Ticker"], {"name": r["Company"], "sector": sec, "sp500": 0})
        companies[r["Ticker"]]["nasdaq100"] = 1
    for s in DOW30:
        companies[s]["dow30"] = 1
    rows = []
    for r in sp:                                              # current members from the date they joined
        rows.append((r["Symbol"], r["Date added"], ""))
    for r in hist:                                            # earlier spells under the same ticker
        if r["ticker"] not in companies:
            continue
        end = r["end_date"] or ("" if r["ticker"] in current else LATE_2026_REMOVAL)
        rows.append((r["ticker"], r["start_date"], end))
    merged = []
    for s in sorted({r[0] for r in rows}):
        spells = sorted((pd.Timestamp(a), pd.Timestamp(b) if b else pd.Timestamp.max) for x, a, b in rows if x == s)
        cur = list(spells[0])
        for a, b in spells[1:]:
            if a <= cur[1]:
                cur[1] = max(cur[1], b)
            else:
                merged.append((s, *cur))
                cur = [a, b]
        merged.append((s, *cur))
    LISTS_DIR.mkdir(exist_ok=True)
    pd.DataFrame([{"symbol": s, "start": a.date(), "end": "" if b == pd.Timestamp.max else b.date()} for s, a, b in merged]
                 ).to_csv(LISTS_DIR / "sp500_membership.csv", index=False)
    pd.DataFrame([{"symbol": s, "name": v["name"], "sector": v["sector"], "sp500": v.get("sp500", 0),
                   "nasdaq100": v.get("nasdaq100", 0), "dow30": v.get("dow30", 0)} for s, v in sorted(companies.items())]
                 ).to_csv(LISTS_DIR / "companies.csv", index=False)
    print(f"{len(companies)} companies, {len(merged)} S&P membership spells")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--build"]:
        build(*sys.argv[2:5])
    else:
        print(len(stocks()), "stocks with price data")
