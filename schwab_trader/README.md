# schwab_trader

A weekly, rules-based share strategy for a Charles Schwab account, with backtests, a
weekly 0-100 stock scorecard, a volatile-day detector for 0DTE
QQQ options, a Sunday-evening email sized for two accounts ($1,500 and ~$1.5M), and a
Schwab order bot that runs in dry-run mode until you say otherwise.

**The strategies (details in [results/STRATEGY_RESULTS.md](results/STRATEGY_RESULTS.md)).**
Each account runs the share strategy that grew money fastest at its size, checking the
market trend first: in a downtrend (S&P 500 below its 200-day average) only names still
rising over 12 months are bought.

| Account size | Every week, hold | Last 5 years | S&P 500 | Worst drop |
|---|---|---:|---:|---:|
| under $25,000 | the 3 strongest of 59 large US stocks + 17 ETFs by 12-month return (skipping the latest month) that the account can afford | $1,500 → **$6,398** (33.6%/yr) | $2,508 | −38% |
| $25,000 and up | the top 2 of 59 stocks by scorecard v2 (half momentum, half volatility) | $1.5M → **$6.71M** (33.8%/yr) | $2.51M | −52% (−$3.0M from the peak) |

Both beat the S&P on uptrend and downtrend days. Only about half of positions close at a
profit, and the drops are large. Nothing tested reached 200%/yr; the best honest single
years were +104% to +126%.

For the $1.5M margin account there is also a high-accuracy options rule: sell a 0DTE
QQQ iron condor on days the volatility detector rates calm (won 89–93% of trades in
2021–2026; +0.4% to +21%/yr depending on real option prices).

## What it does

Every week (Friday close → Monday 9:35 AM ET):
1. Checks the market trend (SPY vs. its 200-day average).
2. Ranks the names for each account's strategy (above) and holds the top 3 (under
   $25k) or top 2 ($25k and up) the account can afford, in equal dollar amounts, whole
   shares only, no margin.
3. Emails the picks, shares and dollars for each account, the next morning's 0DTE plan
   for accounts that trade options, and earnings beats/misses.

| File | Purpose |
|---|---|
| `active_strategy.py` | **The strategies in use**: trend-checked momentum top 3 (under $25k), scorecard v2 top 2 ($25k and up) |
| `share_competition.py`, `regime_research.py` | How it was chosen; up/downtrend test |
| `pullback.py`, `condor_research.py` | High-win-rate alternatives (dip-buying 70%; calm-day condors 89–93%) |
| `profit_sweep.py` | Half of profits moved into options |
| `strategy.py` | ETF rotation signal + whole-share sizing |
| `backtest.py`, `research.py` | Backtest engine and the design/test/robust protocol |
| `weekly_report.py`, `notify.py` | Weekly picks with buy/sell dates & times, sized per account → email |
| `sizing.py` | How much to buy for each account in `ACCOUNTS` |
| `daily_report.py`, `daily_research.py` | Dad's daily plan email and its research ([results](results/daily/RESULTS.md)) |
| `patterns.py` | Trends, chart patterns, "similar past mornings", Asia → US impact |
| `scorecard.py`, `scorecard_research.py` | Weekly 0-100 stock score, reject rule, put/avoid list ([results](results/scorecard/RESULTS.md)) |
| `earnings.py`, `earnings_backtest.py` | Earnings play book: 8 quarters of SEC filings, cash-flow-led score, drift backtest |
| `vol_model.py`, `zero_dte_research.py`, `vol_alert.py` | Volatile-day detector, 0DTE QQQ study, morning alert ([results](results/zero_dte/RESULTS.md)) |
| `options_backtest.py` | SPY calls/puts from 0DTE to 2 months ([results](results/options/RESULTS.md)) |
| `import_robinhood.py` | Load saved Robinhood price results into `data/` (`--source local`) |
| `live_trader.py` | Places the strategy's trades on Schwab (dry run by default) |
| `calendar_utils.py` | NYSE holidays, so dates/times in the report are real sessions |

## Install

```bash
pip install pandas numpy yfinance schwab-py matplotlib requests
python -m schwab_trader.data                 # download ETF history (Yahoo, free)
python -m schwab_trader.backtest             # 5-year backtest vs SPY
python -m pytest tests/test_schwab_trader.py
```

## Weekly email

