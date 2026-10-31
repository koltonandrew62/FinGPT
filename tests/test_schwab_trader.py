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


def _pool_prices(n=300, start="2025-08-01", with_volume=False):
    """Every pool name rising, later names faster and more volatile; SPY in an uptrend."""
    from schwab_trader.active_strategy import POOL
    rng = np.random.default_rng(0)
    paths = {sym: 100 * (1.0 + 0.0001 * i) ** np.arange(n) * np.exp((0.001 + 0.0002 * i) * rng.standard_normal(n))
             for i, sym in enumerate(POOL)}
    paths["SPY"] = 100 * 1.0005 ** np.arange(n)
    close = make_prices(paths, start=start)["close"]
    return (close, pd.DataFrame(1e6, index=close.index, columns=close.columns)) if with_volume else close


def test_weekly_report_lists_buy_and_sell_times_for_each_account():
    from schwab_trader.active_strategy import LABEL, LARGE_LABEL
    from schwab_trader.sizing import BOTH_ACCOUNTS, accounts_from_env
    from schwab_trader.weekly_report import active_section
    accounts = accounts_from_env(BOTH_ACCOUNTS)
    closes, volumes = _pool_prices(with_volume=True)
    active = active_section(closes, accounts, volumes)
    subject, text, page = render(active, None, accounts)
    assert subject.startswith("Weekly picks") and "MARKET TREND: UPTREND" in text
    assert "9:35 AM ET" in text and "YOU - HOLD THESE" in text and "DAD - HOLD THESE" in text
    you, dad = active["accounts"]["You"], active["accounts"]["Dad"]
    assert (you["label"], len(you["plan"])) == (LABEL, 3) and (dad["label"], len(dad["plan"])) == (LARGE_LABEL, 2)
    assert "scorecard v2 ranking" in text and "$1.5M -> $6.71M" in text and "$1,500 -> $6,398" in text


def test_strategy_follows_account_size():
    from schwab_trader.active_strategy import LARGE_TOP_N, scorecard_ranking, target_weights, target_weights_for
    from schwab_trader.earnings_backtest import EARNINGS_UNIVERSE
    closes, volumes = _pool_prices(with_volume=True)
    price = lambda s: 50.0
    assert target_weights_for(closes, volumes, 1500, price) == target_weights(closes, 1500, price)
    assert target_weights_for(closes, None, 1_500_000, price) == target_weights(closes, 1_500_000, price)
    big = target_weights_for(closes, volumes, 1_500_000, price)
    assert list(big) == list(scorecard_ranking(closes, volumes).index[:LARGE_TOP_N])
    assert len(big) == LARGE_TOP_N and set(big) <= set(EARNINGS_UNIVERSE)       # stocks only, no ETFs


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
    from schwab_trader.sizing import BOTH_ACCOUNTS, accounts_from_env, contracts, share_plan
    assert [a.name for a in accounts_from_env()] == ["You"]          # the weekly email: your account only
    you, dad = accounts_from_env(BOTH_ACCOUNTS)
    assert (you.value, you.margin, you.options_risk) == (1500, False, 0) and dad.strategy_dollars == 1_500_000
    plan = share_plan({"XLE": 0.5, "XLK": 0.5}, {"XLE": 69.33, "XLK": 183.23}, dad)
    assert sum(d for _, d in plan.values()) <= 1_500_000 and plan["XLE"][0] > 10_000
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


# ----------------------------------------------------------- Dad's daily plan
def test_trend_labels_and_analogs_use_only_the_past():
    from schwab_trader.patterns import FEATURES, analog_forecast, trend_label
    up = pd.Series(100 * 1.002 ** np.arange(300), index=pd.bdate_range("2024-01-01", periods=300))
    assert trend_label(up) == {"short": "up", "medium": "up", "long": "up"}
    assert trend_label(up[::-1].set_axis(up.index)) == {"short": "down", "medium": "down", "long": "down"}
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", periods=800)
    X = pd.DataFrame(rng.standard_normal((800, len(FEATURES))), index=idx, columns=FEATURES)
    y = pd.Series(rng.standard_normal(800), index=idx)
    day = idx[600]
    a = analog_forecast(X, y, day, k=25)
    y2 = y.copy()
    y2.iloc[596:] = 99.0                                   # outcomes from 5 sessions before `day` on
    assert analog_forecast(X, y2, day, k=25) == a and 0 <= a["p_up"] <= 1


