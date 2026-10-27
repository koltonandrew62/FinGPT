"""Short sells and bearish options (the other side of the trade), tested like the rest.

Signals (S&P 500 stocks, point-in-time membership):
  rally  - a weak stock (below its 200-day average, among the `bottom` weakest by 12-1
           momentum) that just bounced hard (2-day RSI above `rsi_min`): short the next open.
  eps    - after an earnings report the stock gapped down at least `min_gap` and closed
           below its open: short the next open (optionally only after an EPS miss).
  Optional filters: only while SPY is below its 200-day average; skip stocks with earnings
  due during the trade.
Exits: cover with a limit order when the stock falls `target`, a buy-stop when it rises
`stop` (filled at the open if it gaps above it), else cover at the close of session
`hold`. Costs: 5 bps a side plus 2.5%/yr for borrowing the shares and the dividends a
short seller pays. Same selection as the long side: at least 70% of design trades won,
no design trade worse than -20%, best average.

Bearish options on the same signals: SELL a bear call spread (call `otm` above the
price, a second one `width` further up) or BUY a put, priced like the put spreads
(Black-Scholes on trimmed realized volatility at fair / mid / rich levels, bid/ask and
commissions included). Chosen by the highest design win rate with a positive average.

    python -m schwab_trader.short_research
"""
from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .accuracy_research import Exit, arrays, research_setup, select_safe, sessions_to_earnings, stats
from .backtest import performance
from .config import DATA_DIR, RESULTS_DIR, SPLITS
from .daily_research import (CASH, DESIGN, STOCKS, TEST, calm_vol, in_window, opt_half_spread, pricing_multipliers,
                             select, trade_stats)
from .options_backtest import COMMISSION, MULTIPLIER, bs_price, implied_vol, rate
from .pullback import indicators
from .strategy import split_factor

OUT = RESULTS_DIR / "shorts"
SLIP = 0.0005
CARRY = 0.025                  # a year: stock-borrow fee + dividends owed while short


# ----------------------------------------------------------------- engine
def simulate_short(px: dict, arr: dict, entries: pd.DataFrame, ex: Exit, symbols, carry: float = CARRY) -> pd.DataFrame:
    """Short at the entry session's open; cover at -target (limit), +stop (stop) or the
    close of session `hold`. If both are touched the same day the stop is assumed first."""
    if entries.empty:
        return pd.DataFrame(columns=["symbol", "entry", "exit", "reason", "return"])
    idx = px["close"].index
    col = {s: i for i, s in enumerate(symbols)}
    e = entries[entries.symbol.isin(col)]
    r = idx.get_indexer(pd.DatetimeIndex(e["entry"]))
    keep = r >= 0
    e, r = e[keep], r[keep]
    c = np.array([col[s] for s in e.symbol])
    T = len(idx)
    rows = r[:, None] + np.arange(ex.hold)[None, :]
    valid = rows < T
    rc = np.minimum(rows, T - 1)
    O, H, L, C = (a[rc, c[:, None]] for a in (arr["open"], arr["high"], arr["low"], arr["close"]))
    entry = O[:, 0] * (1 - SLIP)
    tgt = entry * (1 - ex.target) if ex.target else np.full(len(e), -np.inf)
    stp = entry * (1 + ex.stop) if ex.stop else np.full(len(e), np.inf)
    hit_t, hit_s = (L <= tgt[:, None]) & valid, (H >= stp[:, None]) & valid
    first_t = np.where(hit_t.any(1), hit_t.argmax(1), ex.hold)
    first_s = np.where(hit_s.any(1), hit_s.argmax(1), ex.hold)
    last = valid.sum(1) - 1
    i = np.arange(len(e))
    stop_first = first_s <= first_t
    by_stop = (first_s < ex.hold) & stop_first & (first_s <= last)
    by_tgt = (first_t < ex.hold) & ~stop_first & (first_t <= last)
    k = np.where(by_stop, first_s, np.where(by_tgt, first_t, last))
    cover = C[i, last]
    cover = np.where(by_stop, np.where(k > 0, np.maximum(O[i, k], stp), stp), cover)
    cover = np.where(by_tgt, np.where(k > 0, np.minimum(O[i, k], tgt), tgt), cover)
    reason = np.where(by_stop, "stop", np.where(by_tgt, "target", "time"))
    ret = (entry - cover * (1 + SLIP)) / entry - carry * (k + 1) / 252
    ok = np.isfinite(ret) & valid[:, 0]
    complete = (r + ex.hold - 1 < T) | (reason != "time")
    out = pd.DataFrame({"symbol": e.symbol.values, "entry": idx[r], "exit": idx[np.minimum(r + k, T - 1)],
                        "reason": reason, "return": ret})
    return out[ok & complete].reset_index(drop=True)


