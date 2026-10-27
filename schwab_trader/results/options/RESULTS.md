# Options study: buying SPY calls and puts with $1,500

**Bottom line: no options strategy tested came close to 200% a year, and most of them
destroyed the account. Same-day (0DTE) options were the worst group.**

A $1,500 account is below Schwab's $2,000 margin minimum, so it can only *buy* calls
and puts (no spreads, no selling). 1,248 variants were tested: always-calls, trend
(SPY vs. its 50-day average) or reversal (fade yesterday's move); daily or weekly
entries; expiries of 0, 1, 5, 21 and 42 trading days; at-the-money to 2% out of the
money; 5–50% of the account per trade; with or without a +100% / −50% exit. Costs:
$0.65 per contract each way (Schwab) plus the bid/ask spread, max($0.01, 1.5% of price).

**Prices are modelled, not historical quotes** (expired-contract prices were not
available): Black-Scholes with a skew, at-the-money volatility = VIX × a multiplier.
SPY's actual move from the open to the exit close averaged only **62%** of what VIX
implied for same-day trades and 70–82% for 1–42 days (measured on 2005–2021). So three
pricing levels were run:

- **fair**: volatility = what actually happened. Buyers pay no volatility premium; real
  markets are never this generous, so treat it as the best case.
- **vix**: VIX as is.
- **mid**: halfway; used to pick the strategy.

## Design period (2005-02 → 2021-10, mid pricing)

Only **28 of 1,248** variants made money; **411 lost 90% or more**. The highest return
(the pick, by the same max-return rule as the ETF study): weekly, buy an at-the-money
call ~2 months out with 25% of the account, sell at +100% / −50% or after 4 weeks:
$1,500 → $18,117 (16%/yr) with a 62% drawdown.

## Last 5 years (2021-10 → 2026-10), run once

**The pick could not trade.** With SPY at $450–760, one 2-month at-the-money call costs
~$1,500–3,000, so 25% of a $1,500 account never covers a single contract. It back-tested
well in 2005–2021 only because SPY was ~$120 then.

All 1,248 variants on the last 5 years (SPY: $1,500 → $2,509, 10.8%/yr):

| Pricing | Made money | Beat SPY | Lost ≥ 90% | Best / yr | ≥ 200% / yr |
|---|---:|---:|---:|---:|---:|
| vix | 25 | 3 | 136 | 20% | **0** |
| mid | 25 | 2 | 160 | 29% | **0** |
| fair (best case) | 45 | 13 | 181 | 195% | **0** |

**0DTE only** (96 variants per pricing level): under vix and mid pricing **none made
money**; 51–53 lost ≥ 90%; the median account ended at **$116–$132**. Under fair pricing,
5 of 96 made money and 58 lost ≥ 90%.

The 195% "fair" result was 2%-out-of-the-money 0DTE calls that traded only 26 times in
5 years (most days the option was too cheap to bother); 5 trades won. Nearly all of the
gain came from three huge intraday reversals: Feb 24, 2022 (+4.2% open to close),
Oct 13, 2022 (+4.8%) and Apr 9, 2025, the tariff-pause day (+11.2%), when one trade
turned $4,731 into $359,434. The same rule lost 97% over 2005–2021 at mid pricing. It is
a lottery ticket, not a strategy.

The only family that held up was 2-month calls (always-calls or trend signal; best: 20–29%
a year in the test with −67% to −73% drawdowns), and with $1,500 most of those contracts
are unaffordable at today's SPY price.

Resampling the pick's own 2005–2021 daily returns into 2,000 five-year paths gave a
median of $2,960, a 21% chance of losing money, and a **0% chance of 200%/yr** (or
1,000%/yr).

## Why

Option buyers pay a volatility premium: VIX has usually run above the moves that
followed. On top of that, spreads and per-contract fees take a large bite out of cheap
short-dated options, and most of them expire worthless (buying at-the-money 0DTE calls
every day, the median trade lost 100%).
200–1,000% a year needs a few enormous wins in a row with no wipe-out in between, which
the data say is far less likely than losing the account.

Reproduce: `python -m schwab_trader.options_backtest --phase design` then `--phase test`.
Raw grids: `design_grid.csv`, `test_grid.csv`; summary: `test_summary.json`.