def test_option_trades_never_lose_more_than_their_risk():
    from schwab_trader.daily_research import OptConfig, option_trade
    spread = OptConfig("dip", "put_spread", 0.04, 0.05, 10, 0.0)
    win = option_trade(spread, 100.0, np.full(11, 101.0), 0.30, 0.04, 1.0)
    loss = option_trade(spread, 100.0, np.full(11, 70.0), 0.30, 0.04, 1.0)
    assert win["pnl"] > 0 and loss["pnl"] < 0 and loss["ret"] >= -1.05
    call = option_trade(OptConfig("dip", "call", 0.0, 0, 21, 0.0, 5), 100.0, np.full(22, 50.0), 0.30, 0.04, 1.0)
    assert -1.01 <= call["ret"] < 0


def test_eps_rules_and_reaction_days():
    from schwab_trader.daily_research import EpsConfig, eps_trades, reaction_day
    idx = pd.bdate_range("2025-01-06", periods=40)
    assert reaction_day(idx, "2025-01-08", "pm") == pd.Timestamp("2025-01-09")
    assert reaction_day(idx, "2025-01-08", "am") == pd.Timestamp("2025-01-08")
    c = pd.DataFrame({"AAA": 100.0}, index=idx)
    o = c.copy()
    o.loc[idx[10], "AAA"], c.loc[idx[10:], "AAA"] = 105.0, 107.0      # gaps up 5% and holds
    px = {"open": o, "close": c}
    ev = pd.DataFrame({"symbol": ["AAA"], "day": [idx[10]], "surprise": [0.1]})
    gap = eps_trades(px, ev, EpsConfig("gap_open", 0.04, 5))
    assert gap.iloc[0]["entry"] == idx[10] and gap.iloc[0]["exit"] == idx[15]
    nxt = eps_trades(px, ev, EpsConfig("next_open", 0.02, 3))
    assert nxt.iloc[0]["entry"] == idx[11] and nxt.iloc[0]["exit"] == idx[14]
    assert eps_trades(px, ev, EpsConfig("gap_open", 0.06, 5)).empty         # gap too small


def test_overnight_moves_split_by_session():
    from schwab_trader.daily_report import friday_expiry, overnight_moves, strike
    t = pd.to_datetime(["2025-10-27T19:30Z", "2025-10-27T21:00Z", "2025-10-28T02:00Z", "2025-10-28T12:00Z"], utc=True)
    ext = {"SPY": pd.DataFrame({"close": [100.0, 101.0, 102.01, 100.9899], "session": ["reg", "post", "overnight", "pre"]}, index=t)}
    m = overnight_moves(ext)["SPY"]
    assert abs(m["post"] - 0.01) < 1e-9 and abs(m["overnight"] - 0.01) < 1e-9 and abs(m["pre"] + 0.01) < 1e-9
    assert abs(m["total"] - 0.009899) < 1e-9 and not m["opened"]
    assert strike(87.3) == 87 and strike(212.0) == 212.5 and strike(517.0) == 515
    assert friday_expiry(dt.date(2026, 10, 28), 10).weekday() == 4


def test_missing_daily_bar_is_built_from_regular_hours_bars():
    from schwab_trader.daily_report import complete_daily, earnings_between
    idx = pd.bdate_range("2025-10-20", periods=5)                          # through Fri Oct 24
    frame = pd.DataFrame({"SPY": [100.0] * 5}, index=idx)
    px = {"open": frame.copy(), "high": frame.copy(), "low": frame.copy(), "close": frame.copy(),
          "volume": frame * 0 + 1e6}
    t = pd.date_range("2025-10-27 13:30", periods=13, freq="30min", tz="UTC")  # Mon 9:30 AM - 3:30 PM ET
    ext = {"SPY": pd.DataFrame({"open": np.linspace(101, 103, 13), "high": 104.0, "low": 100.5,
                                "close": np.linspace(101.5, 103.5, 13), "volume": 1e5, "session": "reg"}, index=t)}
    out = complete_daily(px, ext, pd.Timestamp("2025-10-27 20:00", tz="America/New_York"))
    row = {k: out[k].loc["2025-10-27", "SPY"] for k in ("open", "high", "low", "close")}
    assert row == {"open": 101.0, "high": 104.0, "low": 100.5, "close": 103.5}
    up = [{"symbol": "MRK", "date": "2025-10-29", "timing": "am"}]
    assert "2025-10-29" in earnings_between("MRK", dt.date(2025, 10, 28), dt.date(2025, 11, 7), up)
    assert earnings_between("MRK", dt.date(2025, 10, 30), dt.date(2025, 11, 7), up) == ""


