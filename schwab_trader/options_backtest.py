"""Model-based backtest of buying SPY calls/puts (0DTE through ~2-month expiries).

A $1,500 account is below Schwab's $2,000 margin minimum, so it can only BUY
options (no spreads, no selling). This tests that, honestly:

* Prices come from Black-Scholes with a simple skew (OTM puts dearer, OTM calls
  cheaper). Historical quotes for expired contracts were not available, so every
  premium here is MODELLED, not a real fill. At-the-money implied vol is VIX times
  a multiplier, under three pricing regimes:
    "fair" - the multiplier that makes implied = realized SPY moves (measured on the
             design period only): buyers pay no volatility premium. Best case.
    "vix"  - VIX as is. VIX has historically run well above realized moves.
    "mid"  - halfway between. Used to pick the strategy; all three are reported.
* Entry at the open (signal from the prior close); exit at a close. 0DTE options
  are sold at the close for their intrinsic value - they cannot be held to expiry
  in this account because an in-the-money SPY option becomes 100 shares (~$76k).
* Costs: Schwab's $0.65 per contract each way plus the bid/ask half-spread,
  max($0.01, 1.5% of the option price), on every fill. Whole contracts only.

    python -m schwab_trader.options_backtest --phase design|test
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import asdict, dataclass
from multiprocessing import Pool

import numpy as np
import pandas as pd

from .backtest import performance, yearly_returns
from .config import BENCHMARK, DATA_DIR, RESULTS_DIR
from .data import load_prices

COMMISSION = 0.65          # per contract, per side (Schwab)
MULTIPLIER = 100
DIV_YIELD = 0.014          # SPY dividend yield, roughly
MIN_EQUITY = 50.0          # below this the account stops trading
RUIN = 0.10                # "wiped out" = account fell below 10% of where it started
# Approximate average fed funds rate by year (only matters for the longer expiries).
RATES = {2004: .0135, 2005: .032, 2006: .05, 2007: .05, 2008: .019, 2016: .004, 2017: .01, 2018: .018,
         2019: .022, 2020: .004, 2021: .001, 2022: .017, 2023: .05, 2024: .051, 2025: .042, 2026: .037}
OUT_DIR = RESULTS_DIR / "options"


def rate(year: int) -> float:
    return RATES.get(year, 0.0015)   # 2009-2015: near zero


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def implied_vol(atm_iv: float, spot: float, strike: float, t: float) -> float:
    """VIX-based smile: IV changes with standardized moneyness x = ln(K/S)/(iv*sqrt(t))."""
    if t <= 0:
        return atm_iv
    x = math.log(strike / spot) / (atm_iv * math.sqrt(t))
    slope = 0.30 if x < 0 else 0.15            # puts side steeper than calls side
    return atm_iv * min(2.0, max(0.6, 1.0 - slope * x))


def bs_price(spot: float, strike: float, t: float, iv: float, r: float, call: bool, q: float = DIV_YIELD) -> float:
    """Black-Scholes value of one share's worth of option (multiply by 100 per contract)."""
    if t <= 0 or iv <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    sq = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    if call:
        return spot * math.exp(-q * t) * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * math.exp(-q * t) * _ncdf(-d1)


def half_spread(mid: float) -> float:
    return max(0.01, 0.015 * mid)


@dataclass(frozen=True)
class OptionsConfig:
    signal: str = "trend"          # "calls" | "trend" (SPY vs 50-day avg) | "reversal" (fade yesterday)
    frequency: str = "daily"       # "daily" | "weekly" (first session of each week)
    dte: int = 0                   # trading days to expiry at entry (0 = expires today)
    hold: int = 0                  # trading days held before selling at the close (<= dte)
    otm: float = 0.0               # strike distance from spot in the trade's direction (0.01 = 1%)
    risk: float = 0.25             # fraction of equity spent on premium per trade
    take_profit: float = 0.0       # e.g. 1.0 sells at +100% (checked at closes); 0 = off
    stop_loss: float = 0.0         # e.g. 0.5 sells at -50% (checked at closes); 0 = off
    pricing: str = "mid"           # "fair" | "mid" | "vix"

    def label(self) -> str:
        exits = f" tp{self.take_profit:g}/sl{self.stop_loss:g}" if self.take_profit else ""
        return (f"{self.signal} {self.frequency} {self.dte}dte hold{self.hold} otm{self.otm:g} "
                f"risk{self.risk:g}{exits} [{self.pricing}]")


