"""High-hit-rate options for the $1.5M margin account: sell a 0DTE QQQ iron condor at the
open on days the volatile-day detector expects to be CALM, close it at the end of the day.

An iron condor sells a call k% above and a put k% below the open and buys protection w%
further out, so the most a trade can lose is fixed: (wing width - credit) x 100 per set.
It wins when QQQ closes between the short strikes. Sellers face the opposite pricing risk
from buyers: "fair" pricing (no volatility premium) is the WORST case for a seller and
"vix" the best; all four levels are reported.

Same protocol as the rest: pick on 2005-2021 (walk-forward detector probabilities, mid
pricing) the highest-return rule among those winning >= 80% of trades, test it once on
2021-10 .. 2026-10. Costs: $0.65 per contract per leg each way, bid/ask max($0.01, 1.5%).

    python -m schwab_trader.condor_research
"""
from __future__ import annotations

import itertools
import json

import numpy as np
import pandas as pd

from .backtest import performance
from .options_backtest import COMMISSION, MULTIPLIER, bs_price, half_spread, implied_vol, rate
from .zero_dte_research import DESIGN, TEST, context

CASH = 1_500_000.0
OUT = __import__("schwab_trader.config", fromlist=["RESULTS_DIR"]).RESULTS_DIR / "condor"


def condor_strikes(spot: float, k: float, w: float) -> tuple[int, int, int, int]:
    """(short call, long call, short put, long put) on $1 strikes."""
    sc, sp = round(spot * (1 + k)), round(spot * (1 - k))
    lc, lp = max(round(spot * (1 + k + w)), sc + 1), min(round(spot * (1 - k - w)), sp - 1)
    return sc, lc, sp, lp


def run_condor(q: pd.DataFrame, vix_open: pd.Series, flags: pd.Series, k: float, w: float, risk: float,
               iv_mult: pd.Series, start, end, initial_cash: float = CASH):
    idx = q.index[(q.index >= pd.Timestamp(start)) & (q.index <= pd.Timestamp(end))]
    cash, equity, trades = initial_cash, [], []
    t = 1.0 / 252
    for day in idx:
        if flags.get(day, False) and cash > 0:
            s, c = q.at[day, "open"], q.at[day, "close"]
            atm = vix_open[day] / 100 * iv_mult[day]
            sc, lc, sp, lp = condor_strikes(s, k, w)
            r = rate(day.year)
            mid = {key: bs_price(s, K, t, implied_vol(atm, s, K, t), r, is_call)
                   for key, K, is_call in (("sc", sc, True), ("lc", lc, True), ("sp", sp, False), ("lp", lp, False))}
            credit = (mid["sc"] - half_spread(mid["sc"]) + mid["sp"] - half_spread(mid["sp"])
                      - mid["lc"] - half_spread(mid["lc"]) - mid["lp"] - half_spread(mid["lp"]))
            width = max(lc - sc, sp - lp)
            # worst case: one side finishes fully in the money and is closed as a spread
            max_loss = (width - credit + half_spread(width)) * MULTIPLIER + 6 * COMMISSION
            if credit < 0.02 or max_loss <= 0:
                equity.append(cash)
                continue
            n = int(cash * risk // max_loss)
            if n > 0:
                call_side = max(0.0, c - sc) - max(0.0, c - lc)    # what each side costs to buy back
                put_side = max(0.0, sp - c) - max(0.0, lp - c)
                in_money_legs = sum(v > 0.005 for v in (c - sc, c - lc, sp - c, lp - c))
                closing = sum(half_spread(v) for v in (call_side, put_side) if v > 0.005)   # closed as spreads
                pnl = n * ((credit - call_side - put_side - closing) * MULTIPLIER
                           - (4 + in_money_legs) * COMMISSION)
                cash += pnl
                trades.append({"date": day, "contracts": n, "credit": credit, "max_loss": n * max_loss,
                               "pnl": pnl, "win": pnl > 0})
        equity.append(cash)
    return pd.Series(equity, index=idx), pd.DataFrame(trades)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ctx = context()
    q, vix, probs, mults = ctx["q"], ctx["vix"], ctx["probs"], ctx["mults"]
    dm, tm = ctx["design_mask"], ctx["test_mask"]

    def calm_flags(kind, keep, mask):
        if keep >= 1.0:
            return pd.Series(mask, index=q.index)
        p = probs[kind]
        return (p <= p[dm & p.notna()].quantile(keep)) & mask

    rows = []
    for kind, keep, k, w, risk in itertools.product(["simple", "complex"], [1.0, 0.5, 0.3],
                                                    [0.005, 0.0075, 0.01, 0.015], [0.005, 0.01], [0.005, 0.01, 0.02]):
        if keep >= 1.0 and kind == "complex":
            continue                                        # "every day" doesn't depend on the model
        eq, tr = run_condor(q, vix["open"], calm_flags(kind, keep, dm), k, w, risk, mults["mid"], *DESIGN)
        m = performance(eq)
        rows.append({"model": kind, "calm_share": keep, "k": k, "w": w, "risk": risk, "final_value": m["final_value"],
                     "cagr": m["cagr"], "max_drawdown": m["max_drawdown"], "trades": len(tr),
                     "win_rate": float(tr.win.mean()) if len(tr) else np.nan,
                     "worst_day": float(tr.pnl.min()) if len(tr) else np.nan})
    design = pd.DataFrame(rows).sort_values("cagr", ascending=False)
    design.to_csv(OUT / "design_grid.csv", index=False, float_format="%.4f")
    ok = design[(design.win_rate >= 0.80) & (design.cagr > 0)]
    pick = (ok if len(ok) else design).iloc[0]

    flags = calm_flags(pick.model, pick.calm_share, tm)
    tests = {}
    for name, mult in mults.items():
        eq, tr = run_condor(q, vix["open"], flags, pick.k, pick.w, pick.risk, mult, *TEST)
        m = performance(eq)
        tests[name] = {"final_value": m["final_value"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                       "trades": len(tr), "win_rate": float(tr.win.mean()) if len(tr) else None,
                       "avg_win": float(tr.pnl[tr.win].mean()) if len(tr) else None,
                       "avg_loss": float(tr.pnl[~tr.win].mean()) if (len(tr) and (~tr.win).any()) else None,
                       "worst_day": float(tr.pnl.min()) if len(tr) else None}
        if name == "mid":
            tr.to_csv(OUT / "test_trades_mid.csv", index=False, float_format="%.2f")
            eq.to_csv(OUT / "test_equity_mid.csv", float_format="%.2f")
    design_by_pricing = {}
    for name, mult in mults.items():
        eq, tr = run_condor(q, vix["open"], calm_flags(pick.model, pick.calm_share, dm), pick.k, pick.w, pick.risk,
                            mult, *DESIGN)
        design_by_pricing[name] = {"final_value": performance(eq)["final_value"], "cagr": performance(eq)["cagr"],
                                   "win_rate": float(tr.win.mean()) if len(tr) else None}
    spy = q.index[tm]
    summary = {"start_cash": CASH, "variants": len(design), "variants_80pct_win_design": int(len(ok)),
               "selected": pick.to_dict(), "design_by_pricing": design_by_pricing, "test": tests}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
