"""Five stocks a day to buy, and whether to buy calls on them.

Every evening the signals of the rules that tested well on 2011-2021 (the dip-buy rule
and the chart-pattern buy setups whose average 2011-2021 trade made 0.3% or more) are
ranked by that average, then by how heavily the stock trades; the top five (at most two
from one rule, and not a stock whose earlier pick could still be open) are the day's
list. Each pick keeps its rule's limit sell and time limit, plus a -15% stop. EPS picks
have their own section. Calls: the call rule (strike, expiry, when to sell) and which
picks get one are chosen on 2011-2021 at MID option prices (realistic for a buyer); "buy
calls" only if that made money. (They didn't: the best call rule made money only at
"fair" prices with no volatility premium, and lost 10-15% of the cost a trade at mid.) Stock picks use point-in-time S&P 500 membership.

Also tried and rejected (results/top5/model_experiment.json): a gradient-boosting model
scoring every stock's next-5-day move, its 5 biggest expected moves as buys or shorts -
right on direction 51-52% of the time and -7% to -12% a year. Short sales never made
money in testing (results/shorts), so the list has no shorts.

    python -m schwab_trader.top5_research              # the list, its exits and the call rule
    python -m schwab_trader.top5_research --combined   # the whole plan: top 5 + EPS picks + options + QQQ
"""
from __future__ import annotations

import itertools
import json

import numpy as np
import pandas as pd

from .accuracy_research import Exit, arrays, research_setup, simulate, stats
from .backtest import performance
from .config import DATA_DIR, RESULTS_DIR, SPLITS
from .daily_research import (CASH, DESIGN, STOCKS, TEST, OptConfig, calm_vol, in_window, pricing_multipliers,
                             run_options, trade_stats)

OUT = RESULTS_DIR / "top5"
N_PICKS = 5
MIN_RULE_AVG = 0.003           # a rule joins the list only if its average 2011-2021 trade made 0.3%+
PER_RULE, STOP = 2, 0.15       # at most 2 picks a day from one rule; every pick gets a -15% stop
CALL_CONFIGS = [OptConfig("top5", "call", k, 0, dte, tp, hold) for k, dte, tp, hold in
                itertools.product([0.0, 0.03], [10, 21], [0.5, 1.0], [5, 0])]


def portfolio_mixed(px, tr: pd.DataFrame, w, slots: int, cash: float) -> pd.Series:
    """Equal slots of current equity, longs and shorts (column side), marked at the close."""
    close = px["close"]
    days = close.index[in_window(close.index, w)]
    t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)].sort_values("entry").rename(columns={"return": "ret"})
    by = {d: g for d, g in t.groupby("entry")}
    open_, eq = [], []
    mark = lambda p, d: p["stake"] * p["side"] * (close.at[d, p["sym"]] / p["px0"] - 1)
    for d in days:
        if d in by:
            value = cash + sum(mark(p, d) for p in open_)
            for x in by[d].itertuples():
                if len(open_) >= slots:
                    break
                if any(p["sym"] == x.symbol for p in open_):
                    continue
                open_.append({"sym": x.symbol, "exit": x.exit, "ret": x.ret, "stake": value / slots, "side": x.side,
                              "px0": px["open"].at[d, x.symbol]})
        for p in [p for p in open_ if p["exit"] == d]:
            cash += p["stake"] * p["ret"]
            open_.remove(p)
        eq.append(cash + sum(mark(p, d) for p in open_ if np.isfinite(close.at[d, p["sym"]])))
    return pd.Series(eq, index=days)





def rules(acc: dict) -> dict:
    """{rule: {avg, exit}} for the rules that qualify (decided on 2011-2021 numbers only)."""
    out = {}
    d = acc["dip"]
    if d["design"]["avg"] >= MIN_RULE_AVG:
        out["dip-buy"] = {"avg": d["design"]["avg"], "exit": d["exit"]}
    for name, r in acc["patterns"].items():
        if r["design_avg"] >= MIN_RULE_AVG:
            out[name] = {"avg": r["design_avg"], "exit": {"target": r["exit"]["target"], "stop": STOP, "hold": r["exit"]["hold"]}}
    return out