def portfolio_short(px, tr: pd.DataFrame, w, slots: int, cash: float) -> pd.Series:
    """Equal slots of current equity, each short marked to market at the close."""
    close = px["close"]
    days = close.index[in_window(close.index, w)]
    t = tr[in_window(pd.DatetimeIndex(tr["entry"]), w)].sort_values("entry").rename(columns={"return": "ret"})
    by = {d: g for d, g in t.groupby("entry")}
    open_, eq = [], []
    for d in days:
        if d in by:
            value = cash + sum(p["stake"] * (1 - close.at[d, p["sym"]] / p["px0"]) for p in open_)
            for x in by[d].itertuples():
                if len(open_) >= slots:
                    break
                open_.append({"sym": x.symbol, "exit": x.exit, "ret": x.ret, "stake": value / slots,
                              "px0": px["open"].at[d, x.symbol]})
        for p in [p for p in open_ if p["exit"] == d]:
            cash += p["stake"] * p["ret"]
            open_.remove(p)
        eq.append(cash + sum(p["stake"] * (1 - close.at[d, p["sym"]] / p["px0"]) for p in open_))
    return pd.Series(eq, index=days)


# ---------------------------------------------------------------- signals
def rally_entries(px: dict, rsi_min: float, bottom: int, eligible: pd.DataFrame | None) -> pd.DataFrame:
    syms = [s for s in STOCKS if s in px["close"]]
    close = px["close"][syms]
    el = None if eligible is None else eligible.reindex(columns=syms).fillna(False)
    ind = indicators(close, el)
    n = ind["mom_rank"].notna().sum(axis=1)
    weak = ind["mom_rank"].gt(n - bottom, axis=0)
    sig = (close < ind["sma200"]) & weak & (ind["rsi2"] > rsi_min)
    if el is not None:
        sig &= el.astype(bool)
    sig = sig.fillna(False)
    fresh = sig & ~sig.shift(1, fill_value=False)
    st = fresh.stack()
    st = st[st]
    idx = close.index
    pos = idx.get_indexer(st.index.get_level_values(0)) + 1
    ok = pos < len(idx)
    return pd.DataFrame({"symbol": st.index.get_level_values(1)[ok], "entry": idx[pos[ok]]})


def eps_entries(px: dict, events: pd.DataFrame, min_gap: float, need_miss: bool) -> pd.DataFrame:
    c, o = px["close"], px["open"]
    idx = c.index
    rows = []
    for e in events.itertuples():
        if e.symbol not in c or e.day not in idx:
            continue
        d = idx.get_loc(e.day)
        if d < 1 or d + 1 >= len(idx):
            continue
        prev = c.iat[d - 1, c.columns.get_loc(e.symbol)]
        op, cl = o.at[e.day, e.symbol], c.at[e.day, e.symbol]
        if not (np.isfinite(prev) and np.isfinite(op) and np.isfinite(cl)):
            continue
        if op / prev - 1 > -min_gap or not cl < op:
            continue
        if need_miss and not (e.surprise < 0):
            continue
        rows.append({"symbol": e.symbol, "entry": idx[d + 1]})
    return pd.DataFrame(rows, columns=["symbol", "entry"])


def spy_downtrend(px: dict) -> pd.Series:
    c = px["close"]["SPY"]
    return (c < c.rolling(200).mean()).fillna(False)


