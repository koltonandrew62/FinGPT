"""0DTE QQQ options on days the volatile-day detector flags.

Protocol (same as the rest of the project):
  1. Train the detectors on 2005-2021 with walk-forward predictions (each year is
     predicted by a model trained only on earlier years) and pick the highest-return
     trading rule on those predictions, at mid pricing.
  2. Test that rule once on 2021-10 .. 2026-10 under four pricing levels:
       fair      no volatility premium (best case for a buyer)
       mid       halfway to VIX-level pricing (used for selection)
       vix       QQQ implied vol at VIX's usual premium
       informed  fair, but the market prices each morning's options using everything
                 the simple model knows (e.g. it knows it is jobs-report Friday). Real
                 0DTE prices do this, so this is the fairest test of the detector.
  3. Control: the same trades on randomly chosen days at the same frequency.
  4. The "tune on the 195% window" experiment: search technical-analysis entry rules
     on 2021-2026 itself to maximise return, then run the winning rule on 2005-2021.

Fills: buy at the open, sell at the close for intrinsic value, $1 strikes, Schwab's
$0.65/contract each way, bid/ask max($0.01, 1.5%) on every leg, whole contracts.

    python -m schwab_trader.zero_dte_research
"""
from __future__ import annotations

import itertools
import json
import math

import numpy as np
import pandas as pd

from .backtest import performance, yearly_returns
from .config import DATA_DIR, RESULTS_DIR
from .data import load_prices
from .options_backtest import COMMISSION, MULTIPLIER, bs_price, fair_multipliers, half_spread, implied_vol, rate
from .vol_model import BIG_MOVE_QUANTILE, COMPLEX, SIMPLE, features, fit, predict, target

