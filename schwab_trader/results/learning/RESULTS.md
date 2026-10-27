# Learning from mistakes

`python -m schwab_trader.learning` (the daily email re-runs the same learning every
morning on all data to date, including trades that finished since).

Every past dip-buy and EPS trade of the email's rules is tagged with what was true when
it was bought (S&P 500 below its 200-day average, VIX above 25, VIX up 20%+ in 5 days,
the stock gapping down 2%+ at the open, Asia down 1%+ overnight, the stock's sector fund
below its 50-day average, a broad sell-off, a stock swinging more than 45% a year, an 8%+
drop in 3 days, earnings during the trade). A condition becomes an "avoid" rule only if
trades bought under it won at least 7 points less often and made less on average, with
40+ cases, in both the older and the newer half of the history.

The learning was tested year by year (each year's rules learned only from the years
before it, then applied to that year):

| Rule | Trades 2014–2026 | Won, no learning | Won, with learning | Avg trade, no learning | with learning |
|---|---:|---:|---:|---:|---:|
| Dip-buys | 1,483 | 84.2% | 84.2% | +0.42% | +0.42% |
| EPS picks | 1,003 | 80.9% | 80.8% | +0.38% | +0.37% |

Today no condition passes the bar for either rule (the rules already avoid earnings and,
for EPS picks, down-trending markets). The only rule ever learned (EPS picks during a
broad sell-off, learned for 2025) made 2025 slightly worse, so learned rules are only
applied live when this year-by-year test shows they helped. The second loop - halving a
rule's size when its real picks win significantly less often than its backtest - starts
working once 10 picks have closed (journal.py).