def test_replay_closes_at_target_or_time_and_respects_slots():
    from schwab_trader.accuracy_research import Exit, arrays, simulate
    from schwab_trader.daily_report import replay
    idx = pd.bdate_range("2025-01-06", periods=10)        # B's 20-session limit hasn't come yet
    o = pd.DataFrame({"A": 100.0, "B": 50.0, "C": 20.0}, index=idx)
    h = o * 1.01
    h.iloc[3, 0] = 104.0                                  # A reaches +3% on its 4th session
    px = {"open": o, "high": h, "low": o * 0.99, "close": o}
    entries = [{"symbol": "A", "entry": idx[0]}, {"symbol": "B", "entry": idx[0]}, {"symbol": "C", "entry": idx[1]}]
    still = replay(px, entries, 0.03, 20, 2)
    assert [p["symbol"] for p in still] == ["B"]          # A hit its target, C found no free slot
    late = replay(px, [{"symbol": "B", "entry": idx[5]}], 0.03, 20, 2)
    assert late and late[0]["i0"] == 5
    tr = simulate(px, arrays(px, ["A", "B"]), pd.DataFrame({"symbol": ["A"], "entry": [idx[0]]}), Exit(0.03, None, 20), ["A", "B"])
    assert tr.iloc[0]["reason"] == "target" and tr.iloc[0]["exit"] == idx[3]


# ------------------------------------------------- universe, journal, learning, routine
def test_sp500_membership_is_point_in_time():
    from schwab_trader.universe import sp500_mask, stocks, was_member
    idx = pd.DatetimeIndex(["2020-06-01", "2021-01-04", "2026-10-01"])
    m = sp500_mask(idx, ["TSLA", "AAPL", "SPY"])
    assert list(m["TSLA"]) == [False, True, True]          # joined the index in December 2020
    assert m["AAPL"].all() and m["SPY"].all()              # non-stocks are never filtered
    assert list(was_member(["TSLA", "TSLA"], ["2020-06-01", "2021-06-01"])) == [False, True]
    assert {"AAPL", "MSFT", "NVDA"} <= set(stocks(with_data=False)) and len(stocks(with_data=False)) > 500


def test_safety_selection_prefers_variants_without_big_losses():
    from schwab_trader.accuracy_research import select_safe
    rows = pd.DataFrame({"design_trades": [600, 600, 600], "design_avg": [0.012, 0.008, 0.004],
                         "design_win_rate": [0.80, 0.78, 0.72], "design_worst": [-0.35, -0.18, -0.12]})
    pick, safe = select_safe(rows, 500)
    assert safe and pick.design_avg == 0.008               # best average among worst-trade >= -20%
    pick, safe = select_safe(rows.assign(design_worst=-0.4), 500)
    assert not safe and pick.design_avg == 0.012           # falls back to the plain 70% rule


def test_journal_records_once_and_scores_share_and_option_trades(tmp_path):
    from schwab_trader import journal
    path = tmp_path / "rec.csv"
    idx = pd.bdate_range("2026-01-05", periods=6)
    o = pd.DataFrame({"AAA": 100.0, "QQQ": 500.0}, index=idx)
    h, l, c = o * 1.005, o * 0.995, o.copy()
    h.iloc[2, 0] = 103.0                                     # +2% target fills on session 3
    c.iloc[1, 1] = 490.0                                     # QQQ closes at 490 on the spread's expiry
    px = {"open": o, "high": h, "low": l, "close": c}
    rows = [{"plan_date": "2026-01-05", "kind": "dip", "symbol": "AAA", "ref_price": 100, "target": 0.02, "stop": 0.15, "hold": 20},
            {"plan_date": "2026-01-05", "kind": "qqq_put_spread", "symbol": "QQQ", "model_credit": 1.0,
             "legs": {"expiry": "2026-01-06", "short_put": 495, "long_put": 485}}]
    assert journal.record(rows, path) == 2 and journal.record(rows, path) == 0
    df = journal.evaluate(px, path)
    share = df[df.kind == "dip"].iloc[0]
    assert share.status == "closed" and share.reason == "target" and share["return"] == pytest.approx(1.02 * 0.9995 - 1)
    opt = df[df.kind == "qqq_put_spread"].iloc[0]
    assert opt["return"] == pytest.approx((1.0 - 5.0) / 9.0)  # owed $5 of the $10 spread on a $1 credit
    tr = journal.track_record(df)
    assert tr["dip"]["wins"] == 1 and tr["qqq_put_spread"]["wins"] == 0