OUT = RESULTS_DIR / "zero_dte"
DESIGN = ("2005-02-01", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")
STRUCTURES = ["straddle", "strangle0.5", "strangle1", "follow_gap", "fade_gap"]
# Simulated with a $1.5M account so whole-contract rounding never decides the result
# (a 2% cap on $1,500 cannot buy even one QQQ strangle); dollar figures in the summary
# are scaled back to a $1,500 start.
SIM_CASH = 1_500_000.0
PER_1500 = 1500.0 / SIM_CASH


def legs(structure: str, spot: float, gap_up: bool) -> list:
    """[(is_call, strike), ...] for one unit of the position."""
    atm = round(spot)
    if structure == "straddle":
        return [(True, atm), (False, atm)]
    if structure.startswith("strangle"):
        k = float(structure[len("strangle"):]) / 100
        return [(True, round(spot * (1 + k))), (False, round(spot * (1 - k)))]
    if structure == "follow_gap":
        return [(gap_up, atm)]
    if structure == "fade_gap":
        return [(not gap_up, atm)]
    raise ValueError(structure)


def run_0dte(q: pd.DataFrame, vix_open: pd.Series, flags: pd.Series, structure: str, risk: float,
             iv_mult: pd.Series, start, end, initial_cash: float = 1500.0):
    """Trade 0DTE on flagged days; `iv_mult` is the per-day multiplier on VIX for QQQ's implied vol."""
    idx = q.index[(q.index >= pd.Timestamp(start)) & (q.index <= pd.Timestamp(end))]
    cash, equity, trades = initial_cash, [], []
    prev_close = q["close"].shift(1)
    t = 1.0 / 252
    for day in idx:
        if flags.get(day, False) and cash >= 50:
            spot, close = q.at[day, "open"], q.at[day, "close"]
            atm_iv = vix_open[day] / 100 * iv_mult[day]
            unit_cost, unit_legs = 0.0, []
            for is_call, k in legs(structure, spot, spot >= prev_close[day]):
                mid = bs_price(spot, k, t, implied_vol(atm_iv, spot, k, t), rate(day.year), is_call)
                ask = mid + half_spread(mid)
                unit_cost += ask * MULTIPLIER + COMMISSION
                unit_legs.append((is_call, k, ask))
            n = int(cash * risk // unit_cost)
            if n > 0 and all(a >= 0.02 for _, _, a in unit_legs):
                proceeds = 0.0
                for is_call, k, _ in unit_legs:
                    intrinsic = max(0.0, close - k) if is_call else max(0.0, k - close)
                    if intrinsic > 0.005:
                        proceeds += n * ((intrinsic - half_spread(intrinsic)) * MULTIPLIER - COMMISSION)
                cost = n * unit_cost
                cash += proceeds - cost
                trades.append({"date": day, "structure": structure, "contracts": n, "cost": cost,
                               "proceeds": proceeds, "return": proceeds / cost - 1})
        equity.append(cash)
    return pd.Series(equity, index=idx), pd.DataFrame(trades)


def walk_forward(kind: str, X: pd.DataFrame, y: pd.Series, first_test_year: int = 2008, last_year: int = 2021):
    """Out-of-sample probabilities for the design period: year Y predicted by a model fit on years < Y."""
    p = pd.Series(np.nan, index=X.index)
    for year in range(first_test_year, last_year + 1):
        train = (X.index.year < year) & (X.index >= pd.Timestamp("2005-02-01"))
        model, cols = fit(kind, X[train], y[train])
        mask = X.index.year == year
        p[mask] = predict(model, cols, X[mask])
    return p


def auc(score: pd.Series, label: pd.Series) -> float:
    ok = score.notna() & label.notna()
    s, lab = score[ok], label[ok].astype(bool)
    ranks = s.rank()
    n_pos, n_neg = lab.sum(), (~lab).sum()
    return float((ranks[lab].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    px = load_prices(["QQQ", "SPY"])
    q = pd.DataFrame({k: px[k]["QQQ"] for k in ("open", "high", "low", "close", "volume")})
    spy = pd.DataFrame({k: px[k]["SPY"] for k in ("open", "close")})
    vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")[["open", "close"]]
    vix = vix.reindex(q.index).ffill()
    X, move = features(q, vix, spy), target(q)
    design_mask = (q.index >= pd.Timestamp(DESIGN[0])) & (q.index <= pd.Timestamp(DESIGN[1]))
    test_mask = (q.index >= pd.Timestamp(TEST[0])) & (q.index <= pd.Timestamp(TEST[1]))
    cutoff = move[design_mask].quantile(BIG_MOVE_QUANTILE)
    y = (move >= cutoff).astype(float).where(move.notna())

    # --- detectors: walk-forward on design, single fit for test
    probs, report = {}, {"big_move_cutoff": float(cutoff)}
    for kind in ("simple", "complex"):
        wf = walk_forward(kind, X, y)
        model, cols = fit(kind, X[design_mask], y[design_mask])
        test_p = predict(model, cols, X[test_mask])
        probs[kind] = wf.where(~test_mask, test_p)
        top = probs[kind][test_mask] >= probs[kind][design_mask & probs[kind].notna()].quantile(0.8)
        report[f"{kind}_detector"] = {
            "auc_design_walk_forward": auc(wf[design_mask], y[design_mask]),
            "auc_test": auc(test_p, y[test_mask]),
            "test_avg_move_flagged": float(move[test_mask][top].mean()),
            "test_avg_move_not_flagged": float(move[test_mask][~top].mean()),
            "test_share_of_flagged_days_that_were_big": float(y[test_mask][top].mean()),
        }

    # --- pricing multipliers (QQQ implied vol = VIX x multiplier)
    fair_spy = fair_multipliers(spy, vix, *DESIGN)[0]
    fair_q = fair_multipliers(q, vix, *DESIGN)[0]
    vix_q = fair_q / fair_spy                     # QQQ at the same volatility premium VIX carries for SPY
    from sklearn.linear_model import LinearRegression
    ok = design_mask & X[SIMPLE].notna().all(axis=1)
    reg = LinearRegression().fit(X.loc[ok, SIMPLE], move[ok])
    exp_move = pd.Series(np.nan, index=q.index)
    okall = X[SIMPLE].notna().all(axis=1)
    exp_move[okall] = np.clip(reg.predict(X.loc[okall, SIMPLE]), 0.001, None)
    informed_sigma = exp_move * math.sqrt(math.pi / 2) * math.sqrt(252)      # E|move| -> annualized sigma
    mults = {
        "fair": pd.Series(fair_q, index=q.index), "mid": pd.Series((fair_q + vix_q) / 2, index=q.index),
        "vix": pd.Series(vix_q, index=q.index), "informed": (informed_sigma / (vix["open"] / 100)).fillna(fair_q),
    }
    report["iv_multipliers"] = {"fair": fair_q, "mid": (fair_q + vix_q) / 2, "vix": vix_q}

    # --- design-period selection (walk-forward probabilities, mid pricing)
    def flags_for(kind, rate_, window_mask):
        p = probs[kind]
        thr = p[design_mask & p.notna()].quantile(1 - rate_)
        return (p >= thr) & window_mask

    rows = []
    grid = list(itertools.product(["simple", "complex"], [0.1, 0.2, 0.3], STRUCTURES, [0.02, 0.05, 0.10, 0.25]))
    for kind, rate_, structure, risk in grid:
        eq, tr = run_0dte(q, vix["open"], flags_for(kind, rate_, design_mask), structure, risk, mults["mid"], *DESIGN,
                          SIM_CASH)
        m = performance(eq)
        rows.append({"model": kind, "flag_rate": rate_, "structure": structure, "risk": risk,
                     "final_per_1500": m["final_value"] * PER_1500, "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                     "trades": len(tr), "lost_90pct": bool((eq < 0.1 * SIM_CASH).any())})
    design = pd.DataFrame(rows).sort_values("cagr", ascending=False)
    design.to_csv(OUT / "design_grid.csv", index=False, float_format="%.4f")
    # A rule too small to ever afford a trade "wins" whenever every trading rule loses,
    # so pick the best rule that actually traded and record that not trading beat it.
    traded = design[design.trades > 0]
    pick = traded.iloc[0]
    report["design"] = {"variants": len(design), "made_money": int((design.cagr > 0).sum()),
                        "lost_90pct": int(design.lost_90pct.sum()),
                        "best_rule_was_not_trading": bool(design.iloc[0].trades == 0),
                        "selected": pick.to_dict()}

    # --- one test of the pick, all pricing levels, plus the random-day control
    flags = flags_for(pick.model, pick.flag_rate, test_mask)
    rng = np.random.default_rng(0)
    test_days = q.index[test_mask]
    tests = {}
    for name, mult in mults.items():
        eq, tr = run_0dte(q, vix["open"], flags, pick.structure, pick.risk, mult, *TEST, SIM_CASH)
        rand = []
        for _ in range(200):
            chosen = pd.Series(False, index=q.index)
            chosen[rng.choice(test_days, size=int(flags.sum()), replace=False)] = True
            rand.append(run_0dte(q, vix["open"], chosen, pick.structure, pick.risk, mult, *TEST, SIM_CASH)[0].iloc[-1])
        m = performance(eq)
        tests[name] = {"final_per_1500": m["final_value"] * PER_1500, "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                       "trades": len(tr), "win_rate": float((tr["return"] > 0).mean()) if len(tr) else None,
                       "random_days_median_final_per_1500": float(np.median(rand)) * PER_1500,
                       "pct_random_runs_beaten": float(np.mean(np.array(rand) < eq.iloc[-1]))}
        if name == "mid":
            eq.to_csv(OUT / "test_equity_mid.csv", float_format="%.2f")
            tr.to_csv(OUT / "test_trades_mid.csv", index=False, float_format="%.4f")
    report["test"] = tests

    # --- the "tune on 2021-2026 for 200%+" experiment (fair pricing, as in the 195% result)
    rules = {
        "gap>0.5%": X.gap > 0.005, "gap>1%": X.gap > 0.01, "vix>20": X.vix_open > 20, "vix>25": X.vix_open > 25,
        "vix_gap>5%": X.vix_gap > 0.05, "prev_move>1.5%": X.prev_move > 0.015, "first_friday": X.first_friday == 1,
        "rsi_extreme": X.rsi_dist > 20, "below_sma50": X.d_sma50 < 0, "bb_wide": X.bb_width > X.bb_width.rolling(252).median(),
        "streak<=-3": X.streak <= -3, "near_52w_low": X.d_low252 < 0.05, "macd_neg": X.macd_hist < 0,
        "complex_top10%": probs["complex"] >= probs["complex"][design_mask].quantile(0.9),
        "simple_top10%": probs["simple"] >= probs["simple"][design_mask].quantile(0.9),
    }
    combos = [(a,) for a in rules] + list(itertools.combinations(rules, 2))
    tuned = []
    for combo in combos:
        f = pd.Series(True, index=q.index)
        for r in combo:
            f &= rules[r].fillna(False)
        for structure, risk in itertools.product(STRUCTURES + ["strangle2"], [0.05, 0.10, 0.25, 0.50]):
            eq, tr = run_0dte(q, vix["open"], f & test_mask, structure, risk, mults["fair"], *TEST, SIM_CASH)
            tuned.append({"rule": " & ".join(combo), "structure": structure, "risk": risk,
                          "test_cagr": performance(eq)["cagr"], "test_final_per_1500": eq.iloc[-1] * PER_1500,
                          "trades": len(tr)})
    tuned = pd.DataFrame(tuned).sort_values("test_cagr", ascending=False)
    tuned.to_csv(OUT / "tuned_on_test_grid.csv", index=False, float_format="%.4f")
    best = tuned.iloc[0]
    f = pd.Series(True, index=q.index)
    for r in best.rule.split(" & "):
        f &= rules[r].fillna(False)
    oos = {}
    for name in ("fair", "mid", "informed"):
        eq_d, tr_d = run_0dte(q, vix["open"], f & design_mask, best.structure, best.risk, mults[name], *DESIGN, SIM_CASH)
        m = performance(eq_d)
        oos[name] = {"final_per_1500": m["final_value"] * PER_1500, "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                     "trades": len(tr_d), "lost_90pct": bool((eq_d < 0.1 * SIM_CASH).any())}
    eq_t_mid, _ = run_0dte(q, vix["open"], f & test_mask, best.structure, best.risk, mults["mid"], *TEST, SIM_CASH)
    eq_t_inf, _ = run_0dte(q, vix["open"], f & test_mask, best.structure, best.risk, mults["informed"], *TEST, SIM_CASH)
    report["tuned_on_test"] = {
        "combinations_searched": len(tuned), "over_200pct_in_test_fair": int((tuned.test_cagr >= 2.0).sum()),
        "best": best.to_dict(),
        "best_same_window_mid_pricing_final_per_1500": float(eq_t_mid.iloc[-1]) * PER_1500,
        "best_same_window_informed_pricing_final_per_1500": float(eq_t_inf.iloc[-1]) * PER_1500,
        "best_rule_on_2005_2021": oos,
    }
    (OUT / "summary.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