def candidates(px: dict, acc: dict, eligible: pd.DataFrame | None, events: pd.DataFrame, days: int | None = None,
               no_earnings: bool = False) -> pd.DataFrame:
    """Every signal of the qualifying rules: (date = signal close, symbol, rule, avg, dollar volume)."""
    from .accuracy_research import dip_entries, filtered
    from .patterns import pattern_frames
    rs = rules(acc)
    stocks = [s for s in STOCKS if s in px["close"]]
    idx = px["close"].index
    dv = px["close"] * px["volume"]
    rows = []
    if "dip-buy" in rs:
        d = acc["dip"]
        ent = filtered(px, dip_entries(px, d["rsi_max"], d.get("momentum_top"), eligible), d.get("spy_trend", False),
                       d["exit"]["hold"] if d.get("no_earnings") else None, events)
        sig = idx[np.maximum(idx.get_indexer(pd.DatetimeIndex(ent.entry)) - 1, 0)]
        rows.append(pd.DataFrame({"date": sig, "symbol": ent.symbol.values, "rule": "dip-buy"}))
    frames = pattern_frames(px, stocks)
    for name in [n for n in rs if n != "dip-buy"]:
        f = frames[name]
        if eligible is not None:
            f = f & eligible.reindex(index=f.index, columns=f.columns).fillna(False).astype(bool)
        if days:
            f = f.iloc[-days:]
        st = f.stack()
        st = st[st]
        rows.append(pd.DataFrame({"date": st.index.get_level_values(0), "symbol": st.index.get_level_values(1), "rule": name}))
    c = pd.concat(rows, ignore_index=True)
    if days:
        c = c[c.date >= idx[-days]]
    if no_earnings and len(c):                 # skip stocks that report before their trade would end
        from .accuracy_research import sessions_to_earnings
        pos = idx.get_indexer(pd.DatetimeIndex(c.date)) + 1
        c = c[pos < len(idx)].reset_index(drop=True)
        ent = pd.DataFrame({"symbol": c.symbol, "entry": idx[pos[pos < len(idx)]]})
        hold = c.rule.map({k: v["exit"]["hold"] for k, v in rs.items()}).to_numpy()
        c = c[sessions_to_earnings(px, events, ent) >= hold]
    c["avg"] = c.rule.map({k: v["avg"] for k, v in rs.items()})
    c["dollars"] = dv.stack().reindex(pd.MultiIndex.from_arrays([c.date, c.symbol])).to_numpy()
    return c.reset_index(drop=True)


def pick_daily(c: pd.DataFrame, acc: dict, idx: pd.DatetimeIndex, n: int = N_PICKS, exclude_today=()) -> pd.DataFrame:
    """Each day the n best signals (rule average first, then the most traded stock), at most
    PER_RULE from one rule, skipping stocks picked earlier whose trade could still be open."""
    rs = rules(acc)
    c = c.sort_values(["date", "avg", "dollars"], ascending=[True, False, False])
    pos = {d: i for i, d in enumerate(idx)}
    busy_until = {}
    out = []
    last = idx[-1]
    for d, g in c.groupby("date", sort=True):
        i, k, per = pos.get(d), 0, {}
        if i is None:
            continue
        for r in g.itertuples():
            if busy_until.get(r.symbol, -1) >= i or per.get(r.rule, 0) >= PER_RULE:
                continue
            if d == last and r.symbol in exclude_today:
                continue
            ex = rs[r.rule]["exit"]
            out.append({"date": d, "symbol": r.symbol, "rule": r.rule, "avg": r.avg, **{f"x_{k_}": v for k_, v in ex.items()}})
            busy_until[r.symbol] = i + ex["hold"]
            per[r.rule] = per.get(r.rule, 0) + 1
            k += 1
            if k >= n:
                break
    return pd.DataFrame(out)


def trades_for(px, arr, syms, picks: pd.DataFrame) -> pd.DataFrame:
    idx = px["close"].index
    pos = idx.get_indexer(pd.DatetimeIndex(picks.date)) + 1
    p = picks[pos < len(idx)].assign(entry=idx[pos[pos < len(idx)]])
    out = []
    for (t, st, h), g in p.groupby(["x_target", "x_stop", "x_hold"], dropna=False):
        tr = simulate(px, arr, g[["symbol", "entry"]], Exit(None if pd.isna(t) else float(t), float(st), int(h)), syms)
        out.append(tr.merge(g[["symbol", "entry", "rule"]], on=["symbol", "entry"], how="left"))
    tr = pd.concat(out, ignore_index=True)
    tr["side"] = 1
    return tr


