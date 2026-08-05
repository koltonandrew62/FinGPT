# QQQ 1-Minute Confluence Scalper — TradingView Pine Script v6

A day-trading engine for **QQQ on the 1-minute chart** that takes signals from a weighted vote of
12 technical indicators, draws every indicator it decides from, and is fully backtestable in
TradingView's Strategy Tester. It ships **long-only** by default; shorting is a checkbox away.

| File | Type | What it is |
|---|---|---|
| `QQQ_1m_Confluence_Scalper_Strategy.pine` | `strategy()` | The bot. Backtests, sizes positions, manages stops/targets, plots the price indicators, labels every entry, and shows the live dashboard. |
| `QQQ_1m_Confluence_Scalper_Signals.pine` | `indicator()` | The decision itself as an **oscillator pane**: the Confluence Score histogram, its thresholds, gate shading, entry/exit markers, and alerts. |

---

## Read this first

I built this to be technically correct, honest about its costs, and easy to tune. I cannot make it
**profitable on demand** — nobody can hand you a guaranteed-profitable 1-minute bot, and if they
say they can, that is the tell. On a 1-minute chart you pay the spread plus commission on every
one of potentially thousands of round trips, which is exactly where most intraday edges die.

What I *can* give you, and did:

- A signal engine with no lookahead and no repainting, so the backtest is not lying to you.
- Realistic default costs baked in ($0.005/share commission + 1 tick of slippage per side).
- Hard risk controls: per-trade risk, daily loss limit, trade cap, time stop, forced EOD flat.
- Every knob exposed so you can walk-forward test it instead of trusting my defaults.

**Whether it is profitable is an empirical question that the Strategy Tester answers, not me.**
The tuning workflow below is how you find out honestly. Paper trade before risking real money.

---

## Install

1. TradingView → open a **QQQ** chart → set the timeframe to **1 minute**.
2. Open **Pine Editor** (bottom panel).
3. Paste the contents of `QQQ_1m_Confluence_Scalper_Strategy.pine` → **Save** → **Add to chart**.
4. Open a new Pine Editor tab, paste `QQQ_1m_Confluence_Scalper_Signals.pine` → **Save** →
   **Add to chart**. It lands in its own pane beneath the candles.

You need a TradingView plan with intraday data on QQQ. Free plans give a short 1-minute history,
which is not enough bars for an honest test — see *Data* below.

---

## How it decides

### 1. Twelve indicators vote

Each casts **+1** (long), **−1** (short), or **0** (no opinion), and each carries a weight:

| # | Indicator | Weight | Votes long when |
|---|---|---|---|
| 1 | EMA 9 vs EMA 21 | 12 | Fast EMA above mid EMA |
| 2 | Price vs EMA 50 | 10 | Close above EMA 50 |
| 3 | EMA 50 vs EMA 200 | 8 | Intraday regime is bullish |
| 4 | Higher timeframe (5m) trend | 14 | 5m close above its EMA 20 **and** that EMA rising |
| 5 | Supertrend (10, 2.5) | 12 | Supertrend in uptrend |
| 6 | RSI 14 vs 50 | 8 | RSI above 53 (neutral band ±3) |
| 7 | MACD histogram | 10 | Histogram positive **and** expanding |
| 8 | Stochastic RSI | 6 | %K above %D and not yet overbought |
| 9 | Price vs session VWAP | 12 | Close above VWAP |
| 10 | Volatility expansion / squeeze release | 4 | Bands expanding (or squeeze firing) with price above the basis |
| 11 | Volume-backed candle | 6 | Relative volume ≥ 1.0 on an up candle |
| 12 | Money Flow Index | 4 | MFI above 55 |

Weighted sum → normalised to a **Confluence Score** from **−100 to +100**. That score is the
histogram in the companion pane, and every individual vote is itemised in the dashboard table.

### 2. The setup arms