def filtered(px, entries, spy_down: bool, no_earn_hold: int | None, events) -> pd.DataFrame:
    e = entries
    if spy_down and len(e):
        idx = px["close"].index
        prev = idx[np.maximum(idx.get_indexer(pd.DatetimeIndex(e["entry"])) - 1, 0)]
        e = e[spy_downtrend(px).reindex(prev).to_numpy()]
    if no_earn_hold and len(e):
        e = e[sessions_to_earnings(px, events, e) >= no_earn_hold]
    return e.reset_index(drop=True)


EXITS = [Exit(t, s, h) for t, s, h in itertools.product([0.01, 0.02, 0.03, None], [0.05, 0.08, 0.10, 0.15], [5, 10, 20])]


def grid(px, arr, syms, entries, exits) -> pd.DataFrame:
    rows = []
    for ex in exits:
        tr = simulate_short(px, arr, entries, ex, syms)
        d, t = stats(tr, DESIGN), stats(tr, TEST)
        rows.append({"exit": ex.label().replace("target", "cover target").replace("stop", "buy-stop"), **asdict(ex),
                     **{f"design_{k}": v for k, v in d.items()}, **{f"test_{k}": v for k, v in t.items()}})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- options
@dataclass(frozen=True)
class BearConfig:
    signal: str = "rally"
    structure: str = "call_spread"     # call_spread (sell) | put (buy)
    otm: float = 0.06                  # call spread: short call this far above; put: strike this far below
    width: float = 0.05
    dte: int = 10
    take_profit: float = 0.5           # spread: buy back at (1-tp) of the credit; put: sell at +tp
    hold: int = 0                      # put: sell at the close after `hold` sessions (0 = expiry)

    def label(self) -> str:
        if self.structure == "call_spread":
            tp = f", buy back at {1 - self.take_profit:.0%} of the credit" if self.take_profit else ", hold to expiry"
            return f"sell a bear call spread {self.otm:.0%} above, {self.width:.1%} wide, ~{self.dte} sessions{tp}"
        return (f"buy a put {self.otm:.0%} below, ~{self.dte} sessions"
                + (f", sell at +{self.take_profit:.0%}" if self.take_profit else "") + f", hold {self.hold or self.dte}")


def bear_trade(cfg: BearConfig, s0: float, closes: np.ndarray, iv: float, r: float, real: float) -> dict:
    if cfg.structure == "call_spread":
        ks, kl = s0 * (1 + cfg.otm), s0 * (1 + cfg.otm + cfg.width)
        t0 = (cfg.dte + 1) / 252
        cs, cl = (bs_price(s0, k, t0, implied_vol(iv, s0, k, t0), r, True) for k in (ks, kl))
        credit = (cs - opt_half_spread(cs)) - (cl + opt_half_spread(cl))
        width = kl - ks
        if credit <= 0.01 * width:
            return {}
        risk = (width - credit) * real * MULTIPLIER + 4 * COMMISSION
        for j in range(cfg.dte + 1):
            s = closes[j]
            if j == cfg.dte:
                owed = max(0.0, s - ks) - max(0.0, s - kl)
                cost = owed + (opt_half_spread(owed) if owed > 0 else 0.0)
                legs = 4 if owed > 0 else 2
                break
            if cfg.take_profit:
                t = (cfg.dte - j) / 252
                vs, vl = (bs_price(s, k, t, implied_vol(iv, s, k, t), r, True) for k in (ks, kl))
                buyback = (vs + opt_half_spread(vs)) - (vl - opt_half_spread(vl))
                if buyback <= (1 - cfg.take_profit) * credit:
                    cost, legs = max(buyback, 0.0), 4
                    break
        pnl = (credit - cost) * real * MULTIPLIER - legs * COMMISSION
        return {"pnl": pnl, "risk": risk, "ret": pnl / risk, "days": j}
    k = s0 * (1 - cfg.otm)
    t0 = (cfg.dte + 1) / 252
    mid = bs_price(s0, k, t0, implied_vol(iv, s0, k, t0), r, False)
    paid = mid + opt_half_spread(mid)
    if paid <= 0.05:
        return {}
    last = min(cfg.hold or cfg.dte, cfg.dte)
    for j in range(last + 1):
        t = (cfg.dte - j) / 252
        v = bs_price(closes[j], k, t, implied_vol(iv, closes[j], k, t), r, False)
        got = max(v - opt_half_spread(v), 0.0)
        if (cfg.take_profit and got >= (1 + cfg.take_profit) * paid) or j == last:
            break
    pnl = (got - paid) * real * MULTIPLIER - 2 * COMMISSION
    risk = paid * real * MULTIPLIER + COMMISSION
    return {"pnl": pnl, "risk": risk, "ret": pnl / risk, "days": j}