def main():
    """The rule-based top 5 (python -m schwab_trader.top5_research --model for the rejected ML version)."""
    OUT.mkdir(parents=True, exist_ok=True)
    acc = json.loads((RESULTS_DIR / "accuracy" / "summary.json").read_text())
    px, syms, eligible, _, events, _ = research_setup()
    idx = px["close"].index
    arr = arrays(px, syms)
    # skip stocks with earnings due during the trade, or not: decided on the 2011-2021 average pick
    runs = {}
    for ne in (False, True):
        picks = pick_daily(candidates(px, acc, eligible, events, no_earnings=ne), acc, idx)
        tr = trades_for(px, arr, syms, picks)
        runs[ne] = (picks, tr)
        print("no_earnings", ne, stats(tr, DESIGN), stats(tr, TEST), flush=True)
    no_earn = stats(runs[True][1], DESIGN)["avg"] >= stats(runs[False][1], DESIGN)["avg"]
    picks, tr = runs[no_earn]
    picks.to_csv(OUT / "picks.csv", index=False, float_format="%.5f")
    tr.to_csv(OUT / "trades.csv", index=False, float_format="%.5f")
    summary = {"rules": {k: {"design_avg": v["avg"], "exit": v["exit"]} for k, v in rules(acc).items()},
               "no_earnings": bool(no_earn),
               "earnings_test": {str(k): {"design": stats(v[1], DESIGN), "test": stats(v[1], TEST)} for k, v in runs.items()},
               "picks_per_day": float(picks.groupby("date").size().mean()),
               "buy": {"label": "each rule's limit sell, a -15% stop, sell by its last session",
                       "exit": {"target": None, "stop": STOP, "hold": 20},
                       "design": stats(tr, DESIGN), "test": stats(tr, TEST)},
               "by_rule": {r: {"design": stats(g, DESIGN), "test": stats(g, TEST)} for r, g in tr.groupby("rule")}}
    for name, w in (("design", DESIGN), ("test", TEST)):
        m = performance(portfolio_mixed(px, tr, w, 10, CASH))
        summary["buy"][f"{name}_portfolio"] = {"cagr": m["cagr"], "max_drawdown": m["max_drawdown"]}
    print(json.dumps({k: v for k, v in summary.items() if k != "rules"}, indent=1, default=str), flush=True)

    # calls on the picks: which call, and for which picks (all, or only rules averaging 0.6%+ in 2011-2021)
    stocks = [s for s in STOCKS if s in px["close"]]
    vol = calm_vol(px["close"][stocks])
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    pos = idx.get_indexer(pd.DatetimeIndex(picks.date)) + 1
    ent = picks[pos < len(idx)].assign(entry=idx[pos[pos < len(idx)]])
    ent = ent[ent.symbol.isin(stocks)].reset_index(drop=True)
    mults = pricing_multipliers(px, vol, {"top5": ent[["symbol", "entry"]]})
    summary["option_pricing"] = mults
    rows = []
    for min_avg in (0.0, 0.006):
        e = ent[ent.avg >= min_avg]
        for cfg in CALL_CONFIGS:
            r = {"kind": "call", "min_rule_avg": min_avg, "label": cfg.label().split(": ", 1)[-1], "otm": cfg.otm,
                 "dte": cfg.dte, "take_profit": cfg.take_profit, "hold": cfg.hold}
            for lvl, m in (("fair", mults["fair"]["top5"]), ("mid", mults["fair"]["top5"] * (1 + mults["premium"]) / 2)):
                t = run_options(px, e, cfg, vol, m, splits)
                for name, w in (("design", DESIGN), ("test", TEST)):
                    st = trade_stats(t, w, "ret") if len(t) else {"trades": 0, "win_rate": np.nan, "avg": np.nan}
                    r |= {f"{name}_{k}_{lvl}": v for k, v in st.items()}
            rows.append(r)
    og = pd.DataFrame(rows)
    og.to_csv(OUT / "options_grid.csv", index=False, float_format="%.4f")
    g = og[og.design_trades_mid >= 100]
    best = g.sort_values("design_avg_mid", ascending=False).iloc[0]       # realistic (mid) prices decide
    summary["options"] = {"call": {"use": bool(best.design_avg_mid > 0), **best.to_dict()}}
    print("calls:", json.dumps(summary["options"], indent=1, default=str), flush=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))