**Already set up:** a scheduled Claude task runs every **Sunday at ~6:30 PM ET**. It pulls
prices and the earnings calendar from the connected Robinhood account (read-only), runs
`weekly_report --source local --earnings-json ...` and emails the result through the
connected Gmail. Manage or stop it under scheduled tasks in the Claude app.

To run it yourself instead (e.g. from your own computer with cron):

```bash
export SMTP_USER=you@gmail.com SMTP_PASSWORD='<gmail app password>' EMAIL_TO=you@gmail.com
export SEC_USER_AGENT="Your Name you@gmail.com"     # SEC requires a contact for its free API
export ACCOUNTS="You:1500:1.0:0:cash,Dad:1500000:1.0:0.02:margin"
python -m schwab_trader.weekly_report
```

Gmail needs an **App Password** (Google Account → Security → 2-Step Verification → App
passwords). Each email has:
- the market trend and each account's holdings with a **BUY date/time** (next session,
  9:35 AM ET) and a **SELL date/time** (next weekly check), shares and dollars, a
  warning when a pick reports earnings that week, and the worst backtest drop in dollars;
- the momentum and scorecard rankings;
- the next morning's 0DTE QQQ condor plan for accounts that trade options;
- earnings beats/misses for the last 7 days and who reports in the next 7.

`--with-scorecard` adds the full 0-100 scorecard table and put/avoid list.

`ACCOUNTS` entries are `name:value:strategy_share:options_risk:cash|margin`. The default
runs the share strategy with all of both accounts, no options for the $1,500 account,
and at most 2% ($30,000) at risk per condor in the $1.5M account. Lower
`strategy_share` (e.g. `Dad:1500000:0.10:0.02:margin` for $150,000) to put less of an
account in the strategy; at $1.5M the backtested worst drop was about −$3.0M from the
peak.

## Dad's daily plan (Sunday–Friday, 8:30 AM ET)

`daily_report.py` emails a plan for the $1.5M margin account every morning before the
open (built by a scheduled task in the Claude app from read-only Robinhood data):

