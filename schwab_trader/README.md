# schwab_trader

A weekly, rules-based ETF rotation for a small (~$1,500) Charles Schwab account,
with a backtest, a weekly email + desktop-notification report, an earnings play
book, and a Schwab order bot that runs in dry-run mode until you say otherwise.

**Results (details in [results/RESULTS.md](results/RESULTS.md)).** Over the last 5 years
(Oct 2021 → Oct 2026), $1,500 in the default rule grew to **$3,507 (18.6%/yr)** vs.
**$2,508 (10.8%/yr)** for the S&P 500, with a smaller worst drawdown (−19% vs −25%).
Two caveats you should not skip:

- The rule chosen *before* looking at those 5 years **lost to the S&P** ($1,924). The
  default was picked afterwards from the variants that beat SPY in both 2005–2021 and
  2021–2026, so its numbers are optimistic. Paper-trade it first.
- Nothing came close to the 150%/yr goal ($1,500 → $145,660), and nothing honest will
  with unlevered ETFs.

## What it does

Every week (Friday close → Monday 9:35 AM ET):
1. Ranks 17 ETFs (11 S&P sectors, QQQ, IWM, EFA, EEM, GLD, TLT) by 12-month return
   skipping the latest month ("12-1 momentum").
2. Holds the top 2 in equal dollar amounts, whole shares only, no margin, no options.
3. Sells what dropped out, buys what came in; median holding ≈ 2 weeks.

| File | Purpose |
|---|---|
| `strategy.py` | Signal + whole-share sizing (shared by everything below) |
| `backtest.py`, `research.py` | Backtest engine and the design/test/robust protocol |
| `weekly_report.py`, `notify.py` | Weekly picks with buy/sell dates & times → email + desktop |
| `earnings.py`, `earnings_backtest.py` | Earnings play book: 8 quarters of SEC filings, cash-flow-led score, drift backtest |
| `live_trader.py` | Places the trades on Schwab (dry run by default) |
| `calendar_utils.py` | NYSE holidays, so dates/times in the report are real sessions |

## Install

```bash
pip install pandas numpy yfinance schwab-py matplotlib requests
python -m schwab_trader.data                 # download ETF history (Yahoo, free)
python -m schwab_trader.backtest             # 5-year backtest vs SPY
python -m pytest tests/test_schwab_trader.py
```

## Weekly email + desktop notification

```bash
export SMTP_USER=you@gmail.com SMTP_PASSWORD='<gmail app password>' EMAIL_TO=you@gmail.com
export SEC_USER_AGENT="Your Name you@gmail.com"     # SEC requires a contact for its free API
python -m schwab_trader.weekly_report
```

Gmail needs an **App Password** (Google Account → Security → 2-Step Verification → App
passwords); your normal password will not work. The report lists, for every pick, a
**BUY date/time** (next session, 9:35 AM ET) and a **SELL date/time** (next weekly check
for the rotation; a fixed exit date for earnings trades), plus this week's earnings
beats/misses, next week's reporters with their beat probability, and new 10-Q/10-K
filings scored by the cash-flow model. Desktop pop-ups use macOS Notification Center,
Windows toast, or `notify-send` on Linux (`pip install plyer` also works).

Schedule it for Friday 5 PM ET:
- **macOS/Linux** (`crontab -e`; cron uses your machine's time zone):
  `0 17 * * 5 cd /path/to/FinGPT && /usr/bin/env python3 -m schwab_trader.weekly_report >> ~/.schwab_trader/weekly.log 2>&1`
  (put the `export` lines in a small script cron calls, since cron does not read your shell profile).
- **Windows** (Task Scheduler): `schtasks /create /sc weekly /d FRI /st 17:00 /tn WeeklyPicks /tr "cmd /c cd /d C:\path\to\FinGPT && python -m schwab_trader.weekly_report"`
  and set the variables with `setx`.

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

Not financial advice. Past and backtested returns do not guarantee future results.