A long **arms** when the score first reaches **+45**, a short at **−45**. Arming lasts
`confirmBars` (default 3) bars, then expires. This stops the bot buying a trend that has been
extended for twenty bars already.

**The bot ships long-only** — *Allow shorts* is off by default, because QQQ has a persistent
upward drift and shorting an index ETF fights that drift every session. Tick the box in the
settings to trade both sides. With shorts off, the −45 threshold and the short half of the
histogram still render in the pane for context; they just never fire an order.

### 3. Price has to confirm

- **Breakout** (default) — close takes out the prior bar's high (long) / low (short).
- **Pullback** — price tags the fast EMA and closes back in the trend direction.
- **Immediate** — no price confirmation, the score crossing is enough.

### 4. Every hard gate must pass

Any single failure blocks the trade outright:

| Gate | Default | Why |
|---|---|---|
| Entry window | 09:40–15:45 ET | Skips the opening chaos and the closing auction drift |
| ADX | ≥ 18 | No chop |
| Relative volume | ≥ 0.9 | No dead tape |
| ATR as % of price | 0.02 – 0.60 % | Not too quiet, not a news spike |
| Distance from VWAP | ≤ 3 ATR | Refuses to chase extension |
| Signal-bar range | ≤ 1.5 ATR | Refuses to enter on an already-exploded candle |
| Trades today | < 6 | Overtrading control |
| Day P&L | > −2 % | Daily loss limit — stops the death spiral |
| Cooldown | 3 bars after an exit | No instant revenge re-entry |
| Position | must be flat | One position at a time |

### 5. Risk and exits

- **Size**: fixed-fractional — risk 0.5 % of equity per trade, capped at 4× notional leverage.
- **Stop**: 1.2 × ATR from entry, attached as a bracket the moment the order is submitted, so the
  fill bar is never unprotected.
- **TP1**: 50 % of the position at 1R.
- **After TP1**: stop moves to breakeven + 2 ticks, and the runner trails at 1.5 × ATR.
- **TP2**: runner exits at 2.5R (optional).
- **Time stop**: 45 bars. A 1-minute scalp that hasn't worked in 45 minutes is dead money.
- **EOD**: everything is flat by 15:55 ET. The bot never holds overnight.

---

## What it draws

On the candles (strategy): EMA 9/21 ribbon shaded by direction, EMA 50, EMA 200, session VWAP with
volume-weighted deviation bands, Supertrend coloured by direction, Bollinger Bands and Keltner
Channels, plus your live **stop / TP1 / TP2 / trailing-stop** lines while a trade is open.

At each entry, a label lists **exactly which indicators voted for that trade**, with the score, ATR,
ADX and relative volume at that moment — so you can audit any fill without re-deriving it.

Background shading: green/red when a setup is armed and waiting for confirmation, grey when a gate
is blocking trading.

In the pane (companion indicator): the Confluence Score histogram, threshold lines, gate shading,
entry arrows and exit crosses, and the same vote-by-vote dashboard.

---

## Backtesting it honestly

### Data

1-minute backtests need bars, and TradingView caps history by plan tier. On a free/Essential plan
you may only get a few weeks of 1-minute QQQ — enough to eyeball behaviour, **not** enough to
conclude anything. Aim for **at least 6 months** covering both trending and chopping regimes.

### Settings that change the answer

Open **Strategy Tester → Properties** and check:

- **Bar Magnifier** (Premium): ON. Without it, TradingView guesses the intrabar path, and when a
  single 1-minute bar contains both your stop and your target it resolves that ambiguity in a way
  that flatters the result. This matters a lot for a bot with a 1.2 ATR stop and a 1R target.
- **Recalculate: after order is filled** — leave OFF.
- **Commission**: pre-set to $0.005/share. Set it to what *your* broker actually charges.
- **Slippage**: pre-set to 1 tick. For 1-minute QQQ, test at 2 as a stress case.

### The honest workflow

