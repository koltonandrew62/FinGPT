"""Event-driven daily backtest of the sector rotation with realistic small-account fills.

Rules the simulation enforces:
  * Signals use closes up to the prior trading day; orders fill at the next open.
  * Whole shares only, priced at the real (unadjusted) price on that day.
  * Sells execute before buys; buys never spend more cash than is available.
  * Slippage on every fill and SEC fees on sells; Schwab commissions are $0.
  * Idle cash earns 0% (conservative: a T-bill fund paid ~4-5% in 2023-25).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import BENCHMARK, Costs, StrategyConfig
from .strategy import compute_signal, is_rebalance_day, split_factor, whole_share_targets

TRADING_DAYS = 252


@dataclass
class BacktestResult:
    equity: pd.Series            # daily closing account value
    trades: pd.DataFrame
    holdings: pd.DataFrame       # real shares held after each rebalance
    cfg: StrategyConfig

    def metrics(self) -> dict:
        return performance(self.equity)


def performance(equity: pd.Series) -> dict:
    rets = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    drawdown = equity / equity.cummax() - 1.0
    vol = rets.std() * np.sqrt(TRADING_DAYS)
    return {
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
        "final_value": float(equity.iloc[-1]),
        "total_return": float(total),
        "cagr": float((1.0 + total) ** (1.0 / years) - 1.0),
        "volatility": float(vol),
        "sharpe": float(rets.mean() * TRADING_DAYS / vol) if vol > 0 else float("nan"),
        "max_drawdown": float(drawdown.min()),
    }


def run_backtest(prices: dict, cfg: StrategyConfig, start, end, initial_cash: float = 1500.0,
                 costs: Costs = Costs(), weights_fn=None, splits: dict | None = None) -> BacktestResult:
    """`weights_fn(history_closes) -> {symbol: weight}` (best first) replaces the
    ETF rotation signal, e.g. for the stock scorecard; `splits` overrides the
    ETF split table for whole-share sizing. A weights_fn with `wants_budget = True`
    is called as weights_fn(history, account_value, real_price_at_open)."""
    weights_fn = weights_fn or (lambda history: compute_signal(history, cfg).weights)
    opens, closes = prices["open"], prices["close"]
    all_dates = closes.index
    dates = all_dates[(all_dates >= pd.Timestamp(start)) & (all_dates <= pd.Timestamp(end))]
    if len(dates) == 0:
        raise ValueError("no trading days in the requested window")
    slip = costs.slippage_bps / 1e4

    cash = initial_cash
    units: dict[str, float] = {}   # split-adjusted share units; real shares = units / split factor
    equity, trades, holdings = [], [], []

    for i, day in enumerate(dates):
        pos = all_dates.get_loc(day)
        first_day = i == 0
        if first_day or is_rebalance_day(all_dates[pos - 1], day, cfg.rebalance):
            history = closes.iloc[:pos]                     # through the previous close only
            o = opens.loc[day]
            value = cash + sum(u * o[s] for s, u in units.items())
            if getattr(weights_fn, "wants_budget", False):  # e.g. skip stocks a small account can't afford
                weights = weights_fn(history, value, lambda s: o[s] * split_factor(s, day, splits))
            else:
                weights = weights_fn(history)
            factors = {s: split_factor(s, day, splits) for s in set(units) | set(weights)}
            real_px = {s: o[s] * factors[s] for s in factors}
            target = whole_share_targets(weights, value, real_px, costs.cash_buffer)
            current = {s: int(round(u / factors[s])) for s, u in units.items()}

            orders = {}
            for s in set(current) | set(target):
                diff = target.get(s, 0) - current.get(s, 0)
                keeps_position = current.get(s, 0) > 0 and target.get(s, 0) > 0
                if diff and not (keeps_position and abs(diff) * real_px[s] < costs.min_trade_value * value):
                    orders[s] = diff

            for s, qty in sorted(orders.items(), key=lambda kv: kv[1]):   # sells first
                if qty < 0:
                    fill = real_px[s] * (1 - slip)
                    proceeds = -qty * fill
                    cash += proceeds - proceeds * costs.sec_fee_rate
                    units[s] = units.get(s, 0.0) + qty * factors[s]
                    trades.append((day, s, "SELL", -qty, fill))
            for s in weights:                                             # buys in rank order
                qty = orders.get(s, 0)
                if qty > 0:
                    fill = real_px[s] * (1 + slip)
                    qty = min(qty, int(cash // fill))
                    if qty > 0:
                        cash -= qty * fill
                        units[s] = units.get(s, 0.0) + qty * factors[s]
                        trades.append((day, s, "BUY", qty, fill))
            units = {s: u for s, u in units.items() if abs(u) > 1e-9}
            holdings.append({"date": day, **{s: int(round(u / factors.get(s, split_factor(s, day, splits))))
                                             for s, u in units.items()}, "cash": round(cash, 2)})

        c = closes.loc[day]
        equity.append(cash + sum(u * c[s] for s, u in units.items()))

    return BacktestResult(
        equity=pd.Series(equity, index=dates, name=cfg.label()),
        trades=pd.DataFrame(trades, columns=["date", "symbol", "side", "shares", "price"]),
        holdings=pd.DataFrame(holdings).set_index("date"),
        cfg=cfg,
    )


def buy_and_hold(prices: dict, symbol: str, start, end, initial_cash: float = 1500.0,
                 whole_shares: bool = False, costs: Costs = Costs()) -> pd.Series:
    """Benchmark: buy `symbol` at the first open and hold. Fractional by default,
    i.e. the pure index price return with no cash drag (the harder bar to beat)."""
    closes = prices["close"][symbol]
    dates = closes.index[(closes.index >= pd.Timestamp(start)) & (closes.index <= pd.Timestamp(end))]
    day0 = dates[0]
    fill = prices["open"][symbol].loc[day0] * (1 + costs.slippage_bps / 1e4)
    real_fill = fill * split_factor(symbol, day0)
    shares = int(initial_cash // real_fill) if whole_shares else initial_cash / real_fill
    units = shares * split_factor(symbol, day0)
    cash = initial_cash - shares * real_fill
    return pd.Series(cash + units * closes.loc[dates].to_numpy(), index=dates, name=f"{symbol} buy & hold")


def yearly_returns(equity: pd.Series) -> pd.Series:
    """Calendar-year returns (first/last year are partial)."""
    year_end = equity.groupby(equity.index.year).last()
    prev = year_end.shift(1)
    prev.iloc[0] = equity.iloc[0]
    return year_end / prev - 1.0


def main():
    import argparse

    from .config import ALL_SYMBOLS, DEFAULT_CONFIG
    from .data import load_prices

    ap = argparse.ArgumentParser(description="Backtest the sector rotation against SPY.")
    ap.add_argument("--start", default=None, help="default: 5 years before the last data date")
    ap.add_argument("--end", default=None)
    ap.add_argument("--cash", type=float, default=1500.0)
    args = ap.parse_args()

    prices = load_prices(ALL_SYMBOLS)
    end = pd.Timestamp(args.end) if args.end else prices["close"].index[-1]
    start = pd.Timestamp(args.start) if args.start else end - pd.DateOffset(years=5)
    res = run_backtest(prices, DEFAULT_CONFIG, start, end, args.cash)
    spy = buy_and_hold(prices, BENCHMARK, start, end, args.cash)
    for name, eq in [(DEFAULT_CONFIG.label(), res.equity), ("SPY buy & hold", spy)]:
        m = performance(eq)
        print(f"{name:32s} ${m['final_value']:>9,.2f}  total {m['total_return']:+.1%}  CAGR {m['cagr']:+.1%}  "
              f"maxDD {m['max_drawdown']:.1%}  Sharpe {m['sharpe']:.2f}")
    print(f"{len(res.trades)} trades")


if __name__ == "__main__":
    main()
