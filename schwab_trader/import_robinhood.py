"""Merge saved Robinhood `get_equity_historicals` / `get_index_historicals` results
(JSON files) into the local price CSVs used with `--source local`.

    python -m schwab_trader.import_robinhood result1.txt result2.txt ...

Newer bars overwrite older ones for the same date; interpolated (gap-fill) bars are
skipped. Index results (e.g. VIX) are stored with volume 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from .config import DATA_DIR
from .data import COLUMNS


def parse(path: Path) -> dict[str, pd.DataFrame]:
    text = Path(path).read_text()
    payload = json.loads(text[text.index("{"):])
    out = {}
    for res in payload["data"]["results"]:
        if not res:
            continue
        bars = [b for b in res["bars"] if not b.get("interpolated")]
        price = "price" if "close_price" in (bars[0] if bars else {}) else "value"
        df = pd.DataFrame({
            "date": pd.to_datetime([b["begins_at"][:10] for b in bars]),
            "open": [float(b[f"open_{price}"]) for b in bars],
            "high": [float(b[f"high_{price}"]) for b in bars],
            "low": [float(b[f"low_{price}"]) for b in bars],
            "close": [float(b[f"close_{price}"]) for b in bars],
            "volume": [int(b.get("volume", 0)) for b in bars],
        }).drop_duplicates("date", keep="last").set_index("date")
        out[res["symbol"]] = df
    return out


def merge(frames: dict[str, pd.DataFrame], data_dir: Path = DATA_DIR) -> dict[str, str]:
    data_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for sym, new in frames.items():
        path = data_dir / f"{sym}.csv"
        if path.exists():
            old = pd.read_csv(path, parse_dates=["date"], index_col="date")
            new = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
        new[COLUMNS].to_csv(path)
        summary[sym] = f"{len(new)} rows through {new.index[-1].date()}"
    return summary


if __name__ == "__main__":
    for f in sys.argv[1:]:
        for sym, msg in merge(parse(Path(f))).items():
            print(f"{sym}: {msg}")
