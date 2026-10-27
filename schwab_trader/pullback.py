"""High-hit-rate share strategy: buy short, sharp dips in stocks that are in a strong
uptrend, sell on the bounce.

Each day after the close, a stock is a BUY when
  * it is above its 200-day average (long-term uptrend),
  * optionally it ranks in the top `momentum_top` of the pool by 12-1 momentum, and
  * its 2-day RSI is below `entry_rsi` (a 1-3 day sell-off).
It is bought at the next open and sold at the next open after the close rises back
above its 5-day average (or 2-day RSI > 70), or after `max_hold` days. Positions are
equal slots of the account, whole shares only, skipping shares the account can't
afford. Win rate = share of round trips that made money after costs.

    python -m schwab_trader.pullback
"""
from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .backtest import buy_and_hold, performance
from .config import ALL_SYMBOLS, BENCHMARK, DATA_DIR, RESULTS_DIR, SPLITS, UNIVERSES, Costs
from .data import load_prices
from .earnings_backtest import EARNINGS_UNIVERSE
from .strategy import split_factor

STOCKS = tuple(EARNINGS_UNIVERSE)
ETFS = tuple(UNIVERSES["sectors+"])
POOLS = {"stocks+etfs": STOCKS + ETFS, "stocks": STOCKS, "etfs": ETFS}
OUT = RESULTS_DIR / "pullback"


@dataclass(frozen=True)
class PullbackConfig:
    pool: str = "stocks+etfs"
    momentum_top: int = 20        # 0 = any stock above its 200-day average
    entry_rsi: float = 10.0
    exit_rule: str = "sma5"       # "sma5" | "rsi70" | "up_close" (first close above the prior close)
    max_hold: int = 10
    max_positions: int = 3
    market_filter: bool = False   # only buy while SPY is above its 200-day average

    def label(self) -> str:
        mom = f"mom-top{self.momentum_top}" if self.momentum_top else "any-uptrend"
        mkt = " spy>200d" if self.market_filter else ""
        return (f"{self.pool} {mom} rsi2<{self.entry_rsi:g} exit-{self.exit_rule} hold<={self.max_hold} "
                f"max{self.max_positions}{mkt}")


