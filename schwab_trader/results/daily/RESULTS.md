# Dad's daily plan: what was tested and how it did

`python -m schwab_trader.daily_research` (the original research) and
`python -m schwab_trader.daily_research --revalidate` (the same options rules and the
whole plan re-checked on the current stock list and the email's share rules). Same
protocol as the rest of the project: each rule was chosen on 2011-02 → 2021-10 by a rule
written before running, then tested once on 2021-10-25 → 2026-10-23.

**Stocks: every S&P 500, Nasdaq-100 and Dow 30 company (522), with point-in-time S&P
500 membership** (a stock is only bought on days it was in the index; see
`universe.py`). Companies that left the index and stopped trading aren't in the data,
so some survivorship bias remains. Option prices are modelled (no historical option
quotes were available), at three levels: **fair** (no volatility premium — the worst
case for a seller), **rich** (the premium VIX carries over SPY's realized moves, 1.34×)
and **mid** (halfway). Real bid/ask prices are now recorded every morning (journal).

## Whole plan on $1.5M (all of the below together, mid pricing)

| | Ended at | Per year | Worst drop | Trades won |
|---|---:|---:|---:|---:|
| 2021–2026 | **$3,273,101** | **+17.0%** | −15% | 89% of 2,586 |
| S&P 500 (SPY) | $2,508,112 | +10.8% | −25% | |
| 2011–2021 | $5,955,775 | +13.7% | −21% | 89% of 4,254 |

2021–2026 profit by piece: QQQ 0DTE condors +$0.98M, dip-buy shares +$0.51M, put
spreads on dip picks +$0.25M, put spreads on EPS picks +$0.04M, QQQ put spreads
+$0.03M, EPS shares −$0.04M.

The earlier version of this table ($4.08M, +22.2%/yr) used October 2021's 59 largest
companies — stocks known in 2021 to have done well, which flatters 2011–2021 and the
dip-buys. The numbers above are the honest replacement.

## The pieces (2021–2026 unless noted)

| Play | Rule | Trades won | Result |
|---|---|---:|---|
| Regular stock picks | buy 1–3 day dips (2-day RSI < 5) in the 25 strongest of the S&P 500 / Nasdaq-100 stocks + 17 ETFs above their 200-day average, skipping stocks with earnings due; +2% limit sell, −15% stop, else sell after 20 sessions; 3 slots | 85% of 607 (84% in 2011–2021) | +11.0%/yr on its half |
| EPS picks | after a report, buy the next open if the stock gapped up 4%+ and closed above its open, only while SPY is above its 200-day average; +2% / −15% / 20 sessions; 5 slots | 80% of 548 (83% in 2011–2021) | about break-even: avg +0.1% a trade, −1.1%/yr on its half |
| Put spreads on dip picks | sell a put 8% below, buy one 5% further down, ~1 week, buy back at 50% of the credit; 1% of the account at risk each | 94% of 600 (fair pricing 94%; 96% in 2011–2021) | worst −104% of the amount at risk |
| Put spreads on EPS picks | same, ~2 weeks | 94% of 538 (fair 93%) | |
| QQQ put spreads | when QQQ (above its 200-day average) has a sharp dip: sell a put 8% below, 2.5% wide, ~1 week | 100% of 50 | small: few signals |
| QQQ 0DTE iron condors | calm days only (volatile-day detector), 1% strikes, 2% at risk | 89–93% | see `results/condor` |

From the original research on the 59 largest stocks: EPS beats alone barely help:
positions after beats won 61%, after misses 58%. No EPS
share rule reached 70% in both periods (`eps_grid.csv`, 240 variants). The high win
rates come from **selling** put spreads far below the price: they win most weeks and
lose the whole spread in a sharp drop (the worst trades lost about 100% of what was at
risk), which is why each one is capped at 1% of the account.

Earnings dates: Robinhood's calendar has EPS estimates/actuals from October 2017
(17,641 reports for the 522 stocks); earlier dates were inferred from trading volume and
matched the real report day 74% of the time (80% for the 59 largest).

## Asia → US (US-listed Asian stock funds, their opening gap = the overnight session)

| | Moves at the US open with Asia (beta) | Correlation | Day kept going Asia's way after a 1%+ Asian move |
|---|---:|---:|---:|
| SPY | 0.47 | 0.68 | 51% |
| QQQ | 0.63 | 0.69 | 52% |
| Technology (XLK) | 0.72 | 0.68 | 53% |
| Utilities / staples | 0.08–0.15 | 0.18–0.28 | 49–50% |

Asia tells you where US stocks open — technology and consumer stocks most — but not
what they do after the open.

## Pattern memory

Chart patterns across the 59 stocks, buying the next open and holding 5 days
(`patterns_test.csv`; base rate = any stock, any day: 52% in 2021–2026):

| Pattern | Rose over 5 days, 2021–2026 | 2011–2021 |
|---|---:|---:|
| volume-surge down day | 57% | 56% |
| 5 down days in a row | 56% | 56% |
| oversold in a downtrend (RSI2 < 5) | 54% | 57% |
| sharp dip in an uptrend | 54% | 56% |
| new 52-week high | 51% | 53% |

The "most similar past mornings" forecaster (trend, momentum, VIX, Asia) had no edge on
SPY/QQQ's same-day direction (right 46–48% of its confident days) but some on SPY's
next 5 days: right 62% of confident days vs 58% for always guessing "up". The email
shows only the 5-day forecast.
