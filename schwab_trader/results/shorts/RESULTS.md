# Short sales and bearish options

`python -m schwab_trader.short_research`. Rules chosen on 2011-02 → 2021-10 (70%+ of trades
won, no trade worse than −20%, best average), tested once on 2021-10 → 2026-10, S&P 500
stocks with point-in-time membership. Costs: 5 bps a side plus 2.5%/yr for borrowing the
shares and the dividends a short seller pays.

| Rule | 2011–2021 | 2021–2026 |
|---|---|---|
| Short a weak stock (below its 200-day average, among the 25 weakest) after a sharp bounce (2-day RSI > 90), only while SPY is below its 200-day average, no earnings due; cover at −3%, buy-stop +10%, 20 sessions | won 75% of 354, avg +0.19% | won 74% of 262, avg −0.04% |
| Short after an earnings gap down of 6%+ that closed below its open; cover at −3%, buy-stop +15%, 20 sessions | won 71% of 331, avg +0.20% | won 70% of 375, avg −0.73% |
| Sell bear call spreads on those signals (8% above, 5% wide, ~1 week) | 87–91% won, small gains at mid prices | 88–89% won, −1.3% to −2.3% of the risk a trade |
| Buy puts on those signals | 37–47% won, losses | 42–52% won, losses |

Shorts win often and small, and lose rarely but big; in a market that rises over time that
doesn't pay. The email lists the short-rule signals as "short ideas" with this record, and
the top-5 list has no shorts.
