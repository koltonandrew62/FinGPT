"""How much to buy, per account.

Accounts come from the ACCOUNTS environment variable, one per comma:
    name:value:strategy_share:options_risk:cash|margin
e.g. ACCOUNTS="You:1500:1.0:0:cash,Dad:1500000:0.10:0.02:margin"

  strategy_share  fraction of the account the stock/ETF strategies may run
  options_risk    most of the account one options trade may lose (0 = no options in this account)
  cash|margin     margin accounts (>= $2,000 at Schwab) can short; cash accounts can only buy puts
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .strategy import whole_share_targets

# Your account trades shares only; Dad's runs the share strategy with 10% of the account
# and the 0DTE condor with at most 2% of it at risk per trade (the size backtested).
DEFAULT_ACCOUNTS = "You:1500:1.0:0:cash,Dad:1500000:0.10:0.02:margin"


@dataclass(frozen=True)
class Account:
    name: str
    value: float
    strategy_share: float = 1.0
    options_risk: float = 0.02
    margin: bool = False

    @property
    def strategy_dollars(self) -> float:
        return self.value * self.strategy_share

    @property
    def options_dollars(self) -> float:
        return self.value * self.options_risk


def accounts_from_env(spec: str | None = None) -> list[Account]:
    out = []
    for item in (spec or os.environ.get("ACCOUNTS", DEFAULT_ACCOUNTS)).split(","):
        name, value, share, risk, kind = (x.strip() for x in item.split(":"))
        out.append(Account(name, float(value), float(share), float(risk), kind.lower() == "margin"))
    return out


def share_plan(weights: dict, prices: dict, account: Account) -> dict:
    """{symbol: (shares, dollars)} for the strategy's slice of `account`."""
    shares = whole_share_targets(weights, account.strategy_dollars, prices)
    return {s: (n, n * prices[s]) for s, n in shares.items()}


def contracts(unit_cost: float, account: Account) -> int:
    """Whole option positions (one unit = all legs x 100 shares incl. fees) within the options budget."""
    return int(account.options_dollars // unit_cost) if unit_cost > 0 else 0


def money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 1000 else f"${x:,.2f}"