def test_learning_adopts_only_conditions_that_hurt_in_both_halves():
    from schwab_trader.learning import learn, size_factor, walk_forward
    rng = np.random.default_rng(0)
    n = 2000
    entry = pd.bdate_range("2012-01-02", periods=n)
    bad = rng.random(n) < 0.2                                # losing condition, all the way through
    early = (rng.random(n) < 0.2) & (np.arange(n) < n // 2)  # hurt only in the first half
    p_win = np.where(bad, 0.6, 0.85) - np.where(early, 0.3, 0)
    ret = np.where(rng.random(n) < p_win, 0.02, -0.05)
    f = pd.DataFrame({"entry": entry, "exit": entry + pd.Timedelta(days=10), "return": ret, "vix_high": bad,
                      "asia_down": early})
    rules = learn(f)
    assert "vix_high" in rules and "asia_down" not in rules
    wf = walk_forward(f, first_year=2015)
    assert wf["all_years"]["win_kept"] > wf["all_years"]["win_all"] and wf["use_live"]
    assert size_factor({"closed": 12, "wins": 6}, 0.85)[0] == 0.5    # 50% live vs 85% backtest
    assert size_factor({"closed": 12, "wins": 10}, 0.85)[0] == 1.0


def test_routine_tells_results_apart():
    from schwab_trader.routine import kind_of
    bar = {"begins_at": "2026-10-27T13:30:00Z"}
    assert kind_of({"data": {"results": [{"symbol": "A", "interval": "day", "bars": [bar]}]}}) == "daily"
    assert kind_of({"data": {"results": [{"symbol": "A", "interval": "30minute", "bounds": "24_5", "bars": [bar]}]}}) == "extended"
    assert kind_of({"data": {"results": [{"symbol": "A", "interval": "5minute", "bounds": "regular", "bars": [bar]}]}}) == "intraday"
    assert kind_of({"data": {"results": [{"instrument_id": "x", "symbol": "VIX", "interval": "day", "bars": [bar]}]}}) == "index_daily"
    assert kind_of({"data": {"results": [{"symbol": "A", "eps": {}, "report": {}}]}}) == "earnings"
    assert kind_of({"data": {"instruments": [{"id": "x"}]}}) == "option_instruments"
    assert kind_of({"data": {"results": [{"quote": {"instrument_id": "x"}}]}}) == "option_quotes"


def test_import_rescales_history_after_a_split():
    from schwab_trader.import_robinhood import rescale_for_split
    idx = pd.bdate_range("2026-01-05", periods=6)
    old = pd.DataFrame({"open": 200.0, "high": 202.0, "low": 198.0, "close": 200.0, "volume": 1000}, index=idx[:4])
    new = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 2000}, index=idx[2:])
    fixed = rescale_for_split(old, new)
    assert fixed["close"].iloc[0] == pytest.approx(100.0) and fixed["volume"].iloc[0] == 2000
    assert rescale_for_split(old, old.iloc[2:]).equals(old)       # no split: unchanged


def test_holdings_file_parses_symbols_and_shares(tmp_path):
    from schwab_trader.daily_report import holdings_from
    p = tmp_path / "h.txt"
    p.write_text("Holdings as of Oct 27\nAAPL 1,200\nmsft: 300\nBRK.B 50\ncash 25000 dollars\n")
    h = holdings_from(p)
    assert h == {"AAPL": 1200, "MSFT": 300, "BRK.B": 50}     # "Holdings" and "cash" lines are not stocks