def direction(cfg: OptionsConfig, closes: np.ndarray, i: int) -> bool:
    """True = buy calls, False = buy puts, from closes up to day i-1 only."""
    if cfg.signal == "calls":
        return True
    if cfg.signal == "trend":
        return closes[i - 1] > closes[i - 50:i].mean()
    if cfg.signal == "reversal":
        return closes[i - 1] < closes[i - 2]
    raise ValueError(cfg.signal)


def fair_multipliers(spy: pd.DataFrame, vix: pd.DataFrame, start, end) -> dict:
    """Realized / VIX-implied move size for each expiry, from open to the exit close."""
    v = vix.reindex(spy.index).ffill()["open"] / 100.0
    window = (spy.index >= pd.Timestamp(start)) & (spy.index <= pd.Timestamp(end))
    out = {}
    for dte in (0, 1, 5, 21, 42):
        z = np.log(spy["close"].shift(-dte) / spy["open"]) / (v * np.sqrt((dte + 1) / 252))
        out[dte] = float(z[window].std())
    return out


def iv_multiplier(cfg: OptionsConfig, fair: dict) -> float:
    return {"fair": fair[cfg.dte], "mid": (fair[cfg.dte] + 1.0) / 2, "vix": 1.0}[cfg.pricing]


def run_options_backtest(spy: pd.DataFrame, vix: pd.DataFrame, cfg: OptionsConfig, start, end,
                         initial_cash: float = 1500.0, fair: dict | None = None):
    """`spy` and `vix` are daily frames with open/close columns; `fair` comes from
    fair_multipliers() on the design window."""
    mult = iv_multiplier(cfg, fair or fair_multipliers(spy, vix, *DESIGN))
    idx = spy.index
    o, c = spy["open"].to_numpy(), spy["close"].to_numpy()
    v = vix.reindex(idx).ffill()
    v_open, v_close = v["open"].to_numpy() / 100.0, v["close"].to_numpy() / 100.0
    days = np.where((idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end)))[0]
    cash, equity, trades = initial_cash, [], []
    pos = None   # dict(call, strike, expiry_i, n, cost, entry_i)
    ruined_on = None

    def mark(i, at_close=True):
        t = max(0, pos["expiry_i"] - i) / 252.0
        spot = c[i] if at_close else o[i]
        iv = implied_vol(v_close[i] * mult, spot, pos["strike"], t) if t > 0 else 0.0
        return bs_price(spot, pos["strike"], t, iv, rate(idx[i].year), pos["call"])

    for k, i in enumerate(days):
        if i < 60:
            equity.append(cash)
            continue
        new_week = k == 0 or idx[i].isocalendar()[:2] != idx[days[k - 1]].isocalendar()[:2]
        if pos is None and cash >= MIN_EQUITY and (cfg.frequency == "daily" or new_week):
            call = direction(cfg, c, i)
            spot = o[i]
            strike = round(spot * (1 + cfg.otm if call else 1 - cfg.otm))   # $1 strikes
            t = (cfg.dte + 1) / 252.0      # sessions left including today's (0DTE: one session)
            iv = implied_vol(v_open[i] * mult, spot, strike, t)
            mid = bs_price(spot, strike, t, iv, rate(idx[i].year), call)
            ask = mid + half_spread(mid)
            n = int(cash * cfg.risk // (ask * MULTIPLIER + COMMISSION))
            if ask >= 0.05 and n > 0:
                cost = n * (ask * MULTIPLIER + COMMISSION)
                cash -= cost
                pos = {"call": call, "strike": strike, "expiry_i": i + cfg.dte, "n": n, "cost": cost,
                       "entry_i": i, "premium": ask}
        if pos is not None:
            value = mark(i)
            held = i - pos["entry_i"]
            pnl = (value - pos["premium"]) / pos["premium"]
            done = held >= cfg.hold or i >= pos["expiry_i"] or \
                (cfg.take_profit and pnl >= cfg.take_profit) or (cfg.stop_loss and pnl <= -cfg.stop_loss)
            if done:
                bid = max(0.0, value - half_spread(value)) if value > 0.005 else 0.0
                proceeds = pos["n"] * bid * MULTIPLIER - (pos["n"] * COMMISSION if bid > 0 else 0.0)
                cash += proceeds
                trades.append({"entry": idx[pos["entry_i"]], "exit": idx[i], "type": "CALL" if pos["call"] else "PUT",
                               "strike": pos["strike"], "contracts": pos["n"], "cost": pos["cost"],
                               "proceeds": proceeds, "return": proceeds / pos["cost"] - 1})
                pos = None
        val = cash + (pos["n"] * mark(i) * MULTIPLIER if pos else 0.0)
        if ruined_on is None and val < RUIN * initial_cash:
            ruined_on = idx[i]
        equity.append(val)
    eq = pd.Series(equity, index=idx[days], name=cfg.label())
    return eq, pd.DataFrame(trades), ruined_on


SMALL_ACCOUNT_RISKS = [0.05, 0.10, 0.25, 0.50]
LARGE_ACCOUNT_RISKS = [0.0025, 0.005, 0.01, 0.02, 0.05, 0.10]


def make_grid(risks) -> list:
    return [OptionsConfig(signal=s, frequency=f, dte=d, hold=h, otm=m, risk=r, take_profit=tp, stop_loss=sl)
            for s, f, (d, h), m, r, (tp, sl) in itertools.product(
                ["calls", "trend", "reversal"], ["daily", "weekly"],
                [(0, 0), (1, 1), (5, 5), (21, 5), (21, 21), (42, 5), (42, 21)],
                [0.0, 0.005, 0.01, 0.02], risks, [(0.0, 0.0), (1.0, 0.5)])
            if not (d == 0 and tp)]   # 0DTE exits at the close; daily bars can't see intraday targets


GRID = make_grid(SMALL_ACCOUNT_RISKS)
DESIGN = ("2005-02-01", "2021-10-22")
TEST = ("2021-10-25", "2026-10-23")
_DATA = None


def _data():
    global _DATA
    if _DATA is None:
        spy = load_prices([BENCHMARK])
        vix = pd.read_csv(DATA_DIR / "VIX.csv", parse_dates=["date"], index_col="date")[["open", "close"]]
        spy = pd.DataFrame({"open": spy["open"][BENCHMARK], "close": spy["close"][BENCHMARK]})
        _DATA = (spy, vix, fair_multipliers(spy, vix, *DESIGN))
    return _DATA


def evaluate(args):
    cfg, window, cash = args
    spy, vix, fair = _data()
    eq, trades, ruined = run_options_backtest(spy, vix, cfg, *window, initial_cash=cash, fair=fair)
    m = performance(eq)
    return {"label": cfg.label(), **asdict(cfg), **{k: m[k] for k in ("final_value", "cagr", "max_drawdown")},
            "trades": len(trades), "win_rate": float((trades["return"] > 0).mean()) if len(trades) else np.nan,
            "ruined": ruined is not None, "ruined_on": None if ruined is None else ruined.date().isoformat()}


def bootstrap(eq: pd.Series, years: float = 5.0, paths: int = 2000, block: int = 21, seed: int = 7,
              start: float = 1500.0) -> dict:
    """Resample the strategy's daily returns in 1-month blocks to see the spread of
    5-year outcomes, not just the one path history happened to take."""
    rets = eq.pct_change().dropna().to_numpy()
    rng = np.random.default_rng(seed)
    n = int(252 * years)
    finals = np.empty(paths)
    for p in range(paths):
        starts = rng.integers(0, len(rets) - block, size=n // block + 1)
        path = np.concatenate([rets[s:s + block] for s in starts])[:n]
        finals[p] = start * np.prod(1.0 + path)
    cagr = (finals / start) ** (1 / years) - 1
    return {"median_final": float(np.median(finals)), "p_lose_90pct": float((finals < 0.1 * start).mean()),
            "p_lose_money": float((finals < start).mean()), "p_cagr_200pct": float((cagr >= 2.0).mean()),
            "p_cagr_1000pct": float((cagr >= 10.0).mean()), "p95_final": float(np.percentile(finals, 95))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=["design", "test"], required=True)
    ap.add_argument("--cash", type=float, default=1500.0, help="starting account size (e.g. 1500000)")
    args = ap.parse_args()
    global OUT_DIR
    large = args.cash >= 100_000
    grid = make_grid(LARGE_ACCOUNT_RISKS if large else SMALL_ACCOUNT_RISKS)
    if large:
        OUT_DIR = RESULTS_DIR / "options_1.5M"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.phase == "design":
        with Pool() as pool:
            design = pd.DataFrame(pool.map(evaluate, [(cfg, DESIGN, args.cash) for cfg in grid]))
        design.sort_values("cagr", ascending=False).to_csv(OUT_DIR / "design_grid.csv", index=False, float_format="%.4f")
        pick = design.sort_values(["cagr", "final_value"], ascending=False).iloc[0]   # max return, as asked
        (OUT_DIR / "selected_config.json").write_text(json.dumps(
            {k: (v.item() if hasattr(v, "item") else v) for k, v in pick.items()}, indent=2, default=str))
        print(f"{(design.cagr > 0).sum()}/{len(design)} variants made money; {design.ruined.sum()} were wiped out")
        print(design.sort_values("cagr", ascending=False).head(12)[
            ["label", "final_value", "cagr", "max_drawdown", "win_rate", "ruined"]].to_string())
        print("SELECTED:", pick["label"])
        return

    pick = json.loads((OUT_DIR / "selected_config.json").read_text())
    cfg = OptionsConfig(**{k: pick[k] for k in OptionsConfig.__dataclass_fields__})
    spy, vix, fair = _data()
    eq, trades, ruined = run_options_backtest(spy, vix, cfg, *TEST, initial_cash=args.cash, fair=fair)
    eq_design, _, _ = run_options_backtest(spy, vix, cfg, *DESIGN, initial_cash=args.cash, fair=fair)
    bench = spy["close"].loc[TEST[0]:TEST[1]]
    bench = args.cash * bench / spy["open"].loc[TEST[0]:].iloc[0]
    stress = {p: performance(run_options_backtest(spy, vix, OptionsConfig(**{**asdict(cfg), "pricing": p}), *TEST,
                                                  initial_cash=args.cash, fair=fair)[0])["final_value"]
              for p in ("fair", "mid", "vix")}
    grid_all = [OptionsConfig(**{**asdict(c), "pricing": p}) for c in grid for p in ("fair", "mid", "vix")]
    with Pool() as pool:
        test_grid = pd.DataFrame(pool.map(evaluate, [(c, TEST, args.cash) for c in grid_all]))
    test_grid.sort_values("cagr", ascending=False).to_csv(OUT_DIR / "test_grid.csv", index=False, float_format="%.4f")
    by_pricing = {p: {"variants": int((g := test_grid[test_grid.pricing == p]).shape[0]),
                      "made_money": int((g.cagr > 0).sum()), "wiped_out": int(g.ruined.sum()),
                      "beat_spy": int((g.cagr > performance(bench)["cagr"]).sum()),
                      "cagr_200pct_plus": int((g.cagr >= 2.0).sum()), "best_cagr": float(g.cagr.max())}
                  for p in ("fair", "mid", "vix")}
    summary = {"selected": cfg.label(), "test": {**performance(eq), "trades": len(trades),
               "win_rate": float((trades["return"] > 0).mean()) if len(trades) else None,
               "ruined_on": None if ruined is None else ruined.date().isoformat()},
               "spy_test": performance(bench), "final_value_by_pricing": stress, "fair_multipliers": fair,
               "start_cash": args.cash, "bootstrap_from_design_returns": bootstrap(eq_design, start=args.cash),
               "yearly": pd.DataFrame({"options": yearly_returns(eq), "spy": yearly_returns(bench)}).to_dict("index"),
               "grid_test_by_pricing": by_pricing}
    (OUT_DIR / "test_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    pd.DataFrame({"options": eq, "spy": bench}).to_csv(OUT_DIR / "test_equity.csv", float_format="%.2f")
    trades.to_csv(OUT_DIR / "test_trades.csv", index=False, float_format="%.4f")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