def rsi(close: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def indicators(close: pd.DataFrame) -> dict:
    enough = close.notna().rolling(253).sum() >= 253
    mom = (close.shift(21) / close.shift(252) - 1).where(enough)
    return {"rsi2": rsi(close), "sma5": close.rolling(5).mean(), "sma200": close.rolling(200).mean(),
            "mom_rank": mom.rank(axis=1, ascending=False)}


def signals(close: pd.DataFrame, ind: dict, cfg: PullbackConfig,
            market: pd.Series | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(entry, exit) boolean frames, evaluated at each day's close."""
    entry = (close > ind["sma200"]) & (ind["rsi2"] < cfg.entry_rsi)
    if cfg.momentum_top:
        entry &= ind["mom_rank"] <= cfg.momentum_top
    if cfg.market_filter and market is not None:
        entry = entry.mul(market > market.rolling(200).mean(), axis=0).astype(bool)
    exit_ = {"sma5": close > ind["sma5"], "rsi70": ind["rsi2"] > 70, "up_close": close > close.shift(1)}[cfg.exit_rule]
    return entry.fillna(False), exit_.fillna(False)


def run_pullback(prices: dict, cfg: PullbackConfig, start, end, initial_cash: float = 1500.0,
                 splits: dict | None = None, costs: Costs = Costs(), ind: dict | None = None):
    syms = list(POOLS[cfg.pool])
    close, opens = prices["close"][syms], prices["open"][syms]
    ind = ind or indicators(close)
    entry, exit_ = signals(close, {k: v[syms] for k, v in ind.items()}, cfg, prices["close"].get(BENCHMARK))
    idx = close.index
    days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]
    slip = costs.slippage_bps / 1e4
    cash, held, equity, trips = initial_cash, {}, [], []
    for day in days:
        i = idx.get_loc(day)
        prev = idx[i - 1]
        o = opens.loc[day]
        # exits at today's open, decided at yesterday's close
        for s in [s for s, h in held.items() if exit_.at[prev, s] or (i - h["i"]) >= cfg.max_hold]:
            h = held.pop(s)
            f = split_factor(s, day, splits)
            fill = o[s] * f * (1 - slip)
            n = int(round(h["units"] / f))
            proceeds = n * fill * (1 - costs.sec_fee_rate)
            cash += proceeds
            trips.append({"symbol": s, "entry": idx[h["i"]], "exit": day, "return": proceeds / h["cost"] - 1})
        # entries: yesterday's signals, most oversold first
        cands = entry.loc[prev]
        cands = cands[cands].index.difference(list(held))
        if len(cands) and len(held) < cfg.max_positions:
            value = cash + sum(h["units"] * o[s] for s, h in held.items())
            slot = value / cfg.max_positions
            for s in ind["rsi2"].loc[prev, cands].sort_values().index:
                if len(held) >= cfg.max_positions:
                    break
                f = split_factor(s, day, splits)
                fill = o[s] * f * (1 + slip)
                n = int(min(slot, cash) // fill)
                if n <= 0 or not np.isfinite(fill):
                    continue
                cash -= n * fill
                held[s] = {"units": n * f, "cost": n * fill, "i": i}
        c = close.loc[day]
        equity.append(cash + sum(h["units"] * c[s] for s, h in held.items()))
    eq = pd.Series(equity, index=days)
    eq.attrs["held"] = {s: {"since": idx[h["i"]], "i": h["i"], "shares": h["units"]} for s, h in held.items()}
    return eq, pd.DataFrame(trips)


GRID = [PullbackConfig(pool=p, momentum_top=m, entry_rsi=r, exit_rule=x, max_positions=n)
        for p, m, r, x, n in itertools.product(["stocks+etfs", "etfs"], [0, 10, 20], [5, 10],
                                               ["sma5", "rsi70"], [2, 3, 5])]
# Second pass on the family that held up (stocks+ETFs, top-10 momentum, RSI2 < 10):
# the two standard ways to raise a dip-buyer's hit rate.
GRID_2 = [PullbackConfig(pool="stocks+etfs", momentum_top=10, entry_rsi=10, exit_rule=x, max_hold=h,
                         max_positions=n, market_filter=mf)
          for x, h, n, mf in itertools.product(["rsi70", "up_close", "sma5"], [10, 20], [2, 3], [False, True])]
DESIGN = ("2011-02-01", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")


def main():
    import sys
    global GRID, OUT
    if "--pass2" in sys.argv:
        GRID, OUT = GRID_2, RESULTS_DIR / "pullback_pass2"
    OUT.mkdir(parents=True, exist_ok=True)
    prices = load_prices(list(dict.fromkeys(list(ALL_SYMBOLS) + list(STOCKS))))
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    ind = indicators(prices["close"][list(POOLS["stocks+etfs"])])
    rows = []
    for cfg in GRID:
        r = {"label": cfg.label(), **asdict(cfg)}
        for name, w in (("design", DESIGN), ("test", TEST)):
            eq, trips = run_pullback(prices, cfg, *w, 1500.0, splits, ind=ind)
            m = performance(eq)
            r |= {f"{name}_win_rate": float((trips["return"] > 0).mean()) if len(trips) else np.nan,
                  f"{name}_trades": len(trips), f"{name}_cagr": m["cagr"], f"{name}_final": m["final_value"],
                  f"{name}_maxdd": m["max_drawdown"], f"{name}_avg_trade": float(trips["return"].mean()) if len(trips) else np.nan}
        rows.append(r)
        print(f"{cfg.label():62s} design win {r['design_win_rate']:.0%} cagr {r['design_cagr']:+.1%} | "
              f"test win {r['test_win_rate']:.0%} cagr {r['test_cagr']:+.1%} ${r['test_final']:,.0f}", flush=True)
    grid = pd.DataFrame(rows)
    grid.to_csv(OUT / "grid.csv", index=False, float_format="%.4f")
    # Rule fixed before running: among design-period variants winning >= 80% of trades,
    # take the highest design CAGR; then report its test numbers.
    ok = grid[grid.design_win_rate >= 0.80]
    pick = (ok if len(ok) else grid).sort_values("design_cagr", ascending=False).iloc[0]
    spy = {k: performance(buy_and_hold(prices, BENCHMARK, *w, 1500.0)) for k, w in (("design", DESIGN), ("test", TEST))}
    summary = {"selected": pick.to_dict(), "variants_with_80pct_design": int(len(ok)),
               "variants_with_80pct_test": int((grid.test_win_rate >= 0.80).sum()),
               "spy_design_cagr": spy["design"]["cagr"], "spy_test_cagr": spy["test"]["cagr"],
               "spy_test_final": spy["test"]["final_value"]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