def test_open_check_prices_the_condor_from_real_quotes(tmp_path, monkeypatch):
    import json
    from schwab_trader import journal, routine
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "rec.csv")
    monkeypatch.setattr(routine, "WORK", tmp_path)
    monkeypatch.setattr(routine, "INLINE", tmp_path / "inline")
    monkeypatch.setattr(routine, "RESULT_DIRS", [])
    routine.start("open-plan")
    today = pd.Timestamp.now(tz="America/New_York").date().isoformat()
    strikes = [722, 729, 708, 701]
    state = {"opens": {"QQQ": 715.0, "SPY": 760.0, "VIX": 17.8}, "verdict": {"prob": 0.05, "threshold": 0.08, "calm": True},
             "condor": {"strikes": strikes, "model_credit": 1.4, "fair_credit": 0.76, "model_max_loss": 565.0},
             "legs": [["QQQ", today, t, k, "x"] for t, k in zip(["call", "call", "put", "put"], strikes)]}
    (tmp_path / "open_state.json").write_text(json.dumps(state))
    inst = {"data": {"instruments": [{"id": f"id{k}", "chain_symbol": "QQQ", "expiration_date": today, "type": t,
                                      "strike_price": f"{k:.4f}"} for t, k in zip(["call", "call", "put", "put"], strikes)]}}
    bidask = {722: (1.10, 1.14), 729: (0.30, 0.33), 708: (1.20, 1.24), 701: (0.36, 0.40)}
    quotes = {"data": {"results": [{"quote": {"instrument_id": f"id{k}", "bid_price": str(b), "ask_price": str(a)}}
                                   for k, (b, a) in bidask.items()]}}
    (tmp_path / "inline" / "i.json").write_text(json.dumps(inst))
    (tmp_path / "inline" / "q.json").write_text(json.dumps(quotes))
    assert routine.open_build() == 0
    text = (tmp_path / "open.txt").read_text()
    assert "mid credit $1.6" in text and "sets with a limit order" in text     # 1.12-0.315+1.22-0.38 = 1.645
    rec = journal.load()
    assert rec.iloc[0].kind == "qqq_condor" and rec.iloc[0].real_credit == pytest.approx(1.645)


def test_short_engine_covers_at_target_and_buy_stop():
    from schwab_trader.accuracy_research import Exit, arrays
    from schwab_trader.short_research import simulate_short
    idx = pd.bdate_range("2026-02-02", periods=8)
    o = pd.DataFrame({"A": 100.0, "B": 100.0}, index=idx)
    h, l = o * 1.005, o * 0.995
    l.iloc[2, 0] = 96.5                                      # A falls 3%+ on session 3: covered at the limit
    h.iloc[1, 1] = 111.0                                     # B jumps 10%+ on session 2: bought back at the stop
    px = {"open": o, "high": h, "low": l, "close": o}
    ent = pd.DataFrame({"symbol": ["A", "B"], "entry": [idx[0], idx[0]]})
    tr = simulate_short(px, arrays(px, ["A", "B"]), ent, Exit(0.03, 0.10, 5), ["A", "B"], carry=0.0).set_index("symbol")
    assert tr.at["A", "reason"] == "target" and tr.at["A", "return"] == pytest.approx(0.03, abs=2e-3)
    assert tr.at["B", "reason"] == "stop" and tr.at["B", "return"] == pytest.approx(-0.10, abs=2e-3)


def test_email_holds_only_what_an_email_recommended(tmp_path, monkeypatch):
    from schwab_trader import journal
    from schwab_trader.daily_report import actions_section, only_recommended
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "rec.csv")
    journal.record([{"plan_date": "2026-10-28", "kind": "eps", "symbol": "PNR", "target": 0.02, "stop": 0.15, "hold": 20}])
    past = [{"symbol": "PNR", "entry": pd.Timestamp("2026-10-28")}, {"symbol": "INTC", "entry": pd.Timestamp("2026-10-09")}]
    assert [e["symbol"] for e in only_recommended(past, ["eps"])] == ["PNR"]      # INTC was never emailed: no HOLD line
    top = {"buys": ["AAA"], "shorts": ["ZZZ"], "sells": [], "covers": [], "holds": [], "short_holds": [], "options": []}
    lines = "\n".join(actions_section([{"symbol": "PNR", "tag": "B", "shares": 10}], top, [], [], ["JCI"], [], [], [],
                                      dt.date(2026, 10, 29)))
    assert "PNR [B] 10 sh" in lines and "SHORT:      ZZZ (top 5)" in lines and "HOLD:       JCI" in lines


