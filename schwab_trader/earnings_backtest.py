"""Backtest of the earnings-drift play book.

Each week, look at the large caps whose 10-Q/10-K became public since the last
rebalance. Buy the highest-scoring ones (score >= threshold) at the next open and
hold for `hold_weeks`. Same fill rules as the sector backtest: whole shares at the
real price, slippage, sells before buys, cash earns 0%.

The entry date is the SEC filing date, which is the same day as or later than the
earnings press release, so the test can only enter late, never early.

    SEC_USER_AGENT="Your Name you@email.com" python -m schwab_trader.earnings_backtest
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass

import pandas as pd

from .backtest import BacktestResult, buy_and_hold, performance
from .config import BENCHMARK, DATA_DIR, RESULTS_DIR, Costs
from .earnings import BULLISH, company_facts, quarterly_fundamentals, score_history
from .strategy import is_rebalance_day, split_factor

# The largest US companies by market cap in October 2021 (start of the test window),
# excluding banks/insurers (free cash flow is not meaningful for them) and companies
# without US 10-Q filings. Fixed in advance to limit hindsight bias.
EARNINGS_UNIVERSE = (
    "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "META", "NVDA", "JNJ", "UNH", "V", "HD", "PG", "MA",
    "DIS", "ADBE", "CRM", "NFLX", "PYPL", "XOM", "CMCSA", "PFE", "CSCO", "KO", "PEP", "TMO", "ABT",
    "NKE", "INTC", "VZ", "AVGO", "COST", "WMT", "ABBV", "LLY", "MRK", "ACN", "DHR", "ORCL", "T",
    "CVX", "MCD", "QCOM", "TXN", "LIN", "MDT", "NEE", "UPS", "PM", "HON", "UNP", "LOW", "INTU",
    "AMD", "SBUX", "AMGN", "BMY", "IBM", "CAT", "BA",
)


@dataclass(frozen=True)
class EarningsConfig:
    threshold: float = BULLISH
    hold_weeks: int = 13
    max_positions: int = 3

    def label(self) -> str:
        return f"earnings thr{self.threshold:g} hold{self.hold_weeks}w max{self.max_positions}"


def build_events(fundamentals: dict) -> pd.DataFrame:
    """All point-in-time scores: one row per (symbol, quarterly filing)."""
    frames = []
    for sym, q in fundamentals.items():
        hist = score_history(q)
        if len(hist):
            frames.append(hist.assign(symbol=sym))
    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "end", "filed", "score"])
    return events.dropna(subset=["score"]).sort_values("filed").reset_index(drop=True)


def run_earnings_backtest(prices: dict, events: pd.DataFrame, cfg: EarningsConfig, start, end,
                          initial_cash: float = 1500.0, splits: dict | None = None,
                          costs: Costs = Costs()) -> BacktestResult:
    splits = splits or {}
    opens, closes = prices["open"], prices["close"]
    all_dates = closes.index
    dates = all_dates[(all_dates >= pd.Timestamp(start)) & (all_dates <= pd.Timestamp(end))]
    slip = costs.slippage_bps / 1e4

    cash = initial_cash
    held: dict[str, dict] = {}   # symbol -> {"units": split-adjusted units, "exit_on": Timestamp}
    last_rebalance = dates[0] - pd.Timedelta(days=7)
    equity, trades, holdings = [], [], []

    for i, day in enumerate(dates):
        pos = all_dates.get_loc(day)
        if i == 0 or is_rebalance_day(all_dates[pos - 1], day, "weekly"):
            o = opens.loc[day]
            # Exits first.
            for sym in [s for s, h in held.items() if h["exit_on"] <= day]:
                f = split_factor(sym, day, splits)
                qty = int(round(held[sym]["units"] / f))
                fill = o[sym] * f * (1 - slip)
                proceeds = qty * fill
                cash += proceeds - proceeds * costs.sec_fee_rate
                trades.append((day, sym, "SELL", qty, fill))
                del held[sym]
            # New reports since the last rebalance, best score first.
            new = events[(events.filed >= last_rebalance) & (events.filed < day)
                         & (events.score >= cfg.threshold) & events.symbol.isin(opens.columns)]
            new = new.sort_values("score", ascending=False).drop_duplicates("symbol")
            value = cash + sum(h["units"] * o[s] for s, h in held.items())
            slot = value / cfg.max_positions
            for _, ev in new.iterrows():
                sym = ev.symbol
                if len(held) >= cfg.max_positions:
                    break
                if sym in held or pd.isna(o.get(sym)):
                    continue
                f = split_factor(sym, day, splits)
                fill = o[sym] * f * (1 + slip)
                qty = int(min(slot * (1 - costs.cash_buffer), cash) // fill)
                if qty <= 0:
                    continue   # a single share costs more than the slot: unaffordable for this account
                cash -= qty * fill
                held[sym] = {"units": qty * f, "exit_on": day + pd.Timedelta(weeks=cfg.hold_weeks)}
                trades.append((day, sym, "BUY", qty, fill))
            last_rebalance = day
            holdings.append({"date": day, **{s: int(round(h["units"] / split_factor(s, day, splits)))
                                             for s, h in held.items()}, "cash": round(cash, 2)})
        c = closes.loc[day]
        equity.append(cash + sum(h["units"] * c[s] for s, h in held.items()))

    return BacktestResult(
        equity=pd.Series(equity, index=dates, name=cfg.label()),
        trades=pd.DataFrame(trades, columns=["date", "symbol", "side", "shares", "price"]),
        holdings=pd.DataFrame(holdings).set_index("date"),
        cfg=cfg,
    )


GRID = [EarningsConfig(threshold=t, hold_weeks=h, max_positions=m)
        for t, h, m in itertools.product([0.0, 0.25, 0.4], [4, 8, 13], [2, 3, 4])]
DESIGN = ("2012-01-03", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")


def main():
    from .data import load_prices

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cash", type=float, default=1500.0)
    args = ap.parse_args()

    fundamentals = {}
    for sym in EARNINGS_UNIVERSE:
        try:
            fundamentals[sym] = quarterly_fundamentals(company_facts(sym))
        except Exception as exc:   # one bad ticker should not stop the study
            print(f"skip {sym}: {exc}")
    events = build_events(fundamentals)
    prices = load_prices([s for s in EARNINGS_UNIVERSE if (DATA_DIR / f"{s}.csv").exists()] + [BENCHMARK])
    splits_path = DATA_DIR / "splits.json"
    splits = json.loads(splits_path.read_text()) if splits_path.exists() else {}

    def grid(window):
        rows = []
        for cfg in GRID:
            m = performance(run_earnings_backtest(prices, events, cfg, *window, args.cash, splits).equity)
            rows.append({"label": cfg.label(), "threshold": cfg.threshold, "hold_weeks": cfg.hold_weeks,
                         "max_positions": cfg.max_positions, **m})
        return pd.DataFrame(rows)

    design = grid(DESIGN)
    best = design.sort_values(["cagr", "sharpe"], ascending=False).iloc[0]   # max return, chosen on design data
    cfg = EarningsConfig(float(best.threshold), int(best.hold_weeks), int(best.max_positions))
    res = run_earnings_backtest(prices, events, cfg, *TEST, args.cash, splits)
    spy = buy_and_hold(prices, BENCHMARK, *TEST, args.cash)
    RESULTS_DIR.mkdir(exist_ok=True)
    design.to_csv(RESULTS_DIR / "earnings_design_grid.csv", index=False, float_format="%.4f")
    grid(TEST).to_csv(RESULTS_DIR / "earnings_test_grid.csv", index=False, float_format="%.4f")
    res.trades.to_csv(RESULTS_DIR / "earnings_test_trades.csv", index=False, float_format="%.4f")
    print("selected on design period:", cfg.label())
    for name, eq in [("earnings strategy", res.equity), ("SPY", spy)]:
        m = performance(eq)
        print(f"  {name:18s} ${m['final_value']:>10,.2f}  CAGR {m['cagr']:+6.1%}  maxDD {m['max_drawdown']:6.1%}")


if __name__ == "__main__":
    main()
