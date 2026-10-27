import datetime as dt

import numpy as np
import pandas as pd
import pytest

from schwab_trader.backtest import buy_and_hold, performance, run_backtest
from schwab_trader.calendar_utils import first_trading_day_of_week, is_trading_day, order_time
from schwab_trader.config import ALL_SYMBOLS, Costs, StrategyConfig
from schwab_trader.earnings import (beat_probability, earnings_features, outcome, quarterly_fundamentals,
                                    score_features, score_history, surprise_history)
from schwab_trader.earnings_backtest import EarningsConfig, build_events, run_earnings_backtest
from schwab_trader.live_trader import execute, market_is_open, plan_orders
from schwab_trader.strategy import compute_signal, is_rebalance_day, split_factor, whole_share_targets
from schwab_trader.weekly_report import render, trade_dates

NO_COSTS = Costs(slippage_bps=0.0, sec_fee_rate=0.0, cash_buffer=0.0, min_trade_value=0.0)


def make_prices(paths: dict, start="2020-01-01") -> dict:
    """Open == close == the given daily path for each symbol; every other fund in
    the strategy universes is present but not trading yet (all NaN)."""
    n = len(next(iter(paths.values())))
    idx = pd.bdate_range(start, periods=n)
    close = pd.DataFrame({s: np.asarray(p, dtype=float) for s, p in paths.items()}, index=idx)
    for sym in ALL_SYMBOLS:
        if sym not in close:
            close[sym] = np.nan
    return {"open": close.copy(), "close": close}


# ---------------------------------------------------------------- strategy
def test_whole_share_targets_spends_leftover_on_best_ranked():
    shares = whole_share_targets({"A": 0.5, "B": 0.5}, 1000, {"A": 300.0, "B": 90.0}, cash_buffer=0.0)
    # Floors give A=1 ($300), B=5 ($450); $250 left buys 2 more B (cap 1.5x) -> under $1000.
    assert shares == {"A": 1, "B": 7}
    assert shares["A"] * 300 + shares["B"] * 90 <= 1000


def test_whole_share_targets_keeps_filtered_slots_in_cash():
    shares = whole_share_targets({"A": 0.5}, 1000, {"A": 100.0}, cash_buffer=0.0)
    assert shares == {"A": 5}   # the other half stays in cash, not spent on A


def test_split_factor():
    assert split_factor("XLK", "2025-12-04") == 2.0
    assert split_factor("XLK", "2025-12-05") == 1.0
    assert split_factor("ZZZ", "2020-01-01", {"ZZZ": [("2021-01-01", 4.0)]}) == 4.0


def test_is_rebalance_day():
    assert is_rebalance_day("2026-10-23", "2026-10-26", "weekly")        # Fri -> Mon
    assert not is_rebalance_day("2026-10-26", "2026-10-27", "weekly")
    assert is_rebalance_day("2026-09-30", "2026-10-01", "monthly")


def test_compute_signal_refuses_missing_universe_columns():
    closes = pd.DataFrame({"XLK": np.ones(300)}, index=pd.bdate_range("2020-01-01", periods=300))
    with pytest.raises(ValueError, match="missing"):
        compute_signal(closes, StrategyConfig(universe="sectors"))


def test_compute_signal_ranks_and_absolute_filter():
    n = 300
    up = 100 * 1.002 ** np.arange(n)
    flat_up = 100 * 1.001 ** np.arange(n)
    down = 100 * 0.999 ** np.arange(n)
    prices = make_prices({"XLK": up, "XLF": flat_up, "XLE": down})
    cfg = StrategyConfig(universe="sectors", lookback="12", top_n=3, trend_filter="absolute")
    sig = compute_signal(prices["close"], cfg)
    assert list(sig.scores.index) == ["XLK", "XLF", "XLE"]
    assert set(sig.weights) == {"XLK", "XLF"} and sig.risk_off == ["XLE"]