def test_top5_ranks_by_rule_average_caps_each_rule_and_skips_open_picks():
    from schwab_trader.top5_research import pick_daily
    acc = {"dip": {"design": {"avg": 0.006}, "exit": {"target": 0.02, "stop": 0.15, "hold": 20}},
           "patterns": {"A": {"design_avg": 0.007, "exit": {"target": 0.03, "hold": 20}},
                        "B": {"design_avg": 0.004, "exit": {"target": 0.03, "hold": 20}},
                        "C": {"design_avg": 0.001, "exit": {"target": 0.03, "hold": 20}}}}   # C is below the 0.3% bar
    idx = pd.bdate_range("2026-03-02", periods=3)
    rows = [("A", s, 0.007, d) for s, d in (("S1", 5e9), ("S2", 4e9), ("S3", 3e9))]
    rows += [("B", s, 0.004, d) for s, d in (("S4", 9e9), ("S5", 1e9), ("S6", 2e9))]
    c = pd.DataFrame([{"date": idx[0], "symbol": s, "rule": r, "avg": a, "dollars": d} for r, s, a, d in rows])
    c = pd.concat([c, c.assign(date=idx[1])])
    p = pick_daily(c, acc, idx, n=5)
    day0 = p[p.date == idx[0]]
    assert list(day0.symbol) == ["S1", "S2", "S4", "S6"]       # 2 from A (best average), then 2 from B, most traded first
    assert list(p[p.date == idx[1]].symbol) == ["S3", "S5"]      # day-1 picks are still open; the two left over get in


def test_put_spreads_use_a_listed_expiry_and_skip_earnings():
    from schwab_trader.daily_report import listed_expiry, spread_lines
    start = dt.date(2026, 10, 29)                                       # a Thursday
    assert listed_expiry(start, 10, ["2026-11-20", "2026-12-18"]) == dt.date(2026, 11, 20)   # 16 sessions: within 8-20
    assert listed_expiry(start, 5, ["2026-11-20"]) is None                                   # too far from ~5 sessions
    assert listed_expiry(start, 5, ["2026-11-06", "2026-11-13"]) == dt.date(2026, 11, 6)
    pick = {"config": {"signal": "eps", "structure": "put_spread", "otm": 0.08, "width": 0.05, "dte": 10,
                       "take_profit": 0.5, "hold": 0}, "label": "eps: sell put spread"}
    rows = []
    lines = spread_lines("FLEX", 90.14, 0.35, pick, start, 1.0, 15_000, (), rows, "eps_put_spread", 0.8,
                         {"FLEX": ["2026-11-20"]})
    legs = rows[0]["legs"]
    assert "SELL Nov 20 $83 put / BUY $78 put" in lines[0] and legs["expiry"] == "2026-11-20" and legs["expiry_checked"]
    assert legs["alt"] == [85.0, 80.0] and legs["model"]["risk"] == 15_000      # the wider $5 strikes, in case
    rows = []
    lines = spread_lines("FLEX", 90.14, 0.35, pick, start, 1.0, 15_000, (), rows, "eps_put_spread", 0.8, {"FLEX": ["2026-12-18"]})
    assert "no listed expiry" in lines[0] and not rows
    rows = []
    up = [{"symbol": "FLEX", "date": "2026-11-05", "timing": "pm"}]
    lines = spread_lines("FLEX", 90.14, 0.35, pick, start, 1.0, 15_000, up, rows, "eps_put_spread", 0.8, {"FLEX": ["2026-11-20"]})
    assert "skip - it reports earnings 2026-11-05 pm" in lines[0] and not rows
    rows = []                                                          # chain not looked up yet: flagged for the routine
    spread_lines("FLEX", 90.14, 0.35, pick, start, 1.0, 15_000, (), rows, "eps_put_spread", 0.8, {})
    assert rows[0]["legs"]["expiry_checked"] is False