def combined():
    """The whole new plan on $1.5M: half in the top-5 list (10 slots, buys and shorts),
    half in EPS picks (5 slots), put spreads on EPS picks and QQQ put spreads (1% at risk
    each, at most 5 open), calls/puts on top-5 picks when the rule says so (1% each), and
    the calm-day 0DTE QQQ condor (2%); mid option prices."""
    from .accuracy_research import eps_entries as acc_eps_entries, filtered as acc_filtered, portfolio
    from .backtest import buy_and_hold
    from .daily_research import EpsConfig, OUT as DAILY_OUT, condor_overlay, options_portfolio
    px, syms, eligible, _, events, _ = research_setup()
    arr = arrays(px, syms)
    acc = json.loads((RESULTS_DIR / "accuracy" / "summary.json").read_text())
    sm = json.loads((OUT / "summary.json").read_text())
    top = pd.read_csv(OUT / "trades.csv", parse_dates=["entry", "exit"])
    e = acc["eps"]
    r = e["rule"]
    eps_tr = simulate(px, arr, acc_filtered(px, acc_eps_entries(px, events, EpsConfig(r["entry"], r["min_gap"], 1, False,
                                                                                         r.get("min_surprise")))[["symbol", "entry"]],
                                            e["spy_trend"], None), Exit(**e["exit"]), syms)
    overlays = {name: pd.read_csv(DAILY_OUT / f"options_trades_{name}_mid.csv", parse_dates=["entry", "exit"]) for name in ("eps", "qqq")}
    picks = pd.read_csv(OUT / "picks.csv", parse_dates=["date"])
    idx = px["close"].index
    pos = idx.get_indexer(pd.DatetimeIndex(picks.date)) + 1
    ent = picks[pos < len(idx)].assign(entry=idx[pos[pos < len(idx)]])
    stocks = [s for s in STOCKS if s in px["close"]]
    o = sm.get("options", {}).get("call")
    if o and o["use"]:
        vol = calm_vol(px["close"][stocks])
        splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
        mults = sm["option_pricing"]
        base = ent[(ent.avg >= o["min_rule_avg"]) & ent.symbol.isin(stocks)]
        cfg = OptConfig("top5", "call", float(o["otm"]), 0, int(o["dte"]), float(o["take_profit"]), int(o["hold"]))
        overlays["call"] = run_options(px, base, cfg, vol, mults["fair"]["top5"] * (1 + mults["premium"]) / 2, splits)
    if "put_spread" in sm:                               # put spreads on every pick (1% at risk, at most 5 open)
        c = sm["put_spread"]["config"]
        base = ent[ent.symbol.isin(stocks)]
        vol = calm_vol(px["close"][stocks])
        splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
        mults = sm["option_pricing"]
        overlays["top-5 put spread"] = run_options(px, base, OptConfig(**c), vol,
                                                   mults["fair"]["top5"] * (1 + mults["premium"]) / 2, splits)
    res = {}
    for name, w in (("design", DESIGN), ("test", TEST)):
        t5 = portfolio_mixed(px, top, w, 10, CASH / 2)
        ep = portfolio(px, eps_tr, w, 5, CASH / 2)
        total = t5 + ep.reindex(t5.index).ffill()
        parts = {"top-5 shares": float(t5.iloc[-1] - t5.iloc[0]), "EPS shares": float(ep.iloc[-1] - ep.iloc[0])}
        wins = n = 0
        for tr in (top, eps_tr):
            t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]
            wins, n = wins + int((t["return"] > 0).sum()), n + len(t)
        for k, tr in overlays.items():
            ov = options_portfolio(tr, px["close"].index, w, CASH, 0.01, 5)
            total = total + (ov - CASH).reindex(total.index).ffill().fillna(0)
            parts[f"{k} options"] = float(ov.iloc[-1] - CASH)
            t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)]
            wins, n = wins + int((t["ret"] > 0).sum()), n + len(t)
        ce, ct = condor_overlay(w)
        total = total + (ce - CASH).reindex(total.index).ffill().fillna(0)
        parts["QQQ 0DTE condors"] = float(ce.iloc[-1] - CASH)
        wins, n = wins + int(ct.win.sum()), n + len(ct)
        m = performance(total)
        spy = performance(buy_and_hold(px, "SPY", *w, CASH))
        res[name] = {"final": m["final_value"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"], "trades": n,
                     "win_rate": wins / n if n else np.nan, "parts": parts, "spy_final": spy["final_value"], "spy_cagr": spy["cagr"]}
        if name == "test":
            total.to_csv(OUT / "combined_test_equity.csv", float_format="%.2f")
    sm["combined"] = res
    (OUT / "summary.json").write_text(json.dumps(sm, indent=2, default=str))
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    import sys
    combined() if "--combined" in sys.argv else main()