def run_bear(px, entries, cfg: BearConfig, vol, mult, splits) -> pd.DataFrame:
    c, o = px["close"], px["open"]
    idx = c.index
    rows = []
    for s, d in zip(entries["symbol"], entries["entry"]):
        i = idx.get_loc(d)
        if i < 1 or i + cfg.dte >= len(idx) or s not in vol:
            continue
        v, s0 = vol.at[idx[i - 1], s], o.at[d, s]
        closes = c[s].values[i:i + cfg.dte + 1]
        if not (np.isfinite(v) and v > 0 and np.isfinite(s0) and np.isfinite(closes).all()):
            continue
        t = bear_trade(cfg, s0, closes, v * mult, rate(d.year), split_factor(s, d, splits))
        if t:
            rows.append({"symbol": s, "entry": d, "exit": idx[i + t["days"]], **t})
    return pd.DataFrame(rows)


OPT_GRID = ([BearConfig(sig, "call_spread", k, w, dte, tp) for sig, k, w, dte, tp in itertools.product(
                ["rally", "eps"], [0.04, 0.06, 0.08], [0.025, 0.05], [5, 10], [0.0, 0.5])]
            + [BearConfig(sig, "put", k, 0, dte, tp, h) for sig, k, dte, tp, h in itertools.product(
                ["rally", "eps"], [0.0, 0.03], [10, 21], [0.0, 0.5, 1.0], [5, 0])])


