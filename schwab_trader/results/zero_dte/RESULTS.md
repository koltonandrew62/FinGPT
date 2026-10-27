# Volatile-day detector and 0DTE QQQ options – results

## The detector works

Features known at 9:30 AM ET (VIX level and overnight change, QQQ's gap, yesterday's move
and range, ATR, realized volatility, day of week, jobs-report Fridays; the "complex" set
adds Bollinger width, RSI, MACD, moving-average distances, streaks, 52-week high/low
distance, volume, VIX trend, 5-day return, QQQ vs SPY). Target: one of the biggest 20% of
open-to-close moves. Trained on 2005–2021 with walk-forward predictions, graded on
2021-10 → 2026-10, which it never saw.

| | AUC 2005–2021 (walk-forward) | AUC 2021–2026 | Avg move, flagged vs. other days (2021–2026) |
|---|---:|---:|---|
| Simple (logistic regression) | 0.76 | 0.72 | 1.42% vs. 0.75% |
| Complex (gradient boosting) | 0.74 | 0.74 | 1.34% vs. 0.69% |

52% of flagged days were big-move days, vs. a 20% base rate.

## Trading 0DTE QQQ on flagged days mostly doesn't pay

Buy at the open, sell at the close (modelled premiums; Schwab $0.65/contract and bid/ask
on every leg). Simulated at $1.5M so whole-contract rounding doesn't decide anything;
dollar figures are scaled to a $1,500 start. Pricing levels: **fair** (no volatility
premium), **mid** (used to choose), **vix** (QQQ implied vol at VIX's usual premium),
**informed** (fair, but each morning's price already reflects what the simple model
knows, as real 0DTE prices do for CPI/Fed/jobs days).

**2005–2021 (choosing):** 120 rules (model × share of days flagged × straddle / strangles /
follow-the-gap / fade-the-gap × 2–25% per trade). **None made money at mid pricing; 111
lost 90% or more.** The least-bad rule that traded: complex model, top 10% of days, buy
one at-the-money option in the direction of the opening gap, 2% of the account per trade.

| That rule | 2005–2021 | 2021–2026 (one test) | 2021–2026, same trades on 200 random sets of days (median) |
|---|---:|---:|---:|
| fair | $3,726 (+5.6%/yr) | **$7,452 (+37.9%/yr)** | $1,567 |
| informed | $3,089 (+4.4%/yr) | $5,250 (+28.5%/yr) | $1,641 |
| mid | $641 (−5.0%/yr) | $2,348 (+9.4%/yr) | $700 |
| vix | $216 (−10.9%/yr) | $1,137 (−5.4%/yr) | $418 |

It beat all 200 random-day runs at every pricing level, so the detector adds real
value. Whether it makes money depends on what real 0DTE options cost: positive if they
are priced near fair, negative near VIX levels, and it lost money over 2005–2021 at mid.
Treat it as unproven.

**Mixing it into the ETF rotation (2021–2026, mid pricing):** 100% ETF $3,511 (18.6%/yr,
worst drop −19.3%); 90/10 $3,395 (17.8%, −14.6%); 75/25 $3,220 (16.5%, −12.1%). Lower
return, smaller drops.

**Account size:** a 2% cap on $1,500 is $30, which rarely buys one QQQ 0DTE contract. The
default cap for the $1.5M account is 0.25% ($3,750) per trade.

## The "tune on the 195% window for 200%+" experiment

Searched 2,880 combinations on 2021–2026 itself at fair pricing (15 technical rules
alone and in pairs: gap size, VIX level and jump, yesterday's move, jobs Friday, RSI
extreme, below 50-day average, wide Bollinger bands, down streaks, near 52-week low,
negative MACD, top-10% detector days × 6 option structures × 5–50% of the account).

- **118 combinations beat 200% a year** on that window. The best (VIX > 20 and a
  complex-model top-10% day, 0.5%-out-of-the-money strangles, 50% of the account per
  trade) turned $1,500 into **$60.6 billion** on paper.
- Same rule, same years, at mid pricing: **$0.28**. At informed pricing: $4.27M.
- Same rule on 2005–2021: **lost 99.99%** at fair, mid and informed pricing alike.

Picking the best of thousands of rules on the period you then report is curve-fitting:
the 200%+ comes from a handful of days that a search happened to line up with, and it
does not repeat.

Reproduce: `python -m schwab_trader.zero_dte_research`. Daily alert:
`python -m schwab_trader.vol_alert` (uses `model.joblib`, retrain with `--save-model`).
