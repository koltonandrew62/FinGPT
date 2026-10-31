# Crashes, the crash shield and bubble gauges

`python -m schwab_trader.crash_research`. No forecasts: everything below uses only prices that had already
happened on each day. Data: the S&P 500 (SPX) and the Nasdaq 100 (NDX, the index behind QQQ) every day since
1993 (Robinhood index history), so the dot-com bubble and bust — the closest past case to an AI bubble — and
2008 are included. The plan's own stock data starts in 2010, so the plan itself is tested on 2011–2026.

## Every 15%+ crash since 1993

| Index | Peak | Low | Drop | Holding the index only while the shield is off |
|---|---|---|---:|---:|
| Nasdaq 100 | 1994-03-17 | 1994-06-24 | −16% | −15% |
| Nasdaq 100 | 1997-01-22 | 1997-04-02 | −15% | −15% |
| Nasdaq 100 | 1997-10-09 | 1997-12-24 | −18% | −18% |
| Nasdaq 100 | 1998-07-20 | 1998-10-08 | −23% | −6% |
| Nasdaq 100 | 2000-03-27 | 2002-10-07 | −83% | −30% |
| Nasdaq 100 | 2015-11-03 | 2016-02-09 | −16% | −7% |
| Nasdaq 100 | 2018-08-29 | 2018-12-24 | −23% | −14% |
| Nasdaq 100 | 2020-02-19 | 2020-03-20 | −28% | −28% |
| Nasdaq 100 | 2021-11-19 | 2022-12-28 | −36% | −21% |
| Nasdaq 100 | 2025-02-19 | 2025-04-08 | −23% | −23% |
| S&P 500 | 1998-07-17 | 1998-08-31 | −19% | −19% |
| S&P 500 | 2000-03-24 | 2002-10-09 | −49% | −7% |
| S&P 500 | 2007-10-09 | 2009-03-09 | −57% | −5% |
| S&P 500 | 2018-09-20 | 2018-12-24 | −20% | −10% |
| S&P 500 | 2020-02-19 | 2020-03-23 | −34% | −34% |
| S&P 500 | 2022-01-03 | 2022-10-12 | −25% | −13% |
| S&P 500 | 2025-02-19 | 2025-04-08 | −19% | −19% |

## The crash shield

Chosen on 1993–2010 only (the dot-com bust, 2008 and everything between), from the fixed list below, as the rule
with the best return per unit of worst drop (CAGR ÷ max drawdown, averaged over the two indexes) holding the
index while the rule is off and cash while it is on, with at most 6 switches a year (0.1% a switch).

| Rule | 1993–2010: S&P | Nasdaq 100 | 2011–2026: S&P | Nasdaq 100 | Switches/yr |
|---|---:|---:|---:|---:|---:|
| none (always in the index) | +6.0%/yr, −57% | +10.7%/yr, −83% | +12.1%/yr, −34% | +17.6%/yr, −36% | 0.0 |
| S&P 500 below its 200-day average | +5.3%/yr, −31% | +11.3%/yr, −57% | +6.9%/yr, −21% | +11.4%/yr, −27% | 7.4 |
| Nasdaq 100 below its 200-day average | +6.9%/yr, −26% | +11.4%/yr, −53% | +7.6%/yr, −24% | +12.9%/yr, −23% | 7.3 |
| either index below its 200-day average | +5.9%/yr, −24% | +11.4%/yr, −48% | +6.4%/yr, −20% | +10.9%/yr, −22% | 7.7 |
| S&P 500 below its 200-day average 3 days running (off after 3 days above) | +6.2%/yr, −20% | +13.7%/yr, −36% | +7.6%/yr, −25% | +12.2%/yr, −31% | 2.7 |
| Nasdaq 100 below its 200-day average 3 days running (off after 3 days above) | +7.3%/yr, −23% | +13.1%/yr, −47% | +7.6%/yr, −27% | +12.4%/yr, −27% | 2.9 |
| S&P 500 50-day average below its 200-day | +8.7%/yr, −19% | +14.8%/yr, −36% | +7.7%/yr, −34% | +11.6%/yr, −28% | 0.9 |
| S&P 500 10%+ below its 1-year high | +6.5%/yr, −28% | +11.6%/yr, −59% | +8.0%/yr, −21% | +12.2%/yr, −30% | 3.2 |
| Nasdaq 100 15%+ below its 1-year high | +7.5%/yr, −25% | +14.9%/yr, −31% | +10.5%/yr, −26% | +15.0%/yr, −26% | 3.0 |

(Each cell: yearly return, worst drop.) **Chosen: S&P 500 50-day average below its 200-day.** In 1993–2010 it kept the
Nasdaq 100's 2000–2002 fall to −30% (index −83%) and the S&P 500's 2007–2009 fall to −5% (index −57%).
In 2011–2026 the crashes were fast (2020, 2025): it turned on near the bottom and missed the rebound, so the
same rule returned less than holding (S&P +7.7%/yr vs +12.1%, Nasdaq 100 +11.6% vs +17.6%).

## The whole plan through the 2011–2026 crashes

| S&P 500 drop | S&P 500 | Nasdaq 100 | Plan | Plan's worst drop | Plan with the shield | Its worst drop |
|---|---:|---:|---:|---:|---:|---:|
| 2011-04-29 → 2011-10-03 | −19% | −13% | −21.1% | −22.6% | −16.1% | −20.9% |
| 2015-05-21 → 2016-02-11 | −14% | −13% | +1.3% | −8.7% | −3.8% | −7.6% |
| 2018-01-26 → 2018-02-08 | −10% | −10% | −5.0% | −5.0% | −5.1% | −5.2% |
| 2018-09-20 → 2018-12-24 | −20% | −22% | −9.8% | −9.8% | −2.3% | −3.6% |
| 2020-02-19 → 2020-03-23 | −34% | −28% | −12.1% | −12.2% | −10.9% | −11.1% |
| 2022-01-03 → 2022-10-12 | −25% | −35% | −8.2% | −13.6% | −6.1% | −10.7% |
| 2025-02-19 → 2025-04-08 | −19% | −23% | −11.7% | −11.7% | −9.2% | −9.2% |

| Whole plan | 2011–2021 | 2021–2026 |
|---|---:|---:|
| without the shield | +15.4%/yr, worst drop −23% | +18.5%/yr, worst drop −14% |
| with the shield | +12.6%/yr, worst drop −21% | +14.3%/yr, worst drop −11% |

The shield was to go into the plan only if, on 2011–2021, it cut the worst drop by 2+ points at a cost of at
most 0.5%/yr, or raised the yearly return (decided before the run). It cut the worst drop by 2 points but cost
2.8 points a year, so **the plan doesn't trade on it**. The email shows its state every day,
and `SHIELD_ACTS = True` in `daily_report.py` makes the plan follow it (no new top-5 buys, EPS buys or options
plays while it is on).

What the test can't show: the plan's stocks have no data before 2010, so the plan itself has never been run
through a slow, multi-year bust like 2000–2002 — the case where the shield helped most. Its quick crashes were
all V-shaped.

## Bubble gauges (the email shows these daily)

As of 2026-10-29:

- Nasdaq 100 5.3% above its 200-day average — higher than 41% of days since 1993. At the March 2000 top: 59% above.
- 3-year rise: +99% (into March 2000: +426%). Nasdaq 100 vs the S&P 500 over 3 years: +10% (March 2000: +202%).
- After the most stretched 10% of days since 1993, the Nasdaq 100 was higher 12 months later 87% of the time (median +22%, worst −69%); after any day 82% (median +19%). Being stretched has not, by itself, meant a fall was next — the worst cases came from 2000.