def test_position_past_its_stop_before_the_open_is_a_sell_at_the_open():
    from schwab_trader.daily_report import actions_section, position_lines
    idx = pd.bdate_range("2026-10-26", periods=4)
    pos = [{"symbol": "RDDT", "entry": idx[2], "px0": 134.98, "target_px": 139.03, "stop_px": 114.73, "i0": 2, "exit_i": None}]
    stopped = set()
    lines, sells, holds = position_lines("top-5 pick", pos, 0.03, 20, dt.date(2026, 10, 30), idx, [],
                                         prices={"RDDT": 102.5}, stopped=stopped)
    assert sells == ["RDDT"] and not holds and stopped == {"RDDT"}
    assert "SELL RDDT  at the open" in lines[0] and "past its $114.73 stop" in lines[0]
    _, sells, holds = position_lines("top-5 pick", pos, 0.03, 20, dt.date(2026, 10, 30), idx, [], prices={"RDDT": 130.0})
    assert holds == ["RDDT"] and not sells
    top = {"buys": [], "shorts": [], "sells": ["RDDT", "VLO"], "covers": [], "holds": [], "short_holds": [], "options": []}
    text = "\n".join(actions_section([], top, [], [], [], [], [], [], dt.date(2026, 10, 30), stopped))
    assert "RDDT (top 5) - at the open: already past its stop; VLO (top 5) - at the close" in text


def test_open_check_reads_the_vix_open_from_the_bar_ending_at_930(tmp_path):
    import json
    from schwab_trader import routine
    today = pd.Timestamp.now(tz="America/New_York").normalize()
    at = lambda h, m: (today + pd.Timedelta(hours=h, minutes=m)).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    vix = {"data": {"results": [{"instrument_id": routine.VIX_ID, "bars": [
        {"begins_at": at(8, 30), "open_value": "17.5", "close_value": "17.4"},
        {"begins_at": at(9, 0), "open_value": "17.4", "close_value": "17.2"}]}]}}
    qqq = {"data": {"results": [{"symbol": "QQQ", "bars": [{"begins_at": at(9, 30), "open_price": "700.5"}]}]}}
    (tmp_path / "v.json").write_text(json.dumps(vix))
    (tmp_path / "q.json").write_text(json.dumps(qqq))
    opens = routine._today_opens({"index_intraday": [str(tmp_path / "v.json")], "intraday": [str(tmp_path / "q.json")]})
    assert opens == {"VIX": 17.2, "QQQ": 700.5}


def test_open_check_falls_back_to_listed_strikes_and_skips_what_it_cant_place(tmp_path, monkeypatch):
    import json
    from schwab_trader import journal, routine
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "rec.csv")
    model = {"price": 90.14, "t": 16 / 252, "iv": 0.4, "iv_fair": 0.3, "risk": 15_000}
    base = {"plan_date": "2026-10-30", "kind": "eps_put_spread", "model_credit": 0.9, "max_loss": 400.0}
    journal.record([{**base, "symbol": "FLEX", "legs": {"expiry": "2026-11-20", "short_put": 83.0, "long_put": 78.0,
                                                          "alt": [85.0, 80.0], "contracts": 36, "min_credit": 0.5, "model": model}},
                    {**base, "symbol": "GEN", "legs": {"expiry": "2026-11-20", "short_put": 26.0, "long_put": 24.0,
                                                         "contracts": 86, "min_credit": 0.2, "model": model}},
                    {**base, "symbol": "VRT", "legs": {"expiry": "2026-11-13", "short_put": 217.5, "long_put": 205.0,
                                                         "contracts": 14, "min_credit": 1.94, "model": model}}])
    quotes = {("FLEX", 85.0): (1.10, 1.20), ("FLEX", 80.0): (0.40, 0.50), ("VRT", 217.5): (2.32, 2.73), ("VRT", 205.0): (0.84, 1.26)}
    leg_quote = lambda s, e, t, k: quotes.get((s, float(k)))
    j = journal.load()
    out = {r.symbol: routine.spread_check(j, i, r, leg_quote) for i, r in j.iterrows()}
    j = j.set_index("symbol")
    assert "take it: SELL the $85 put / BUY the $80 put" in out["FLEX"][0] and "aren't listed" in out["FLEX"][0]
    assert j.at["FLEX", "status"] == "open" and json.loads(j.at["FLEX", "legs"])["short_put"] == 85.0
    assert j.at["FLEX", "real_credit"] == pytest.approx(0.70)
    assert j.at["GEN", "status"] == "skipped" and j.at["GEN", "reason"] == "not listed"
    assert j.at["VRT", "status"] == "skipped" and j.at["VRT", "reason"] == "credit below floor"     # 1.475 < 1.94