- market trends (SPY, QQQ, sectors) and the 5-day "similar past mornings" forecast;
- Asia overnight (US-listed Japan/China/Hong Kong/Korea/Taiwan/Australia/India funds in
  Robinhood's 24-hour market), the US sectors and stocks it moves most at the open, and
  after-hours / overnight / pre-market moves;
- **EPS picks** (last night's and this morning's reports, beat/miss, gap) with buy and
  sell dates; **regular stock picks** (dip-buys); a **put-spread options play** on each
  new pick; the **QQQ** 0DTE condor plan and QQQ put spreads; chart patterns spotted at
  the close with their track record; the week's earnings calendar.

Half the account goes to EPS picks (5 slots of $150,000), half to dip-buys (3 slots of
$250,000); each options play risks at most 1% ($15,000), condors 2%. Whole plan,
2021–2026: $1.5M → **$4.08M (+22.2%/yr)** vs the S&P's $2.51M, worst drop −11%, 85%
of 1,625 trades won (mid option pricing). Details: [results/daily/RESULTS.md](results/daily/RESULTS.md).

```bash
python -m schwab_trader.daily_research               # the research (about 25 minutes)
python -m schwab_trader.daily_report --extended BARS... --earnings-json PAST NEXT
```

The weekly email is now for your account only; set `ACCOUNTS` to `BOTH_ACCOUNTS` in
`sizing.py` to size Dad's account there too.

## Trading on Schwab

1. Create an app at <https://developer.schwab.com> with **Accounts and Trading
   Production** + **Market Data Production**, callback `https://127.0.0.1:8182`.
   Approval can take several days.
2. `export SCHWAB_API_KEY=... SCHWAB_APP_SECRET=...` (and `SCHWAB_ACCOUNT_NUMBER=...` if
   you have more than one account).
3. Monday after 9:35 AM ET:
   ```bash
   python -m schwab_trader.live_trader          # dry run: prints ranking, targets, orders
   python -m schwab_trader.live_trader --live   # sends them after you type YES
   ```
   The first run opens a browser to log in. **Schwab's login token lasts 7 days**, so
   expect to log in again about weekly; that is a Schwab rule, not a bug.

Safety rails: only the strategy's ETFs are touched (your other holdings are ignored),
buys are capped by cash (never margin), marketable limit orders only, sells before buys,
one rebalance per week unless `--force`, `--max-capital 1500` caps what it manages, and
every action is logged to `~/.schwab_trader/trades.log`. A **cash account** is the best
fit: no pattern-day-trader limits and no borrowing. Weekly trades create short-term
capital gains in a taxable account; a Roth IRA avoids that.

## Earnings play book

`earnings.py` rebuilds each company's last 8+ quarters from SEC EDGAR 10-Q/10-K filings,
using figures as first reported and the date they became public, then scores the latest
quarter from −1 to +1:

| Block | Weight | Inputs |
|---|---:|---|
| Cash flow | 55% | TTM free-cash-flow growth, FCF-margin change, operating cash flow ÷ net income, # FCF-positive quarters |
| Earnings | 25% | EPS vs. same quarter last year, standardized by its 8-quarter volatility; revenue growth |
| Balance sheet | 20% | Net-debt change, share-count change (buybacks), current-ratio change |

≥ +0.25 is BULLISH (a buy for the earnings-drift strategy), ≤ −0.25 BEARISH. The weights
were set in advance, not fitted. The drift backtest buys BULLISH filers at the next
Monday open after the filing and holds a fixed number of weeks:

```bash
python -m schwab_trader.data --stocks           # stock prices + split history
SEC_USER_AGENT="Your Name you@gmail.com" python -m schwab_trader.earnings_backtest
```

It has **not been run on real data yet** (SEC was unreachable from the environment that
built this); it is unit-tested on synthetic filings.

## Stock scorecard

Every stock in the watchlist gets a weekly 0-100 score; any component below 25 rejects
it as a buy; the weakest (trend and momentum both failing) form the put/avoid list. The
first version (seven classic technical components) did **not** predict returns; the
current one keeps only momentum and volatility, which did. Over the last 5 years its
weekly top 5 made 19.9%/yr vs SPY's 10.8% at $1.5M, with a −40% worst drop. Buying puts
on the put list lost ~4% a trade, and shorting it made ~0%, so treat it as a list of stocks
not to own. Details: [results/scorecard/RESULTS.md](results/scorecard/RESULTS.md).

## Volatile-day detector and 0DTE QQQ

The detector is genuinely predictive (AUC 0.72–0.74 on 2021–2026 data it never saw;
flagged days moved twice as much). *Buying* 0DTE options on volatile days won only
40–47% of trades and lost money over 2005–2021, so it is used the other way round:
`vol_alert.py` runs after the 9:30 open and, on days rated among the calmest 30%,
recommends *selling* a 1%-wide QQQ iron condor for accounts with `options_risk > 0`
(won 89–93% of trades in 2021–2026). The weekly email includes the same check as a
table for the next session at different opening gaps. Tuning technical rules on the
2021–2026 window produced 200%+ on paper (even $60 billion) and lost 99.99% on
2005–2021. Details: [results/zero_dte/RESULTS.md](results/zero_dte/RESULTS.md),
[results/STRATEGY_RESULTS.md](results/STRATEGY_RESULTS.md).

## Options study (0DTE through 2-month expiries)

`options_backtest.py` tests buying SPY calls/puts, the only options trades a $1,500
account can make at Schwab (spreads and selling need a margin account, $2,000 minimum).
1,248 variants: always-calls / trend / reversal signals, daily or weekly entries,
0, 1, 5, 21 and 42 trading days to expiry, ATM to 2% OTM, 5–50% of the account per
trade, with or without +100%/−50% exits. Same protocol as the ETFs: pick the highest
return on 2005–2021, test once on 2021–2026.

Historical prices of expired options were not available, so premiums are **modelled**
(Black-Scholes on VIX with a skew) under three pricing levels: `fair` (no volatility
premium, the best case for a buyer), `vix`, and `mid` (used for selection). Costs
include Schwab's $0.65/contract and the bid/ask spread. Results
([results/options/RESULTS.md](results/options/RESULTS.md)): **no variant reached 200%/yr**
in either period; under realistic pricing none of the 96 0DTE variants made money over the
last 5 years and over half lost 90%+; the design-period winner (2-month calls) could not
even buy one contract with $1,500 at today's SPY price. Don't trade 0DTE with this account.

```bash
python -m schwab_trader.options_backtest --phase design
python -m schwab_trader.options_backtest --phase test
```

Not financial advice. Past and backtested returns do not guarantee future results.
