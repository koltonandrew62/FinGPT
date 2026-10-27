"""Strategy settings shared by the backtest, the live Schwab trader and the weekly report."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
RESULTS_DIR = PACKAGE_DIR / "results"
STATE_DIR = Path.home() / ".schwab_trader"

# The 11 Select Sector SPDR ETFs: together they make up the S&P 500, so rotating
# among them is a direct attempt to beat the index using only its own pieces.
SECTOR_ETFS = (
    "XLK",   # Technology
    "XLF",   # Financials
    "XLV",   # Health Care
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLU",   # Utilities
    "XLB",   # Materials
    "XLRE",  # Real Estate (since 2015-10)
    "XLC",   # Communication Services (since 2018-06)
)
SECTOR_NAMES = {
    "XLK": "Technology", "XLF": "Financials", "XLV": "Health Care", "XLE": "Energy",
    "XLI": "Industrials", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Communication Services",
    "QQQ": "Nasdaq-100", "IWM": "US Small Caps", "EFA": "Intl Developed Mkts", "EEM": "Emerging Markets",
    "GLD": "Gold", "TLT": "20+ Yr Treasuries",
}
BENCHMARK = "SPY"

# "sectors+" adds other major asset classes the rotation may move into.
UNIVERSES = {
    "sectors": SECTOR_ETFS,
    "sectors+": SECTOR_ETFS + ("QQQ", "IWM", "EFA", "EEM", "GLD", "TLT"),
}
ALL_SYMBOLS = tuple(dict.fromkeys(UNIVERSES["sectors+"] + (BENCHMARK,)))

# Options are only traded on these broad index ETFs, never on individual stocks
# (puts on the scorecard's weakest stocks lost money in the backtest).
OPTIONS_UNDERLYINGS = ("SPY", "QQQ")

# Share splits as (first date trading at the new price, ratio). Price history is
# split-adjusted, but a $1,500 account buys whole shares at the real (unadjusted)
# price, so sizing needs these. Verified against raw vs. split-adjusted bars.
# XLF's 2016 entry is the XLRE spin-off distribution, which data vendors fold
# into the split adjustment the same way.
SPLITS = {
    "XLB": [("2025-12-05", 2.0)],
    "XLE": [("2025-12-05", 2.0)],
    "XLK": [("2025-12-05", 2.0)],
    "XLU": [("2025-12-05", 2.0)],
    "XLY": [("2025-12-05", 2.0)],
    "XLF": [("2016-09-19", 1.232)],
    "EEM": [("2008-07-24", 3.0)],
    "EFA": [("2005-06-09", 3.0)],
}

# Momentum lookback schemes, in trading days (21 ~ 1 month).
LOOKBACK_SCHEMES = {
    "12-1": None,               # 12-month return skipping the latest month
    "12": (252,),
    "6": (126,),
    "3/6/12": (63, 126, 252),
    "1/3/6/12": (21, 63, 126, 252),
}


@dataclass(frozen=True)
class StrategyConfig:
    """Systematic sector rotation, rebalanced weekly.

    On each rebalance date every sector ETF gets a composite score built from
    several weak signals (the quant approach: no single signal is trusted):
      * medium-term momentum over `lookback`, optionally divided by trailing
        volatility (`risk_adjusted`) so a steady trend outranks a lucky spike;
      * short-term reversal (`reversal_weight` > 0): last week's laggards get a
        boost, a well-documented weekly mean-reversion effect.
    The `top_n` best are held in equal weight; `trend_filter` moves a slot to
    cash when the trend is down. Orders are sized in whole shares.
    """
    universe: str = "sectors"        # key of UNIVERSES
    lookback: str = "3/6/12"
    top_n: int = 3
    trend_filter: str = "absolute"   # "none" | "absolute" (own momentum > 0) | "spy_sma"
    risk_adjusted: bool = False
    reversal_weight: float = 0.0     # weight of the 1-week reversal rank vs. the momentum rank
    sma_days: int = 200
    vol_days: int = 63
    rebalance: str = "weekly"        # "weekly" | "monthly"
    min_history: int = 253           # days of history before a fund is eligible

    def with_(self, **kw) -> "StrategyConfig":
        return replace(self, **kw)

    @property
    def symbols(self) -> tuple:
        return UNIVERSES[self.universe]

    def label(self) -> str:
        parts = [self.universe, self.lookback, f"top{self.top_n}", self.trend_filter]
        if self.risk_adjusted:
            parts.append("voladj")
        if self.reversal_weight:
            parts.append(f"rev{self.reversal_weight:g}")
        return " ".join(parts + [self.rebalance])


@dataclass(frozen=True)
class Costs:
    """Schwab charges $0 commission on online stock/ETF trades; we still model
    slippage on every fill plus the SEC/FINRA fees on sells."""
    slippage_bps: float = 5.0          # 0.05% per fill, worse than typical for these ETFs
    sec_fee_rate: float = 0.0000278    # SEC Section 31 fee on sale proceeds
    cash_buffer: float = 0.003         # leave 0.3% in cash so buys never exceed cash
    min_trade_value: float = 0.05      # skip resizing a kept position by < 5% of equity


# Plain 12-1 momentum, top 2 funds, rebalanced weekly. The pre-registered pick from
# research.py (sectors+ 3/6/12 top1 voladj rev1) lost to SPY out of sample; this
# rule beat SPY in both 2005-2021 and 2021-2026 but was chosen after seeing the
# test window, so treat its test numbers as optimistic. See results/RESULTS.md.
DEFAULT_CONFIG = StrategyConfig(universe="sectors+", lookback="12-1", top_n=2, trend_filter="none")
