# Top 5 stocks a day (and calls, puts, shorts)

`python -m schwab_trader.top5_research` and `--combined`. Rules chosen on 2011-02 → 2021-10,
tested once on 2021-10 → 2026-10, S&P 500 stocks with point-in-time membership.

**The list.** Every evening the signals of the rules whose average 2011–2021 trade made
0.3%+ (the dip-buy rule and 7 chart-pattern buy setups) are ranked by that average, then
by how heavily the stock trades; the top 5 (at most 2 per rule, never a stock whose earlier
pick could still be open) are the day's buys. Each keeps its rule's limit sell and time
limit, plus a −15% stop.

| | Picks | Won | Avg a pick | Worst | On its own money |
|---|---:|---:|---:|---:|---:|
| 2011–2021 | 12,067 | 72% | +0.48% | −50% | +10.8%/yr (worst drop −27%) |
| 2021–2026 | 6,043 | 74% | +0.27% | −66% | +6.7%/yr (worst drop −22%) |

The worst picks gapped far past their stop overnight. Skipping stocks with earnings due
was tested and not adopted (2011–2021 average +0.46% vs +0.48%); the email flags them.

**Put spread on each pick** (sell a put 8% below, buy one 5% further down, ~1 week, buy
back at half the credit; 1% of the account at risk, at most 5 open): won 96% of 9,790 in
2011–2021 and 95% of 5,858 in 2021–2026, +1.6% / +1.9% of the amount at risk a trade at
mid prices (+0.8% / +0.9% at fair prices).

**Calls: no.** The best call rule on these picks (chosen on 2011–2021) lost 5% of its cost
a trade in 2011–2021 and 6% in 2021–2026 at mid option prices; calls only paid at "fair"
prices with no volatility premium, which real options rarely offer.

**Shorts: no.** See [../shorts/RESULTS.md](../shorts/RESULTS.md). Also tried and rejected:
a gradient-boosting model scoring every stock's next 5 days, its 5 biggest expected moves
as buys or shorts — right on direction 51–52% of the time, −7%/yr (2011–2021) and −12%/yr
(2021–2026) ([model_experiment.json](model_experiment.json)).

## Whole plan on $1.5M (mid option prices)

Half in the top-5 list (10 slots), half in EPS picks (5 slots), put spreads on top-5 and
EPS picks, QQQ put spreads (1% at risk each), calm-day 0DTE QQQ condors (2%).

| | Ended at | Per year | Worst drop | Trades won | S&P 500 |
|---|---:|---:|---:|---:|---:|
| 2021–2026 | **$3,310,566** | **+17.3%** | −14% | 85% of 13,280 | $2,508,112 (+10.8%) |
| 2011–2021 | $6,619,399 | +14.8% | −19% | 83% of 23,884 | $5,247,492 (+12.3%) |

2021–2026 profit by piece: QQQ condors +$0.98M, put spreads on top-5 picks +$0.52M,
top-5 shares +$0.29M, EPS put spreads +$0.04M, QQQ put spreads +$0.03M, EPS shares −$0.04M.