# ------------------------------------------------------------------- main
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    px, syms, eligible, _, events, _ = research_setup()
    arr = arrays(px, syms)
    summary = {}

    rows = []
    for rsi_min, bottom in itertools.product((90, 95), (25, 50, 100)):
        base = rally_entries(px, rsi_min, bottom, eligible)
        for spy_down, noearn in itertools.product((False, True), (False, True)):
            for hold in (5, 10, 20):
                ent = filtered(px, base, spy_down, hold if noearn else None, events)
                g = grid(px, arr, syms, ent, [e for e in EXITS if e.hold == hold])
                for k, v in (("rsi_min", rsi_min), ("bottom", bottom), ("spy_down", spy_down), ("no_earnings", noearn)):
                    g.insert(0, k, v)
                rows.append(g)
        print("rally", rsi_min, bottom, flush=True)
    rg = pd.concat(rows, ignore_index=True)
    rg.to_csv(OUT / "rally_grid.csv", index=False, float_format="%.4f")
    pick, safe = select_safe(rg, 300)
    ex = Exit(None if pd.isna(pick.target) else float(pick.target), float(pick.stop), int(pick.hold))
    rally_ent = filtered(px, rally_entries(px, float(pick.rsi_min), int(pick.bottom), eligible), bool(pick.spy_down),
                         ex.hold if pick.no_earnings else None, events)
    tr = simulate_short(px, arr, rally_ent, ex, syms)
    summary["rally"] = {"rsi_min": float(pick.rsi_min), "bottom": int(pick.bottom), "spy_down": bool(pick.spy_down),
                        "no_earnings": bool(pick.no_earnings), "exit": asdict(ex), "safety_rule_met": safe,
                        "design": stats(tr, DESIGN), "test": stats(tr, TEST)}
    for name, w in (("design", DESIGN), ("test", TEST)):
        m = performance(portfolio_short(px, tr, w, 3, CASH))
        summary["rally"][f"{name}_portfolio"] = {"cagr": m["cagr"], "max_drawdown": m["max_drawdown"]}
    print("rally:", json.dumps(summary["rally"], indent=1, default=str), flush=True)

    rows = []
    for gap, miss, spy_down in itertools.product((0.02, 0.04, 0.06), (False, True), (False, True)):
        ent = filtered(px, eps_entries(px, events, gap, miss), spy_down, None, events)
        g = grid(px, arr, syms, ent, EXITS)
        for k, v in (("min_gap", gap), ("need_miss", miss), ("spy_down", spy_down)):
            g.insert(0, k, v)
        rows.append(g)
    eg = pd.concat(rows, ignore_index=True)
    eg.to_csv(OUT / "eps_grid.csv", index=False, float_format="%.4f")
    pick, safe = select_safe(eg, 150)
    ex = Exit(None if pd.isna(pick.target) else float(pick.target), float(pick.stop), int(pick.hold))
    eps_ent = filtered(px, eps_entries(px, events, float(pick.min_gap), bool(pick.need_miss)), bool(pick.spy_down), None, events)
    tr = simulate_short(px, arr, eps_ent, ex, syms)
    summary["eps"] = {"min_gap": float(pick.min_gap), "need_miss": bool(pick.need_miss), "spy_down": bool(pick.spy_down),
                      "exit": asdict(ex), "safety_rule_met": safe, "design": stats(tr, DESIGN), "test": stats(tr, TEST)}
    for name, w in (("design", DESIGN), ("test", TEST)):
        m = performance(portfolio_short(px, tr, w, 3, CASH))
        summary["eps"][f"{name}_portfolio"] = {"cagr": m["cagr"], "max_drawdown": m["max_drawdown"]}
    print("eps:", json.dumps(summary["eps"], indent=1, default=str), flush=True)

    # bearish options on the chosen signals
    vol = calm_vol(px["close"][[s for s in STOCKS if s in px["close"]]])
    splits = {**SPLITS, **json.loads((DATA_DIR / "splits.json").read_text())}
    entries = {"rally": rally_ent, "eps": eps_ent}
    mults = pricing_multipliers(px, vol, entries)
    summary["option_pricing"] = mults
    rows = []
    for cfg in OPT_GRID:
        f = mults["fair"][cfg.signal]
        r = {"label": cfg.label(), **asdict(cfg)}
        for lvl, m in (("fair", f), ("mid", f * (1 + mults["premium"]) / 2), ("rich", f * mults["premium"])):
            t = run_bear(px, entries[cfg.signal], cfg, vol, m, splits)
            for name, w in (("design", DESIGN), ("test", TEST)):
                st = trade_stats(t, w, "ret")
                r |= {f"{name}_{k}_{lvl}": v for k, v in st.items()}
                if len(t):
                    r[f"{name}_worst_{lvl}"] = float(t[in_window(pd.DatetimeIndex(t["entry"]), w)]["ret"].min())
        r |= {"design_trades": r["design_trades_mid"], "design_win_rate": r["design_win_rate_mid"], "design_avg": r["design_avg_mid"]}
        rows.append(r)
    og = pd.DataFrame(rows)
    og.to_csv(OUT / "options_grid.csv", index=False, float_format="%.4f")
    summary["options"] = {}
    for sig in ("rally", "eps"):
        for structure in ("call_spread", "put"):
            pick = select(og[(og.signal == sig) & (og.structure == structure)], 100)
            summary["options"][f"{sig}_{structure}"] = {
                "config": {k: (str(pick[k]) if k in ("signal", "structure") else float(pick[k]) if k in ("otm", "width", "take_profit") else int(pick[k]))
                           for k in ("signal", "structure", "otm", "width", "dte", "take_profit", "hold")},
                "label": pick.label, **{k: pick[k] for k in pick.index if k.startswith(("design_", "test_")) and "trades" not in k or k.endswith("trades_mid")}}
    print("options:", json.dumps(summary["options"], indent=1, default=str), flush=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
