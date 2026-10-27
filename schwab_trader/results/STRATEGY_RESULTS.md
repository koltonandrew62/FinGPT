# Which strategy, and how accurate is it?

All numbers: weekly or daily trading, whole shares/contracts at real prices, slippage
and Schwab fees included, last 5 years = 2021-10-25 → 2026-10-23 unless noted. The
stock list is the largest US companies as of October 2021, so 2011–2021 results for
stock-picking strategies have hindsight; 2021–2026 does not.

## The strategies in the emails (by account size)

`share_competition.py` raced 14 share-only strategies at each size. Rule fixed before
running: highest 2021–2026 result among those that also beat SPY in 2011–2021. Under
$25,000 the emails and the bot use the $1,500 winner; from $25,000 up, the $150,000
winner.

### $1,500: trend-checked momentum top 3

| Strategy ($1,500, weekly) | Ended at | Per year | Worst drop | Beat SPY both periods |
|---|---:|---:|---:|---|
| **Momentum top 3, stocks + ETFs** (winner) | **$6,398** | **33.6%** | −38% | yes |
| Scorecard v2 top 2 | $6,307 | 33.2% | −45% | yes |
| Stock momentum top 3 | $6,232 | 32.9% | −41% | yes |
| ETF rotation top 2 (previous email strategy) | $3,507 | 18.6% | −19% | no (lagged 2011–2021) |
| SPY | $2,508 | 10.8% | −25% | |

**Up and down markets** (`regime_research.py`; trend = SPY above/below its 200-day
average). The winner beat SPY on both kinds of days in both periods:

| | Uptrend days | Downtrend days |
|---|---:|---:|
| 2021–2026: momentum vs SPY | +27%/yr vs +10%/yr | +58%/yr vs +14%/yr |
| 2011–2021: momentum vs SPY | +40%/yr vs +8%/yr | +54%/yr vs +42%/yr |

Moving to defensive assets or cash in downtrends did **worse** (−8% and −9%/yr on
2021–2026 downtrend days vs SPY's +14%): those days include the sharp rebounds. The
emails use the "absolute" rule — in a downtrend only names still rising over 12 months
are bought — which never had to bind historically but guards a broad crash.

### $25,000 and up (your dad's $1.5M): trend-checked scorecard v2 top 2

At $150,000 every stock is affordable, and holding fewer, more volatile names won:

| Strategy ($150,000, weekly) | Ended at | Per year | Worst drop | Beat SPY both periods |
|---|---:|---:|---:|---|
| **Scorecard v2 top 2** (winner) | **$687,531** | **34.5%** | −52% | yes |
| Stocks + ETFs momentum top 2 | $620,542 | 31.9% | −53% | yes |
| ETF rotation top 1 | $567,561 | 30.5% | −27% | no (lagged 2011–2021) |
| Stocks + ETFs momentum top 3 (the $1,500 winner) | $487,976 | 25.9% | −45% | yes |

Scorecard v2 = half 12-1 momentum rank, half 63-day volatility rank, over the 59
stocks; a name scoring under 25 on either is rejected and it needs 60+ overall. With
the same trend check at the full **$1.5M** (`regime_research.py`):

| | Ended at | Per year | Worst drop | Uptrend days | Downtrend days |
|---|---:|---:|---:|---:|---:|
| 2021–2026 | **$6,706,381** | **33.8%** | −52% (−$3.0M from the peak) | +21%/yr vs SPY +10% | +85%/yr vs SPY +14% |
| SPY | $2,508,112 | 10.8% | −25% | | |
| 2011–2021 (hindsight universe) | $164M | 54.9% | −48% | +54%/yr vs +8% | +58%/yr vs +42% |

By calendar year (2021–2026): +15%, −31%, +84%, +38%, −5%, +126%. Two stocks at about
$750,000 each is concentrated; lower `strategy_share` in `ACCOUNTS` to run it on less.

## Dad's daily plan (highest win rate, $1.5M)

Dip-buys + EPS picks + put spreads on each pick + QQQ put spreads and 0DTE condors,
all together on 2021–2026: $1.5M → **$4.08M (+22.2%/yr)**, worst drop −11%, **85% of
1,625 trades won** (S&P: $2.51M). Put spreads 8% below the price won 95–96%; the share
picks 56–68%. See [daily/RESULTS.md](daily/RESULTS.md).

## Win rates ("accuracy") — the 80% question

| Strategy | Trades won (2021–2026) | Return |
|---|---:|---|
| Momentum top 3 (most money) | 48–50% of positions | +33.6%/yr: a few big winners carry it |
| Dip-buying top-10 momentum names (`pullback.py`) | **70%** of 293 | +21.2%/yr, −18% worst drop |
| Buying 0DTE QQQ on volatile days | 40–47% | −24% to +397% total, depends on pricing |
| **Selling 0DTE QQQ iron condors on calm days** (`condor_research.py`, margin account) | **89–93%** | +0.4% to +21%/yr |

For shares, about 70% was the ceiling: 3 of 72 dip-buying variants hit 80% on
2011–2021 with ~25 trades each, and fell to 56–64% on 2021–2026. Adding a market
filter or quicker exits did not raise it (pass 2: 60–70%).

**Calm-day condors** (short strikes 1% from the open, 1% wings, the calmest 30% of
days by the volatile-day detector, at most 2% of $1.5M at risk per trade) — the most
accurate rule on 2005–2021 (87% won), tested once on 2021–2026:

| Pricing | Won | $1.5M became | Worst day |
|---|---:|---:|---:|
| mid | 91% | $2.48M (+10.6%/yr) | −$37,872 |
| vix | 93% | $3.97M (+21.5%/yr) | −$48,619 |
| fair (worst case for a seller) | 89% | $1.72M (+2.8%/yr) | −$31,080 |
| informed | 89% | $1.53M (+0.4%/yr) | −$29,659 |

The rule picked by "highest return with ≥ 80% wins" (0.5% strikes) fell to 69–75% wins
on 2021–2026, which is why the emails use the most-accurate rule instead.

## Options at $1.5M only (buying calls/puts, SPY, 0DTE to 2 months)

1,872 variants, 0.25–10% of the account per trade. The 2005–2021 pick (2-month at-the-
money calls, 10% per trade) turned $1.5M into **$1.59M** (+1.2%/yr) vs SPY's $2.51M.
At mid pricing 381 variants made money, 302 lost 90%+, 7 beat SPY (best +16%/yr).

## Half of profits into options ($1,500, `profit_sweep.py`)

| | 5 years | 10 years (hindsight before 2021) |
|---|---:|---:|
| Shares only | $6,398 | $37,177 |
| Half of monthly profits → 2-month SPY calls | $4,970 | $14,305 |
| Half → 0DTE QQQ on volatile days (mid pricing) | $4,970 | $12,323 |
| Half → 0DTE QQQ (informed pricing) | $4,970 | $297,239 (−61% drop on the way) |

In 5 years the options money never reached the price of one contract, so it sat in
cash. Over 10 years it lowered the result in three of four cases.

## 200% a year

$1,500 at 200%/yr is $362,000 after 5 years and $88.6 million after 10. Nothing tested
honestly came close: the best real result was 33.6%/yr (single best years +104% to
+126%). The 200%+ results only appeared when rules were tuned on the same years they
were scored on, and those rules lost 97–99.99% on other years.
