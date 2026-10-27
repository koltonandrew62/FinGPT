# Dad's daily plan: what was tested and how it did

`python -m schwab_trader.daily_research` (about 25 minutes). Same protocol as the rest
of the project: each rule was chosen on 2011-02 → 2021-10 by a rule written before
running, then tested once on 2021-10-25 → 2026-10-23. The stocks are October 2021's 59
largest US companies, so 2011–2021 has hindsight; 2021–2026 does not. Option prices
are modelled (no historical option quotes were available), at three levels:
**fair** (no volatility premium — the worst case for a seller), **rich** (the premium
VIX carries over SPY's realized moves, 1.34×) and **mid** (halfway).

## Whole plan on $1.5M (all of the below together, mid pricing)

| | Ended at | Per year | Worst drop | Trades won |
|---|---:|---:|---:|---:|
| 2021–2026 | **$4,076,380** | **+22.2%** | −11% | 85% of 1,625 |
| S&P 500 (SPY) | $2,508,112 | +10.8% | −25% | |
| 2011–2021 | $9,240,776 | +18.5% | −17% | 85% of 3,312 |

2021–2026 profit by piece: dip-buy shares +$1.05M, QQQ 0DTE condors +$0.98M, put
spreads on dip picks +$0.28M, EPS shares +$0.21M, put spreads on EPS picks +$0.03M,
QQQ put spreads +$0.03M.

## The pieces (2021–2026 unless noted)

| Play | Rule | Trades won | Result |
|---|---|---:|---|
| Regular stock picks | buy 1–3 day dips (2-day RSI < 10) in the 10 strongest of 59 stocks + 17 ETFs above their 200-day average; sell when RSI > 70 or after 20 sessions; 3 slots | 68% of 359 (70% in 2011–2021) | +19.2%/yr on its half |
| EPS picks | after a report, buy the next open if the stock gapped up 2%+ and closed above its open; hold 20 sessions; 5 slots | 56% of 174 (63% of 353 in 2011–2021) | +2.2% a trade; +5.2%/yr on its half |
| Put spreads on dip picks | sell a put 8% below, buy one 5% further down, ~1 week, buy back at 50% of the credit; 1% of the account at risk each | 96% of 627 (fair pricing 96%) | +1.6% to +3.5% of the amount at risk a trade; worst −104% |
| Put spreads on EPS picks | same, ~2 weeks | 95% of 172 (fair 94%) | +0.2% to +2.1% a trade |
| QQQ put spreads | when QQQ (above its 200-day average) has a sharp dip: sell a put 8% below, 2.5% wide, ~1 week | 100% of 50 (and of ~75 in 2011–2021) | small: few signals |
| QQQ 0DTE iron condors | calm days only (volatile-day detector), 1% strikes, 2% at risk | 89–93% | see `results/condor` |

EPS beats alone barely help: positions after beats won 61%, after misses 58%. No EPS
share rule reached 70% in both periods (`eps_grid.csv`, 240 variants). The high win
rates come from **selling** put spreads far below the price: they win most weeks and
lose the whole spread in a sharp drop (the worst trades lost about 100% of what was at
risk), which is why each one is capped at 1% of the account.

Earnings dates: Robinhood's calendar has EPS estimates/actuals from October 2017
(2,029 reports for the 59 stocks); earlier dates were inferred from trading volume and
matched the real report day 80% of the time.

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
