"""Run the weekly strategy (active_strategy.py: trend-checked momentum top 3 over 59
stocks + 17 ETFs) on a Charles Schwab account. DRY RUN unless --live.

One-time setup
  1. Register at https://developer.schwab.com, create an app with the
     "Accounts and Trading Production" and "Market Data Production" APIs and the
     callback URL https://127.0.0.1:8182 . Approval can take a few days.
  2. export SCHWAB_API_KEY=...  SCHWAB_APP_SECRET=...
     optional: SCHWAB_CALLBACK_URL (default https://127.0.0.1:8182),
               SCHWAB_ACCOUNT_NUMBER (needed if you have more than one account)
  3. python -m schwab_trader.live_trader            # opens a browser to log in once
     Schwab refresh tokens expire after 7 days, so expect to log in again weekly.

Every run (Monday after 9:35 ET, matching the backtest's Monday-open fills)
  python -m schwab_trader.live_trader               # show the plan, send nothing
  python -m schwab_trader.live_trader --live        # send orders after you type YES

Safety rails: manages only the strategy's stocks/ETFs (anything else you own is left
alone), never uses margin (buys are capped by settled-or-available cash), whole
shares and marketable LIMIT day orders only, sells fill before buys, one
rebalance per week unless --force, and every action is logged to
~/.schwab_trader/trades.log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import pandas as pd

from .active_strategy import LABEL, POOL, market_trend, momentum, target_weights
from .config import BENCHMARK, STATE_DIR, Costs
from .data import drop_unfinished_today, fetch_schwab
from .strategy import whole_share_targets

TOKEN_PATH = STATE_DIR / "token.json"
TRADE_LOG = STATE_DIR / "trades.log"
REBALANCE_STATE = STATE_DIR / "last_rebalance.json"
LIMIT_OFFSET = 0.002   # buy limit 0.2% above the ask, sell limit 0.2% below the bid: fills like market, capped


def make_client():
    from schwab import auth

    try:
        api_key, secret = os.environ["SCHWAB_API_KEY"], os.environ["SCHWAB_APP_SECRET"]
    except KeyError:
        sys.exit("Set SCHWAB_API_KEY and SCHWAB_APP_SECRET (see the module docstring).")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return auth.easy_client(api_key, secret, os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
                            str(TOKEN_PATH))


def account_hash(client) -> str:
    resp = client.get_account_numbers()
    resp.raise_for_status()
    accounts = resp.json()
    wanted = os.environ.get("SCHWAB_ACCOUNT_NUMBER")
    if wanted:
        for a in accounts:
            if a["accountNumber"] == wanted:
                return a["hashValue"]
        sys.exit("SCHWAB_ACCOUNT_NUMBER not found among your accounts.")
    if len(accounts) != 1:
        masked = ", ".join("..." + a["accountNumber"][-4:] for a in accounts)
        sys.exit(f"Several accounts ({masked}); set SCHWAB_ACCOUNT_NUMBER to pick one.")
    return accounts[0]["hashValue"]


def read_account(client, acct_hash: str) -> dict:
    resp = client.get_account(acct_hash, fields=[client.Account.Fields.POSITIONS])
    resp.raise_for_status()
    sa = resp.json()["securitiesAccount"]
    bal = sa.get("currentBalances", {})
    # Cash accounts report cashAvailableForTrading; margin accounts get plain cash
    # (never buying power), so the bot cannot borrow.
    cash = bal.get("cashAvailableForTrading", bal.get("cashBalance", 0.0))
    positions = {}
    for p in sa.get("positions", []):
        qty = p.get("longQuantity", 0) - p.get("shortQuantity", 0)
        if qty:
            positions[p["instrument"]["symbol"]] = qty
    return {"type": sa.get("type"), "cash": float(cash), "value": float(bal.get("liquidationValue", 0.0)),
            "positions": positions}


def get_quotes(client, symbols) -> dict:
    resp = client.get_quotes(list(symbols))
    resp.raise_for_status()
    out = {}
    for sym, q in resp.json().items():
        quote = q.get("quote", {})
        last = quote.get("lastPrice") or quote.get("closePrice")
        out[sym] = {"bid": quote.get("bidPrice") or last, "ask": quote.get("askPrice") or last, "last": last}
    return out


def market_is_open(client, now: dt.datetime | None = None) -> bool:
    now = now or pd.Timestamp.now(tz="America/New_York")
    resp = client.get_market_hours([client.MarketHours.Market.EQUITY], date=now.date())
    resp.raise_for_status()
    for market in resp.json().get("equity", {}).values():
        if not market.get("isOpen"):
            continue
        for session in market.get("sessionHours", {}).get("regularMarket", []):
            if pd.Timestamp(session["start"]) <= now <= pd.Timestamp(session["end"]):
                return True
    return False


def plan_orders(weights: dict, account: dict, quotes: dict, universe, costs: Costs = Costs(),
                max_capital: float | None = None) -> tuple[list, dict]:
    """Whole-share orders that move the strategy's ETFs to the target weights.

    Returns (orders, info); each order is {"symbol", "side", "qty", "limit"}.
    """
    managed = {s: q for s, q in account["positions"].items() if s in universe}
    mid = {s: (quotes[s]["bid"] + quotes[s]["ask"]) / 2 for s in quotes}
    budget = account["cash"] + sum(q * mid[s] for s, q in managed.items())
    if max_capital is not None:
        budget = min(budget, max_capital)
    buy_px = {s: round(quotes[s]["ask"] * (1 + LIMIT_OFFSET), 2) for s in weights}
    target = whole_share_targets(weights, budget, buy_px, costs.cash_buffer)

    orders = []
    for sym in sorted(set(managed) | set(target)):
        diff = int(target.get(sym, 0) - managed.get(sym, 0))
        keeps = managed.get(sym, 0) > 0 and target.get(sym, 0) > 0
        if not diff or (keeps and abs(diff) * mid[sym] < costs.min_trade_value * budget):
            continue
        if diff < 0:
            orders.append({"symbol": sym, "side": "SELL", "qty": -diff,
                           "limit": round(quotes[sym]["bid"] * (1 - LIMIT_OFFSET), 2)})
        else:
            orders.append({"symbol": sym, "side": "BUY", "qty": diff, "limit": buy_px[sym]})
    orders.sort(key=lambda o: (o["side"] != "SELL", list(weights).index(o["symbol"]) if o["symbol"] in weights else 0))
    return orders, {"budget": budget, "target": target, "managed": managed}


def _log(event: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds"), **event}) + "\n")


def _wait_for_fills(client, acct_hash: str, order_ids: list, timeout: float = 90.0) -> dict:
    status = {}
    deadline = time.time() + timeout
    while order_ids and time.time() < deadline:
        for oid in list(order_ids):
            resp = client.get_order(oid, acct_hash)
            resp.raise_for_status()
            status[oid] = resp.json().get("status")
            if status[oid] in ("FILLED", "REJECTED", "CANCELED", "EXPIRED"):
                order_ids.remove(oid)
        if order_ids:
            time.sleep(3)
    return status


def execute(client, acct_hash: str, orders: list, account_refresh) -> list:
    """Send sells, wait for fills, then send buys sized to the cash actually available."""
    from schwab.orders.equities import equity_buy_limit, equity_sell_limit
    from schwab.utils import Utils

    utils = Utils(client, acct_hash)
    results = []
    sells = [o for o in orders if o["side"] == "SELL"]
    buys = [o for o in orders if o["side"] == "BUY"]
    ids = []
    for o in sells:
        resp = client.place_order(acct_hash, equity_sell_limit(o["symbol"], o["qty"], f"{o['limit']:.2f}"))
        oid = utils.extract_order_id(resp)
        ids.append(oid)
        results.append({**o, "order_id": oid})
        _log({"action": "place", **o, "order_id": oid})
    if ids:
        for oid, st in _wait_for_fills(client, acct_hash, ids).items():
            _log({"action": "status", "order_id": oid, "status": st})
    cash = account_refresh()["cash"]
    for o in buys:
        qty = min(o["qty"], int(cash // o["limit"]))
        if qty <= 0:
            _log({"action": "skip", **o, "reason": "not enough cash"})
            continue
        resp = client.place_order(acct_hash, equity_buy_limit(o["symbol"], qty, f"{o['limit']:.2f}"))
        oid = utils.extract_order_id(resp)
        cash -= qty * o["limit"]
        results.append({**o, "qty": qty, "order_id": oid})
        _log({"action": "place", **o, "qty": qty, "order_id": oid})
    return results


def rebalance_done_this_week(today: dt.date) -> bool:
    if not REBALANCE_STATE.exists():
        return False
    last = dt.date.fromisoformat(json.loads(REBALANCE_STATE.read_text())["date"])
    return last.isocalendar()[:2] == today.isocalendar()[:2]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="actually send orders (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation (for scheduled runs)")
    ap.add_argument("--force", action="store_true", help="rebalance even if already done this week")
    ap.add_argument("--max-capital", type=float, default=None, help="cap the dollars the bot may manage")
    args = ap.parse_args(argv)
    client = make_client()
    acct = account_hash(client)
    account = read_account(client, acct)
    if account["type"] == "MARGIN":
        print("Note: margin account. Under $25k the pattern-day-trader rule applies; this bot never day-trades "
              "and never borrows, but a CASH account is the simpler choice.")

    symbols = list(dict.fromkeys(list(POOL) + [BENCHMARK]))
    start = (dt.date.today() - dt.timedelta(days=600)).isoformat()
    frames = {s: drop_unfinished_today(df) for s, df in fetch_schwab(client, symbols, start).items()}
    closes = pd.DataFrame({s: f["close"] for s, f in frames.items()}).sort_index()
    managed_now = {s: q for s, q in account["positions"].items() if s in POOL}
    held_quotes = get_quotes(client, set(managed_now)) if managed_now else {}
    budget = account["cash"] + sum(q * (held_quotes[s]["bid"] + held_quotes[s]["ask"]) / 2
                                   for s, q in managed_now.items())
    if args.max_capital is not None:
        budget = min(budget, args.max_capital)
    ranked = list(momentum(closes).index[:15])
    quotes = {**held_quotes, **get_quotes(client, set(ranked) - set(held_quotes))}
    weights = target_weights(closes, budget, lambda s: quotes.get(s, {}).get("ask", float("nan")))
    orders, info = plan_orders(weights, account, quotes, POOL, max_capital=args.max_capital)

    print(f"Signal as of {closes.index[-1].date()} ({LABEL}); market {market_trend(closes)}")
    print("  ranking: " + ", ".join(f"{s} {v:+.0%}" for s, v in momentum(closes).head(6).items()))
    print(f"  target: {info['target'] or 'all cash'}")
    print(f"  account cash ${account['cash']:,.2f}, strategy budget ${info['budget']:,.2f}, "
          f"current strategy holdings {info['managed'] or 'none'}")
    if not orders:
        print("No trades needed.")
        return
    print("Planned orders:")
    for o in orders:
        print(f"  {o['side']:4s} {o['qty']:>4d} {o['symbol']:5s} limit ${o['limit']:,.2f}  (~${o['qty'] * o['limit']:,.2f})")

    if not args.live:
        print("\nDRY RUN - nothing sent. Re-run with --live to place these orders.")
        return
    today = dt.date.today()
    if rebalance_done_this_week(today) and not args.force:
        sys.exit("Already rebalanced this week (use --force to override).")
    if not market_is_open(client):
        sys.exit("Market is closed - run during regular hours (Monday after 9:35 ET).")
    if not args.yes and input(f"Type YES to send {len(orders)} order(s): ").strip() != "YES":
        sys.exit("Cancelled.")
    results = execute(client, acct, orders, lambda: read_account(client, acct))
    REBALANCE_STATE.write_text(json.dumps({"date": today.isoformat(), "orders": results}))
    print("Sent:", json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
