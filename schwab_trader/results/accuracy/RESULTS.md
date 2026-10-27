# Accuracy of each part of the daily plan email

`python -m schwab_trader.accuracy_research`. Every rule was chosen on 2011-02 → 2021-10
and tested once on 2021-10-25 → 2026-10-23.

**Stocks: every S&P 500, Nasdaq-100 and Dow 30 company (522 with price history),
using point-in-time S&P 500 membership**: a stock can only be picked on days it was in
the index (297 such stocks in 2011, 503 now). Companies that left the index and no longer
trade have no price history here, so some survivorship bias remains (smaller than with
the earlier list of October 2021's 59 largest companies, which had hindsight built in).

Trade rules: among variants that won **at least 70%** of design trades **and never lost
more than 20% on one design trade**, the one with the best average design trade. Tested:
profit targets +1/2/3%/none, stops none/5/8/10/15%, 5/10/20 sessions, how many of the
strongest stocks to consider (10/25/50), an S&P 500 trend filter (SPY above its 200-day
average) and skipping trades with an earnings report inside the holding period.

| Part of the email | Rule now used | 2011–2021 | 2021–2026 | Notes |
|---|---|---:|---:|---|
| Regular stock picks | dip (2-day RSI < 5) in the 25 strongest stocks/ETFs; skip if earnings are due during the trade; +2% limit, −15% stop, else sell at the close of session 20 | 84% of 1,206 | **85% of 607** | avg +0.3% a trade, worst −17%; +11.0%/yr on its half (+11.1% in 2011–2021) |
| EPS picks | next open after a report day that gapped up 4%+ and closed above its open, only while SPY is above its 200-day average; +2% / −15% / 20 sessions | 83% of 600 | **80% of 548** | avg +0.1% a trade, worst −16%: **about break-even in 2021–2026** (−1.1%/yr on its half; +6.9%/yr in 2011–2021) |
| Chart patterns | each pattern as a trade: +3% (+2% for crosses and 52-week highs) / 20 sessions | 72–81% | **71–81%** | the email leaves out patterns that only broke even (golden cross, 52-week high) |
| Asia → US open | when Asian funds move 1%+: the US open goes the same way | 78–96% | **S&P 91%, Nasdaq 93%, tech 91%** | staples/energy/health care/utilities 60–69% in 2021–2026 |
| Earnings beats | beat in each of the last 4 quarters → "likely beat" | 86% | **88%** | 49% of reports; every report beat 79% of the time |
| Gaps | 2–4% gap without earnings news stays open that day | 72% | **72%** | gaps under 1% close 63% of the time |
| Market direction | 100 most similar past mornings, SPY over 10 sessions, when ≥65% agree | 71% | 61% | always "up": 60% — no real edge out of sample |
| Put spreads on picks, QQQ | unchanged rules, re-checked on the new picks | 96% / 96% / 100% | 94% / 94% / 100% | from `results/daily` |

Two parts can't honestly reach 70%: the market's direction over the next days and weeks
is barely more predictable than always saying "up". The email shows their real track
record next to the "always up" number instead of a made-up confidence.

The stops and the earnings filter cut the worst dip-buy from −31% (59 stocks, no stop)
to −17%, at the cost of a smaller average trade.
