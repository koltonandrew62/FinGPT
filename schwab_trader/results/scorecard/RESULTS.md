# Weekly stock scorecard (0–100) – results

59 large US stocks (the biggest companies as of October 2021, banks excluded). Every
week each stock gets a 0–100 score; if **any** of its components scores below 25 it is
**rejected** as a buy; the weakest stocks, where trend and momentum both fail, form the
**put/avoid** list. Only the 2021-10 → 2026-10 test is free of hindsight (the stock
list was fixed at its start); 2011–2021 numbers favour stocks that later became giants.

## Version 1: seven components, weights fixed in advance — failed

trend, momentum, relative strength, RSI, low volatility, volume accumulation, distance
from the 52-week high.

| Score bucket | Avg next-4-week return |
|---|---:|
| 0–20 | **+1.7%** |
| 35–50 | +0.6% |
| 60–70 | +0.6% |
| 80–100 | +0.8% |

- A higher score did **not** mean higher returns; if anything the weakest stocks bounced.
- The reject rule threw out winners: buy-grade stocks it rejected made +1.4% over the
  next 4 weeks vs. +0.15% for the ones it kept.
- Put list: its stocks rose 0.55% per 4 weeks (all stocks: +0.74%); 1-month puts on them
  lost 27% per trade on average at typical option prices.
- Top-5 weekly portfolio: 11.1%/yr at $1.5M, 9.6%/yr at $1,500 (SPY 10.8%).

Checking each component on its own (rank correlation with the next 1 and 4 weeks'
returns) showed why: only **momentum** helped in both periods; **low volatility** was
backwards (the more volatile stocks did better, t = −4.3 in 2011–2021); trend, relative
strength, RSI, volume and drawdown added nothing reliable.

## Version 2 (default): momentum + volatility, from 2011–2021 evidence

50% momentum (12-month return skipping the last month, ranked) + 50% volatility
(more volatile ranks higher). Reject if either is below 25. Tested once on 2021–2026:

| Score bucket | Avg next-4-week return | vs. SPY |
|---|---:|---:|
| 0–20 | −0.2% | −0.7% |
| 35–50 | +0.5% | −0.4% |
| 60–70 | +1.1% | +0.2% |
| 80–100 | **+1.8%** | **+0.9%** |

| Top 5 each week | Ended at | Per year | Worst drop |
|---|---:|---:|---:|
| Dad-size account ($1.5M start) | $3.78M | **19.9%** | −39.7% |
| Your account ($1,500 start) | $2,117 | 7.1% | −39.6% |
| SPY | $2,508 per $1,500 | 10.8% | −25.4% |

| Year | Top 5 (v2) | SPY |
|---|---:|---:|
| 2022 | −22.5% | −19.5% |
| 2023 | +40.7% | +24.3% |
| 2024 | +41.8% | +23.3% |
| 2025 | +23.8% | +16.4% |
| 2026 (to Oct 23) | +30.8% | +11.4% |

- Higher scores now did lead to higher returns, and the put list fell 0.13% per 4 weeks
  while the other stocks rose 0.79% (it lagged them in 60% of weeks).
- **But the put list is a "don't own" list, not a trade:** 1-month at-the-money puts on
  it lost 4.4% per trade at typical option prices (won 33% of the time), and shorting it
  made ~0%.
- The $1,500 account can't hold five of these stocks in whole shares (AMD, CAT and others
  cost more than a $300 slot), which is why it trails; the ETF rotation suits it better.
- With only two components, the reject rule never bites: any stock scoring 60+ already
  has both above 25.
- "More volatile = better" partly reflects the hindsight-biased 2011–2021 list; it held
  in the clean test, but over one market regime that favoured big, volatile tech.

Reproduce: `python -m schwab_trader.scorecard_research` (needs the stock prices from
`python -m schwab_trader.data --stocks`). Raw numbers: `summary.json`.