def test_backtest_trades_on_monday_open_with_prior_close_signal():
    n = 400
    a = 100 * 1.002 ** np.arange(n)
    b = 100 * 1.0005 ** np.arange(n)
    prices = make_prices({"XLV": a, "XLP": b, "SPY": b})   # funds with no splits on record
    cfg = StrategyConfig(universe="sectors", lookback="12", top_n=1, trend_filter="none")
    start = prices["close"].index[300]
    res = run_backtest(prices, cfg, start, prices["close"].index[-1], 1500.0, NO_COSTS)
    first = res.trades.iloc[0]
    assert first.symbol == "XLV" and first.side == "BUY" and first.date == start
    assert first.shares == int(1500 // a[300])
    later = res.trades[res.trades.date > start]
    assert all(d.weekday() == 0 for d in later.date)   # bdate_range has no holidays
    assert res.equity.iloc[-1] == pytest.approx(1500 - first.shares * a[300] + first.shares * a[-1])


def test_backtest_survives_a_split_mid_hold():
    n = 320
    adj = np.full(n, 100.0)                     # split-adjusted price is flat
    prices = make_prices({"XLK": adj * 1.0, "SPY": adj})
    prices["close"]["XLK"] = prices["open"]["XLK"] = 100 * 1.001 ** np.arange(n)
    idx = prices["close"].index
    splits = {"XLK": [(idx[310].date().isoformat(), 2.0)]}
    cfg = StrategyConfig(universe="sectors", lookback="12", top_n=1, trend_filter="none")
    # min_trade_value stops the weekly top-up so the split's effect is isolated
    res = run_backtest(prices, cfg, idx[300], idx[-1], 1500.0,
                       Costs(slippage_bps=0, sec_fee_rate=0, cash_buffer=0, min_trade_value=0.2), splits=splits)
    # Real price before the split was 2x the adjusted one, so only half as many shares fit.
    first = res.trades.iloc[0]
    assert first.shares == int(1500 // (prices["open"]["XLK"].iloc[300] * 2))
    assert (res.holdings["XLK"].iloc[-1]) == first.shares * 2   # shares doubled by the split
    assert res.equity.pct_change().abs().max() < 0.01           # no fake jump on split day


def test_buy_and_hold_benchmark():
    prices = make_prices({"SPY": [100, 110, 121]})
    eq = buy_and_hold(prices, "SPY", "2020-01-01", "2020-01-03", 1000, costs=NO_COSTS)
    assert eq.iloc[-1] == pytest.approx(1210)
    assert performance(eq)["max_drawdown"] == 0


# ------------------------------------------------------------- SEC parsing
def _fact(start, end, val, filed, form):
    d = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        d["start"] = start
    return d


def synthetic_companyfacts(ocf_step=10.0, eps_step=0.05, years=range(2019, 2023)):
    """A company whose 10-Qs report cash flow only year-to-date and whose 10-K
    reports only full-year income-statement totals (Q4 must be derived)."""
    rows = {k: [] for k in ["rev", "ni", "ocf", "capex", "eps", "cash", "debt", "assets", "ca", "cl", "sh"]}
    truth = {}
    years = list(years)
    for yr in years:
        ytd = ytd_rev = 0.0
        for qi, (e, f) in enumerate([("03-31", "04-30"), ("06-30", "07-30"), ("09-30", "10-30"), ("12-31", "02-15")]):
            n = (yr - years[0]) * 4 + qi
            start = f"{yr}-01-01"
            qstart = [f"{yr}-01-01", f"{yr}-04-01", f"{yr}-07-01", f"{yr}-10-01"][qi]
            end, filed, form = f"{yr}-{e}", f"{yr + (qi == 3)}-{f}", "10-K" if qi == 3 else "10-Q"
            o = 100 + ocf_step * n
            ytd += o
            truth[end] = o
            r = 1000 + 20 * n
            ytd_rev += r
            rows["ocf"].append(_fact(start, end, ytd, filed, form))
            rows["capex"].append(_fact(start, end, 20 * (qi + 1), filed, form))
            eps_q = lambda m: round(1.0 + eps_step * m, 4)
            if qi < 3:
                rows["rev"].append(_fact(qstart, end, r, filed, form))
                rows["ni"].append(_fact(qstart, end, 80 + 5 * n, filed, form))
                rows["eps"].append(_fact(qstart, end, eps_q(n), filed, form))
            else:
                rows["rev"].append(_fact(start, end, ytd_rev, filed, form))
                rows["ni"].append(_fact(start, end, sum(80 + 5 * m for m in range(n - 3, n + 1)), filed, form))
                rows["eps"].append(_fact(start, end, sum(eps_q(m) for m in range(n - 3, n + 1)), filed, form))
            rows["sh"].append(_fact(qstart, end, 1000 - 5 * n, filed, form))
            for k, v in [("cash", 500 + 10 * n), ("debt", 800 - 10 * n), ("assets", 5000), ("ca", 1500), ("cl", 1000)]:
                rows[k].append(_fact(None, end, v, filed, form))
            if yr > years[0] and qi < 3:   # a later restatement of last year's quarter must be ignored
                rows["rev"].append(_fact(qstart.replace(str(yr), str(yr - 1)), end.replace(str(yr), str(yr - 1)),
                                         999999, filed, form))
    names = [("Revenues", "rev", "USD"), ("NetIncomeLoss", "ni", "USD"),
             ("NetCashProvidedByUsedInOperatingActivities", "ocf", "USD"),
             ("PaymentsToAcquirePropertyPlantAndEquipment", "capex", "USD"),
             ("EarningsPerShareDiluted", "eps", "USD/shares"), ("CashAndCashEquivalentsAtCarryingValue", "cash", "USD"),
             ("LongTermDebt", "debt", "USD"), ("Assets", "assets", "USD"), ("AssetsCurrent", "ca", "USD"),
             ("LiabilitiesCurrent", "cl", "USD"), ("WeightedAverageNumberOfDilutedSharesOutstanding", "sh", "shares")]
    facts = {c: {"units": {u: rows[k]}} for c, k, u in names}
    return {"facts": {"us-gaap": facts}}, truth


def test_quarterly_fundamentals_rebuilds_quarters_point_in_time():
    facts, truth = synthetic_companyfacts()
    q = quarterly_fundamentals(facts)
    assert len(q) == 16
    assert np.allclose(q.ocf, [truth[d.strftime("%Y-%m-%d")] for d in q.index])   # YTD differencing
    assert np.allclose(q.capex, 20)
    assert np.allclose(q.revenue, 1000 + 20 * np.arange(16))                      # Q4 = FY - Q1..Q3
    assert (q.filed > q.index).all()                                               # known only after filing


def test_score_rewards_growing_cash_flow_and_punishes_shrinking():
    good = quarterly_fundamentals(synthetic_companyfacts(ocf_step=10, eps_step=0.05)[0])
    bad = quarterly_fundamentals(synthetic_companyfacts(ocf_step=-10, eps_step=-0.05)[0])
    s_good, s_bad = score_features(earnings_features(good)), score_features(earnings_features(bad))
    assert outcome(s_good) == "BULLISH" and s_bad < s_good
    assert earnings_features(good.iloc[:6]) == {}            # needs 8 quarters
    hist = score_history(good)
    assert len(hist) == 9 and hist.filed.is_monotonic_increasing


def test_surprise_history_and_beat_probability():
    idx = pd.to_datetime(["2026-10-20", "2026-07-20", "2026-04-20", "2027-01-20"]).tz_localize("America/New_York")
    df = pd.DataFrame({"EPS Estimate": [1.0, 1.0, 1.0, 1.1], "Reported EPS": [1.2, 0.9, 1.0, np.nan],
                       "Surprise(%)": [20, -10, 0, np.nan]}, index=idx)
    hist = surprise_history(df)
    assert list(hist.result) == ["BEAT", "MISS", "INLINE"]
    assert hist.surprise_pct.iloc[0] == pytest.approx(20)
    assert beat_probability(hist) == pytest.approx(2 / 5)


def test_earnings_backtest_enters_after_filing_and_exits_after_hold():
    prices = make_prices({"AAA": np.linspace(50, 60, 200), "SPY": np.full(200, 100.0)}, start="2022-01-03")
    idx = prices["close"].index
    filed = idx[12]   # a Wednesday
    events = pd.DataFrame({"symbol": ["AAA", "AAA"], "end": [filed - pd.Timedelta(days=30)] * 2,
                           "filed": [filed, idx[40]], "score": [0.6, -0.5]})
    cfg = EarningsConfig(threshold=0.25, hold_weeks=4, max_positions=2)
    res = run_earnings_backtest(prices, events, cfg, idx[0], idx[-1], 1500.0, costs=NO_COSTS)
    buy, sell = res.trades.iloc[0], res.trades.iloc[1]
    assert buy.side == "BUY" and buy.date > filed and buy.date.weekday() == 0
    assert sell.side == "SELL" and (sell.date - buy.date).days == 28
    assert len(res.trades) == 2   # the negative-score filing is never bought


def test_build_events_from_fundamentals():
    q = quarterly_fundamentals(synthetic_companyfacts()[0])
    ev = build_events({"AAA": q})
    assert set(ev.columns) >= {"symbol", "filed", "score"} and (ev.symbol == "AAA").all()


# ----------------------------------------------------------------- calendar
def test_calendar_holidays_and_order_times():
    assert not is_trading_day(dt.date(2026, 11, 26))   # Thanksgiving
    assert not is_trading_day(dt.date(2026, 7, 3))     # July 4th observed (Saturday)
    assert not is_trading_day(dt.date(2026, 4, 3))     # Good Friday
    assert is_trading_day(dt.date(2026, 10, 26))
    assert first_trading_day_of_week(dt.date(2027, 1, 16)) == dt.date(2027, 1, 19)   # MLK Monday
    assert order_time(dt.date(2026, 11, 2)) == "Mon Nov 02, 2026 9:35 AM ET"
    sunday_evening = pd.Timestamp("2026-10-25 18:30", tz="America/New_York")
    when = trade_dates(dt.date(2026, 10, 23), hold_weeks=13, now=sunday_evening)
    assert when["buy"] == dt.date(2026, 10, 26) and when["review"] == dt.date(2026, 11, 2)
    assert when["sell"] == dt.date(2027, 1, 25)
    # run late on Monday night: the Monday 9:35 buy has passed, so buy at Tuesday's open
    late = trade_dates(dt.date(2026, 10, 23), now=pd.Timestamp("2026-10-26 23:00", tz="America/New_York"))
    assert late["buy"] == dt.date(2026, 10, 27) and late["review"] == dt.date(2026, 11, 2)


def _pool_prices(n=300, start="2025-08-01"):
    from schwab_trader.active_strategy import POOL
    paths = {sym: 100 * (1.0 + 0.0001 * i) ** np.arange(n) for i, sym in enumerate(POOL)}
    paths["SPY"] = 100 * 1.0005 ** np.arange(n)
    return make_prices(paths, start=start)["close"]


def test_weekly_report_lists_buy_and_sell_times_for_each_account():
    from schwab_trader.sizing import accounts_from_env
    from schwab_trader.weekly_report import active_section
    accounts = accounts_from_env()
    active = active_section(_pool_prices(), accounts)
    subject, text, page = render(active, None, accounts)
    assert subject.startswith("Weekly picks") and "MARKET TREND: UPTREND" in text
    assert "9:35 AM ET" in text and "YOU - HOLD THESE" in text and "DAD - HOLD THESE" in text
    assert all(len(a["plan"]) == 3 for a in active["accounts"].values())


def test_active_strategy_skips_unaffordable_and_checks_trend():
    from schwab_trader.active_strategy import market_trend, target_weights
    closes = _pool_prices()
    best = closes.columns[closes.iloc[-22].div(closes.iloc[-253]).argmax()]
    price = lambda s: 5000.0 if s == best else 50.0                     # top name costs $5,000 a share
    small = target_weights(closes, 1500, price)
    big = target_weights(closes, 150_000, price)
    assert best not in small and best in big and len(small) == 3
    down = closes.copy()
    down["SPY"] = 100 * 0.999 ** np.arange(len(down))
    assert market_trend(down) == "downtrend"
    for col in down.columns.drop("SPY"):                               # everything falling over 12 months
        down[col] = 100 * 0.999 ** np.arange(len(down))
    assert target_weights(down, 1500, lambda s: 50.0) == {}            # downtrend rule: only rising names
    assert len(target_weights(down, 1500, lambda s: 50.0, regime="none")) == 3


def test_condor_loses_at_most_its_max_loss():
    from schwab_trader.condor_research import condor_strikes, run_condor
    assert condor_strikes(400.0, 0.01, 0.01) == (404, 408, 396, 392)
    idx = pd.bdate_range("2025-01-06", periods=2)
    flags = pd.Series([False, True], index=idx)
    for close, win in ((400.0, True), (450.0, False), (350.0, False)):
        q = pd.DataFrame({"open": [400.0, 400.0], "close": [400.0, close]}, index=idx)
        eq, tr = run_condor(q, pd.Series(20.0, index=idx), flags, 0.01, 0.005, 0.01, pd.Series(1.0, index=idx),
                            idx[0], idx[-1], 1_500_000)
        t = tr.iloc[0]
        assert bool(t.win) == win and t.pnl >= -t.max_loss - 1e-6 and t.max_loss <= 0.01 * 1_500_000


def test_pullback_buys_dips_in_uptrends_only():
    from schwab_trader.pullback import PullbackConfig, indicators, signals
    n = 300
    up = 100 * 1.003 ** np.arange(n)
    up[-3:] = up[-4] * np.array([0.97, 0.94, 0.92])                     # sharp 3-day dip in an uptrend
    down = 100 * 0.997 ** np.arange(n)
    down[-3:] = down[-4] * np.array([0.97, 0.94, 0.92])
    close = pd.DataFrame({"UP": up, "DOWN": down}, index=pd.bdate_range("2024-01-01", periods=n))
    entry, exit_ = signals(close, indicators(close), PullbackConfig(momentum_top=0))
    assert entry["UP"].iloc[-1] and not entry["DOWN"].iloc[-1]


# ------------------------------------------------------------- live trader
def test_plan_orders_only_touches_strategy_funds_and_fits_cash():
    account = {"cash": 600.0, "positions": {"XLE": 10, "AAPL": 3}}
    quotes = {"XLE": {"bid": 45.0, "ask": 45.1, "last": 45.0}, "XLK": {"bid": 149.9, "ask": 150.0, "last": 150.0}}
    orders, info = plan_orders({"XLK": 1.0}, account, quotes, universe=("XLK", "XLE"))
    assert [o["side"] for o in orders] == ["SELL", "BUY"]
    assert all(o["symbol"] != "AAPL" for o in orders)
    buy = orders[1]
    assert buy["qty"] * buy["limit"] <= info["budget"]


class FakeResp:
    def __init__(self, payload=None, location=None):
        self._payload, self.status_code, self.is_error = payload or {}, 201, False
        self.headers = {"Location": location} if location else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeClient:
    class MarketHours:
        class Market:
            EQUITY = "equity"

    def __init__(self):
        self.placed, self.next_id = [], 1

    def place_order(self, acct, order):
        spec = order.build()
        self.placed.append(spec)
        oid, self.next_id = self.next_id, self.next_id + 1
        return FakeResp(location=f"https://api.schwabapi.com/trader/v1/accounts/{acct}/orders/{oid}")

    def get_order(self, oid, acct):
        return FakeResp({"status": "FILLED"})

    def get_market_hours(self, markets, date=None):
        return FakeResp({"equity": {"EQ": {"isOpen": True, "sessionHours": {"regularMarket": [
            {"start": "2026-10-26T09:30:00-04:00", "end": "2026-10-26T16:00:00-04:00"}]}}}})


def test_execute_sells_before_buys_and_caps_buys_by_cash():
    client = FakeClient()
    orders = [{"symbol": "XLE", "side": "SELL", "qty": 10, "limit": 44.9},
              {"symbol": "XLK", "side": "BUY", "qty": 7, "limit": 150.3}]
    out = execute(client, "HASH", orders, account_refresh=lambda: {"cash": 600.0})
    legs = [(p["orderLegCollection"][0]["instruction"], p["orderLegCollection"][0]["instrument"]["symbol"])
            for p in client.placed]
    assert legs == [("SELL", "XLE"), ("BUY", "XLK")]
    assert out[-1]["qty"] == 3            # $600 cash buys 3 shares at $150.30, not 7
    assert client.placed[1]["orderType"] == "LIMIT" and client.placed[1]["price"] == "150.30"


def test_market_is_open():
    client = FakeClient()
    assert market_is_open(client, pd.Timestamp("2026-10-26 10:00", tz="America/New_York"))
    assert not market_is_open(client, pd.Timestamp("2026-10-26 17:00", tz="America/New_York"))


# ------------------------------------------------------------------ options
def test_black_scholes_parity_and_skew():
    from schwab_trader.options_backtest import bs_price, implied_vol
    S, K, t, r, q = 500.0, 505.0, 5 / 252, 0.04, 0.014
    c, p = bs_price(S, K, t, 0.15, r, True, q), bs_price(S, K, t, 0.15, r, False, q)
    assert c - p == pytest.approx(S * np.exp(-q * t) - K * np.exp(-r * t), abs=1e-9)
    assert bs_price(S, 490, 0, 0.2, r, True) == 10.0               # expired: intrinsic
    assert implied_vol(0.15, S, 490, t) > 0.15 > implied_vol(0.15, S, 510, t)


def test_0dte_trade_settles_at_close_intrinsic_with_costs():
    from schwab_trader.options_backtest import COMMISSION, OptionsConfig, half_spread, run_options_backtest
    idx = pd.bdate_range("2024-01-01", periods=80)
    close = np.full(80, 100.0)
    open_ = close.copy()
    close[-1] = 102.0                                            # last day rallies 2% open -> close
    spy = pd.DataFrame({"open": open_, "close": close}, index=idx)
    vix = pd.DataFrame({"open": 15.0, "close": 15.0}, index=idx)
    cfg = OptionsConfig(signal="calls", dte=0, hold=0, risk=0.5, pricing="vix")
    fair = {0: 1.0, 1: 1.0, 5: 1.0, 21: 1.0, 42: 1.0}
    eq, trades, _ = run_options_backtest(spy, vix, cfg, idx[-1], idx[-1], 1500.0, fair=fair)
    t = trades.iloc[0]
    assert t.strike == 100 and t.type == "CALL"
    intrinsic = 2.0
    expected = t.contracts * ((intrinsic - half_spread(intrinsic)) * 100 - COMMISSION)
    assert t.proceeds == pytest.approx(expected)
    assert t.cost <= 0.5 * 1500 and t.contracts == int(t.contracts)   # whole contracts within the budget


# ---------------------------------------------------------------- scorecard
def _stock_panel(n=320):
    idx = pd.bdate_range("2023-01-02", periods=n)
    rng = np.random.default_rng(1)
    close = pd.DataFrame({
        "UP": 100 * np.exp(np.cumsum(0.002 + 0.02 * rng.standard_normal(n))),      # strong, volatile riser
        "DOWN": 100 * np.exp(np.cumsum(-0.002 + 0.004 * rng.standard_normal(n))),  # steady faller
        "FLAT": 100 * np.exp(np.cumsum(0.008 * rng.standard_normal(n))),
        "MID": 100 * np.exp(np.cumsum(0.0005 + 0.01 * rng.standard_normal(n))),
    }, index=idx)
    volume = pd.DataFrame(1e6, index=idx, columns=close.columns)
    return close, volume


def test_scorecard_scores_buys_rejects_and_put_list():
    from schwab_trader.scorecard import VETO, components, weekly_table
    close, volume = _stock_panel()
    comp = components(close, volume)
    t = weekly_table(comp, close.index[-1])
    assert t.score.between(0, 100).all()
    assert t.loc["UP", "verdict"] == "BUY" and t.loc["DOWN", "verdict"] == "PUT/SHORT"
    assert t.index[0] == "UP"
    rejected = t[t.verdict == "REJECT"]
    assert all(r.failed for _, r in rejected.iterrows())             # every reject names its failing component
    assert all(min(t.loc[s, ["momentum", "high_volatility"]]) < VETO for s in rejected.index)


def test_scorecard_long_weights_fn_has_no_lookahead():
    from schwab_trader.scorecard import components, long_weights_fn
    close, volume = _stock_panel()
    fn = long_weights_fn(components(close, volume), top_n=2)
    w = fn(close.iloc[:-1])
    changed = close.copy()
    changed.iloc[-1] *= 10                                           # tomorrow's move must not matter
    assert fn(changed.iloc[:-1]) == w and abs(sum(w.values()) - len(w) / 2) < 1e-9


# ------------------------------------------------------------------- sizing
def test_accounts_and_share_plans():
    from schwab_trader.sizing import accounts_from_env, contracts, share_plan
    you, dad = accounts_from_env()
    assert (you.value, you.margin, you.options_risk) == (1500, False, 0) and dad.strategy_dollars == 150_000
    plan = share_plan({"XLE": 0.5, "XLK": 0.5}, {"XLE": 69.33, "XLK": 183.23}, dad)
    assert sum(d for _, d in plan.values()) <= 150_000 and plan["XLE"][0] > 1000
    assert contracts(250.0, you) == 0 and contracts(250.0, dad) == 120  # no options vs a $30,000 cap


# ------------------------------------------------------------ 0DTE / vol model
def test_vol_features_use_only_data_known_at_the_open():
    from schwab_trader.vol_model import features
    idx = pd.bdate_range("2024-01-01", periods=300)
    rng = np.random.default_rng(2)
    c = 100 * np.exp(np.cumsum(0.01 * rng.standard_normal(300)))
    q = pd.DataFrame({"open": c * 0.999, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)
    vix = pd.DataFrame({"open": 15.0, "close": 15.0}, index=idx)
    f1 = features(q, vix)
    q2 = q.copy()
    q2.iloc[-1, q2.columns.get_indexer(["high", "low", "close", "volume"])] = [500, 1, 300, 9e9]
    f2 = features(q2, vix)
    pd.testing.assert_series_equal(f1.iloc[-1], f2.iloc[-1])           # today's close/high/low never leak


def test_zero_dte_legs_and_straddle_payoff():
    from schwab_trader.options_backtest import COMMISSION, half_spread
    from schwab_trader.zero_dte_research import legs, run_0dte
    assert legs("strangle1", 400.0, True) == [(True, 404), (False, 396)]
    assert legs("follow_gap", 400.2, False) == [(False, 400)]
    idx = pd.bdate_range("2025-01-06", periods=3)
    q = pd.DataFrame({"open": [400.0, 400.0, 400.0], "close": [400.0, 400.0, 410.0]}, index=idx)
    flags = pd.Series([False, False, True], index=idx)
    eq, tr = run_0dte(q, pd.Series(20.0, index=idx), flags, "straddle", 0.5, pd.Series(1.0, index=idx),
                      idx[0], idx[-1], 10_000.0)
    t = tr.iloc[0]
    assert t.proceeds == pytest.approx(t.contracts * ((10.0 - half_spread(10.0)) * 100 - COMMISSION))  # call leg only
    assert eq.iloc[-1] == pytest.approx(10_000.0 - t.cost + t.proceeds)