1. **In-sample tune** on the first ~70 % of your data. Change **one** parameter at a time.
2. **Out-of-sample verify** on the untouched last ~30 %. If results collapse, you curve-fit —
   go back and use fewer, blunter parameters rather than more.
3. **Cost sensitivity**: double the slippage. If the edge disappears, there was no edge, only a
   cost assumption.
4. **Trade count**: below ~100 closed trades, results are noise. Do not act on them.
5. **Look at the drawdown**, not just net profit. Max drawdown and the equity curve's shape tell
   you whether the strategy is tradable by an actual human.
6. **Forward test on paper** for several weeks before committing money.

### Knobs that move the needle most

| Want | Change |
|---|---|
| Fewer, higher-quality trades | Score threshold 45 → 55–70 |
| More trades | Threshold → 35–40, or confirmation → `Immediate` |
| Less chop damage | ADX min 18 → 25 |
| Trend-following character | `Breakout` confirmation, TP2 3.5R+, wider trail |
| Mean-reversion character | `Pullback` confirmation, TP1 at 0.8R, tighter trail |
| Trade both sides | Tick *Allow shorts* (off by default) |

A threshold of 45 is a **permissive** setting: it needs only a modest majority of the twelve
indicators to agree, so it produces noticeably more signals than 55 did, and more of them will be
marginal. Two things absorb that. The daily cap still allows a maximum of 6 trades, so the extra
signals compete for the same slots rather than multiplying your exposure — the first qualifying
setups of the day win, which is not automatically the same as the best ones. And the hard gates
(ADX, relative volume, ATR range, VWAP extension) still veto, so the extra signals that survive
are the ones arriving in tradable conditions.

Watch two numbers when you compare 45 against 55 in the tester: whether **average trade net
profit** stays comfortably above your round-trip cost, and whether the extra trades improve or
dilute the **profit factor**. A lower threshold that raises gross profit while pushing average
trade toward zero is losing the edge to commission and slippage, not finding more of it.

---

## Automating it

Both scripts emit alerts.

- **Strategy**: create an alert on it and choose *Any alert() function call*. It fires on entry
  signals with the score and price.
- **Indicator**: alert conditions `QQQ Confluence LONG`, `SHORT`, and `EXIT`.

To route these to a broker you need a webhook relay (TradingView alerts → your broker's API).
The `awesome-pinescript` list you supplied catalogues both open-source relays (TradingView-Webhook-Bot,
Quantium Signal) and hosted ones (TradersPost, PickMyTrade, Capitalise.ai). Webhook alerts require a
paid TradingView plan.

**Before automating**: US pattern-day-trader rules require $25k equity in a margin account to day
trade freely, and shorting requires a margin account with locatable shares. That is your broker's
problem to confirm, not the script's.

---

## Technical notes

**Non-repainting by construction.** Higher-timeframe data uses
`request.security(..., expr[1], lookahead = barmerge.lookahead_on)` — the PineCoders idiom that
returns the last *fully closed* HTF bar on both history and real time, so historical signals and
live signals are computed from identical information. `calc_on_every_tick` is `false`, so every
decision is made on a closed bar. Orders fill at the next bar's open
(`process_orders_on_close = false`), not at the close of the bar that produced the signal.

**Session VWAP** is computed manually (cumulative price×volume with a volume-weighted variance for
the bands) and reset at the RTH open, so it stays correct regardless of your extended-hours chart
setting. With extended hours enabled, it will differ slightly from TradingView's built-in VWAP,
which anchors to the full session — that difference is intentional.

**The two files share their indicator, vote and gate logic verbatim.** If you tune one, apply the
same change to the other, or the pane will stop matching the fills.

---

## Disclaimer

This is software for research and education, not financial advice. Backtested results are
hypothetical, contain no guarantee of future performance, and do not capture every real-world cost
(borrow fees, partial fills, halts, outages, data errors). Leveraged intraday trading can lose money
faster than you expect. Trade it on paper first, and only risk capital you can afford to lose.
