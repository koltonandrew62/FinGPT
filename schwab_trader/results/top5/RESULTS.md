# Top 5 stocks a day (and calls, puts, shorts)

`python -m schwab_trader.top5_research` and `--combined`. Rules chosen on 2011-02 → 2021-10,
tested once on 2021-10 → 2026-10, S&P 500 stocks with point-in-time membership.

**The list.** Every evening the signals of the rules whose average 2011–2021 trade made
0.3%+ (the dip-buy rule and 7 chart-pattern buy setups) are ranked by that average, then
by how heavily the stock trades; the top 5 (at most 2 per rule, never a stock whose earlier
pick could still be open) are the day's buys. Each keeps its rule's limit sell and time
limit, plus a −15% stop, and **is sold at the close before an earnings report** (chosen on
2011–2021; see below).

| | Picks | Won | Avg a pick | Worst | On its own money |
|---|---:|---:|---:|---:|---:|
| 2011–2021 | 12,067 | 70% | +0.45% | −50% | +11.5%/yr (worst drop −26%) |
| 2021–2026 | 6,048 | 71% | +0.26% | −66% | +8.6%/yr (worst drop −22%) |

The worst picks gapped far past their stop overnight (not on earnings).

**Earnings.** Three ways were tested. Holding through the report: 72% / 74% won, +0.48% /
+0.27% a pick, +10.8% / +6.1% a year on its own money (2011–2021 / 2021–2026). Selling at
the close before the report: a little lower average pick, but the money is free for the
next signal sooner — +11.5% / +8.6% a year; chosen on the 2011–2021 +11.5% vs +10.8%.
Not buying stocks that report during the trade: same average pick in 2011–2021 (+0.46% vs
+0.48%) and rejected; it would also have made the whole plan worse in 2021–2026 (+14.9%/yr,
worst drop −19%).

**Put spread on each pick** (sell a put 8% below, buy one 5% further down, ~1 week, buy
back at half the credit; 1% of the account at risk, at most 5 open), **none when the stock
reports before the spread expires**: won 97% of 8,997 in 2011–2021 and 95% of 5,470 in
2021–2026, +1.9% / +2.0% of the amount at risk a trade at mid prices (+1.1% / +1.1% at
fair prices). Spreads held through a report won 91% of 793 / 91% of 398 but **lost 1.6% /
0.8%** of the risk a trade — the few losses are big.

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
| 2021–2026 | **$3,493,891** | **+18.5%** | −14% | 83% of 12,898 | $2,508,112 (+10.8%) |
| 2011–2021 | $6,962,592 | +15.4% | −23% | 82% of 23,091 | $5,247,492 (+12.3%) |

2021–2026 profit by piece: QQQ condors +$0.98M, put spreads on top-5 picks +$0.56M,
top-5 shares +$0.38M, EPS put spreads +$0.04M, QQQ put spreads +$0.03M, EPS shares +$0.01M.
(Before the earnings rules below: +17.3%/yr and −14% in 2021–2026, +14.8%/yr and −19% in
2011–2021.)

## Fixes after the first live week (Oct 28–30, 2026)

- **Option contracts that didn't exist** (JCI, PNR, FLEX, GEN, TXT on Nov 13; RDDT's $117.50
  strike). The morning run now looks up each stock's listed expiries (`get_option_chains`)
  and uses the listed one closest to the tested holding time (from 2 sessions shorter to
  twice as long), or skips the spread. The 9:40 check tries the email's strikes and the
  wider standard steps ($2.50 / $5 / $10), re-prices whichever is listed, and says which
  contracts to trade.
- **Put spreads held through earnings** (RDDT, MSTR): skipped now (above).
- **Shares held through earnings** (RDDT fell 21% after its report, through its stop):
  top-5 picks are sold at the close before a report from Nov 2 on (above). Earlier picks
  keep the rule their email gave.
- **Spreads never placed counted as open**: a spread that isn't listed, or whose credit at
  the open is under the backtest's floor, is now marked skipped in the journal. Before,
  those blocked new spreads ("5 already open") and sat in the track record.
- **The 9:40 check waited for VIX**: Robinhood shows a 30-minute bar only once it ends, so
  the 9:30 VIX bar appeared at 10:00. The check now takes the 9:30 value from the bar that
  ends at 9:30.
- **Stops gapped through before the open** (RDDT at $102.50 vs a $114.73 stop): the email
  now lists such a position as SELL at the open instead of HOLD.
- **EPS picks after a miss** (CVNA [M]): requiring a beat was tested and made the rule worse
  in 2011–2021 (79% won, +0.35% a trade vs 83%, +0.70%), so a big gap up after the report
  still counts.