def test_listed_expiries_come_from_standard_chains_only(tmp_path):
    import json
    from schwab_trader.routine import kind_of, listed_expiries
    chains = {"data": {"chains": [{"symbol": "MSTR", "trade_value_multiplier": "100.0000", "expiration_dates": ["2026-11-06"]},
                                  {"symbol": "MSTR", "trade_value_multiplier": "100.0000", "expiration_dates": ["2026-11-20"]},
                                  {"symbol": "MSTR1", "trade_value_multiplier": "50.0000", "expiration_dates": ["2026-11-13"]}]}}
    assert kind_of(chains) == "option_chains"
    (tmp_path / "c.json").write_text(json.dumps(chains))
    assert listed_expiries([str(tmp_path / "c.json")]) == {"MSTR": ["2026-11-06", "2026-11-20"]}


def test_top5_picks_are_sold_at_the_close_before_an_earnings_report(tmp_path, monkeypatch):
    from schwab_trader import journal
    from schwab_trader.daily_report import position_lines, replay, sell_day_before
    assert sell_day_before(dt.date(2026, 11, 4), "am") == dt.date(2026, 11, 3)      # before the open: the session before
    assert sell_day_before(dt.date(2026, 11, 4), "pm") == dt.date(2026, 11, 4)      # after the close: that day's close
    assert sell_day_before(dt.date(2026, 11, 9), "am") == dt.date(2026, 11, 6)      # Monday morning: Friday's close
    idx = pd.bdate_range("2026-11-02", periods=8)
    flat = pd.DataFrame({"A": 100.0}, index=idx)
    px = {"open": flat, "high": flat * 1.005, "low": flat * 0.995, "close": flat}
    ent = [{"symbol": "A", "entry": idx[0]}]
    assert replay(px, ent, 0.03, 20, 10, 0.15)                                       # still held without a report
    assert not replay(px, ent, 0.03, 20, 10, 0.15, sell_days={"A": {idx[3]}})        # sold at the close before it
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "rec.csv")
    journal.record([{"plan_date": "2026-11-02", "kind": "top5_buy", "symbol": "A", "target": 0.03, "stop": 0.15, "hold": 20},
                    {"plan_date": "2026-11-02", "kind": "eps", "symbol": "A", "target": 0.03, "stop": 0.15, "hold": 20}])
    events = pd.DataFrame({"symbol": ["A"], "day": [idx[4]]})                         # the market reacts on session 5
    rec = journal.evaluate(px, events=events).set_index("kind")
    assert rec.at["top5_buy", "reason"] == "earnings" and rec.at["top5_buy", "exit_date"] == idx[3].date().isoformat()
    assert rec.at["eps", "status"] == "open"                                          # the rule is the top-5 list's only
    early = journal.earnings_sell_index(pd.bdate_range("2026-10-26", periods=5), [3], 1)
    assert early is None                                                              # before the rule started: held through
    held = [{"symbol": "A", "entry": pd.Timestamp(idx[0]), "px0": 100.0, "target_px": 103.0, "stop_px": 85.0, "i0": 0, "exit_i": None}]
    up = [{"symbol": "A", "date": "2026-11-03", "timing": "pm"}]
    pre = set()
    lines, sells, _ = position_lines("top-5 pick", held, 0.03, 20, dt.date(2026, 11, 3), idx[:1], up, before_report=pre)
    assert sells == ["A"] and pre == {"A"} and "reports earnings 2026-11-03 pm" in lines[0]
    up = [{"symbol": "A", "date": "2026-11-05", "timing": "am"}]
    lines, _, holds = position_lines("top-5 pick", held, 0.03, 20, dt.date(2026, 11, 3), idx[:1], up, before_report=set())
    assert holds == ["A"] and "sell at the close on Wed Nov 04" in lines[0]
