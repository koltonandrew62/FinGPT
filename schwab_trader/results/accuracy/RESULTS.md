# Accuracy of each part of the daily plan email

`python -m schwab_trader.accuracy_research`. Every rule was chosen on 2011-02 → 2021-10
and tested once on 2021-10-25 → 2026-10-23 (the stocks are October 2021's 59 largest, so
2011–2021 has hindsight). Trade rules: among variants that won **at least 70%** of design
trades, the one with the best average design trade. (Simply maximizing the hit rate picks
+1% profit targets: 90%+ wins but almost no profit and rare large losses — see the grids.)

| Part of the email | Rule now used | 2011–2021 | 2021–2026 | Notes |
|---|---|---:|---:|---|
| Regular stock picks | dip (2-day RSI < 5) in the 10 strongest; limit sell +3%, else sell at the close of session 20 | 79% of 628 | **77% of 336** | avg +0.7% a trade, worst −31%; +14.8%/yr on its half (was 68% wins, +19.2%/yr) |
| EPS picks | next open after a report day that gapped up 4%+ and closed above its open; +3% / 20 sessions | 79% of 165 | **78% of 83** | avg +1.2%, worst −11%; few trades (+3.7%/yr on its half) |
| Chart patterns | each pattern as a trade: +3% (+2% for 52-week highs) / 20 sessions | 72–81% | **69–80%** | some only break even on average (gap-downs, 52-week highs) |
| Asia → US open | when Asian funds move 1%+: the US open goes the same way | 78–96% | **S&P 91%, Nasdaq 93%, tech 91%** | ~23% of days; staples/energy/health care 60–69% |
| Earnings beats | beat in each of the last 8 quarters → "likely beat" | 94% | **92%** | 40% of reports; every report beat 85% of the time |
| Gaps | 2–4% gap without earnings news stays open that day | 73% | **74%** | gaps under 1% close 60% of the time |
| Market direction | 100 most similar past mornings, SPY over 10 sessions, when ≥65% agree | 71% | 61% | always "up": 60% — no real edge out of sample |
| Market trend check | SPY above 200-day and 50-day rising → SPY up 20 sessions later | 67% | 66% | any day: 64% |
| Put spreads, QQQ condors | unchanged | 96–97% / 87% | 95–96% / 89–93% | from `results/daily`, `results/condor` |

Two parts can't honestly reach 70%: the market's direction over the next days and weeks
is barely more predictable than always saying "up". The email shows their real track
record next to the "always up" number instead of a made-up confidence.
