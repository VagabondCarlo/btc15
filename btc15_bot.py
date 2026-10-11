#!/usr/bin/env python3
"""Polymarket US BTC 15-minute up/down bot.

Paper mode by default. Real orders need LIVE=1 in the environment AND typing
YES at startup. Without both it cannot spend money.

What it does each window: watch the gap between BRTI and the price to beat,
and when the gap is in the band the data says is profitable, rest a limit
order inside the spread. Stake is a fraction of the current balance, so wins
compound. The fraction comes from Kelly on the measured edge, cut to a
quarter, and capped.

What it will not do: buy large gaps. A $100 move with three minutes left is
worth about 98% and the market charges 99. Every observed price in that zone
loses money, so the bot refuses those outright rather than sizing them small.

Usage
  ./run_bot.sh                 paper, prints every decision
  LIVE=1 ./run_bot.sh          real orders, asks for confirmation first
  touch STOP                   stops new entries immediately
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from btc15 import state as botstate
from btc15.gateway import (Gateway, WINDOW_SECS, event_slug, parse_terms,
                           twap_at)
from btc15.strategy import (Config, PROFILES, Risk, evaluate, profile,
                            should_exit, stake_for, working_balance)
from btc15.twap import blended_sigma, forecast_settlement

STOP_FILE = Path("STOP")
RUNNING = True

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER, window_start INTEGER, market_slug TEXT,
    side TEXT, limit_price REAL, shares REAL, stake REAL,
    win_rate REAL, model_p REAL, edge REAL, gap REAL, secs_left REAL,
    mode TEXT, status TEXT, fill_price REAL, fee REAL,
    outcome TEXT, pnl REAL, balance_after REAL, venue_order_id TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER, window_start INTEGER, secs_left REAL, gap REAL,
    best_bid REAL, best_ask REAL, model_p REAL, took INTEGER, reason TEXT
);
"""


def _stop(*_):
    global RUNNING
    RUNNING = False
    print("\nshutting down after this window...", flush=True)


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money(d, k):
    v = d.get(k)
    return f(v.get("value")) if isinstance(v, dict) else None


def ladder(levels):
    out = []
    for lv in levels or []:
        p, q = money(lv, "px"), f(lv.get("qty"))
        if p is not None:
            out.append((p, q or 0.0))
    return out


class PaperBroker:
    """Simulates resting a limit order against the real book.

    A resting buy at L fills when a seller is willing to transact at or below
    L, which shows up as the best offer touching L. That is a conservative
    reading: it ignores queue position, so treat paper fills as the optimistic
    case and expect fewer in reality.
    """

    name = "paper"

    def __init__(self, balance: float) -> None:
        self.balance = balance

    def would_fill(self, side: str, limit: float, bids, offers) -> bool:
        if side == "UP":
            best_ask = min((p for p, _ in offers), default=None)
            return best_ask is not None and best_ask <= limit
        # DOWN is the mirror: our DOWN bid at L is an UP offer at 1-L
        best_bid = max((p for p, _ in bids), default=None)
        return best_bid is not None and (1.0 - best_bid) <= limit

    def settle(self, shares: float, cost: float, won: bool) -> float:
        pnl = (shares - cost) if won else -cost
        self.balance += pnl
        return pnl


class LiveBroker:
    """Real orders. UNTESTED against the venue: no API keys were available
    when this was written, so the request shape follows the documented
    schema and the first live order must be checked against the app by hand
    before leaving the bot unattended."""

    name = "live"

    def __init__(self, balance: float, cfg: Config) -> None:
        from btc15.auth import Credentials, Signer
        from btc15.client import PolymarketUS, load_overrides
        from pathlib import Path
        self.api = PolymarketUS(Signer(Credentials.from_env()),
                                overrides=load_overrides(
                                    Path("data/endpoints.json")))
        self.balance = balance
        self.cfg = cfg

    def place(self, market_slug: str, side: str, limit: float, shares: float):
        # price.value is always quoted on the Up side; Down uses 1 - price.
        px = limit if side == "UP" else round(1.0 - limit, 2)
        body = {
            "marketSlug": market_slug,
            "intent": "ORDER_INTENT_BUY_LONG" if side == "UP"
                      else "ORDER_INTENT_BUY_SHORT",
            "timeInForce": self.cfg.time_in_force,
            "price": {"value": f"{px:.2f}", "currency": "USD"},
            "quantity": f"{shares:.2f}",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": False,   # a resting order is not immediate
        }
        return self.api.request("POST", "/v1/orders", body=body)

    def cancel(self, order_id: str):
        """Pull an order that never filled. Leaving it live into settlement
        risks being filled at the worst possible moment."""
        return self.api.request("DELETE", f"/v1/orders/{order_id}")

    def order_status(self, order_id: str):
        return self.api.request("GET", f"/v1/orders/{order_id}")

    def fetch_balance(self) -> float | None:
        try:
            r = self.api.request("GET", "/v1/account/balances")
            for d in (r if isinstance(r, list) else [r]):
                for k in ("buyingPower", "availableBalance", "cash"):
                    v = money(d, k) if isinstance(d, dict) else None
                    if v is not None:
                        return v
        except Exception:
            return None
        return None


def run(cfg: Config, db_path: str, balance: float, live: bool,
        poll_hz: float) -> int:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    botstate.init(con)
    gw = Gateway()
    risk = Risk(cfg)

    # A drifting clock silently corrupts everything: signed requests are
    # rejected past 30 seconds, and settlement is averaged over exact
    # one-second slots. Check before doing anything else.
    try:
        skew = botstate.clock_skew()
        print(f"clock skew {skew:+.2f}s", end="")
        if abs(skew) > 20:
            print("  REFUSING TO START. Fix the clock: "
                  "sudo timedatectl set-ntp true")
            return 2
        print("  ok")
    except Exception as e:
        print(f"could not check the clock ({e}); continuing")

    mode = "live" if live else "paper"
    broker = LiveBroker(balance, cfg) if live else PaperBroker(balance)
    saved_bal, open_order = botstate.load(con, risk, mode)
    if saved_bal is not None:
        broker.balance = saved_bal
    if live:
        real = broker.fetch_balance()
        if real is None:
            print("could not read the account balance from the venue. "
                  "Run preflight.py first.")
            return 2
        if abs(real - broker.balance) > 0.01:
            print(f"  venue says ${real:.2f}, using that")
        broker.balance = real
    if risk.s.halted:
        print(f"HALTED: {risk.s.halted}")
        print("Clear it by deleting the bot_state row, once you know why.")
        return 3
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if cfg.bankroll and not risk.s.equity:
        risk.s.equity = cfg.bankroll
    work = working_balance(broker.balance, cfg, risk.s.equity or None)
    if cfg.bankroll:
        print(f"ring-fence: the bot has ${work:.2f} of the ${broker.balance:.2f} "
              f"in the account.")
        print(f"            ${broker.balance - work:.2f} is reserved and the "
              f"bot cannot reach it, win or lose.")
    if cfg.bankroll and work < broker.balance:
        print(f"mode={broker.name}  account ${broker.balance:.2f}, "
              f"bot may use ${work:.2f} (ring-fenced)")
    else:
        print(f"mode={broker.name}  balance=${broker.balance:.2f}  "
          f"{'takes the offer' if not cfg.maker else 'rests below the market'}")
    print(f"entry: gap ${cfg.gap_min:.0f}-${cfg.gap_max:.0f}, "
          f"{cfg.entry_secs_min}-{cfg.entry_secs_max}s left, "
          f"min edge {cfg.min_edge * 100:.0f}c, "
          f"stake <= {cfg.max_fraction:.0%} of balance")
    print(f"no trade cap. after a loss: sit out {cfg.cooldown_windows} "
          f"windows (doubling to {cfg.cooldown_max} on a streak), review, "
          f"resume.")
    if cfg.bankroll:
        print(f"stop: {cfg.hard_floor_frac:.0%} of peak, and once it reaches "
              f"${cfg.bankroll * cfg.profit_lock_at:.2f} it keeps "
              f"{cfg.profit_lock_keep:.0%} of every gain above "
              f"${cfg.bankroll:.2f}.\n")
    else:
        print(f"hard stop only below {cfg.hard_floor_frac:.0%} of peak.\n")
    # The peak must track the RING-FENCED balance, not the account. Seeding
    # it with the raw account balance made the profit ratchet compute a floor
    # from money the bot could never touch: with $25 in the account and $10
    # ring-fenced, it locked a floor of $17.50 and halted on the first
    # decision, then stayed halted. It logged 153,334 refusals over four days
    # and took no trades.
    risk.note_balance(working_balance(broker.balance, cfg,
                                      risk.s.equity or None))

    # A position restored from disk whose window has already ended must be
    # settled before anything else, or its result is lost and the next
    # window opens a second position on top of it.
    if open_order and open_order.get("we", 0) < time.time():
        print("  settling a position left over from the last run")
        try:
            _resolve(con, gw, broker, risk, open_order, {})
        except Exception as e:
            print(f"  could not settle it: {e}")
        open_order = None
        botstate.save(con, broker.balance, risk, None, mode)

    cur_ws = None
    terms = None
    entries_this_window = 0
    last_terms_try = 0.0
    last_clock_check = time.time()
    prints: dict[int, float] = {}
    sigma = float("nan")
    open_order: dict | None = None
    traded_this_window = False

    while RUNNING:
        loop = time.time()
        now = int(loop)
        ws = now // WINDOW_SECS * WINDOW_SECS
        risk.note_balance(working_balance(broker.balance, cfg,
                                          risk.s.equity or None))

        if ws != cur_ws:
            if open_order:
                _resolve(con, gw, broker, risk, open_order, prints)
                open_order = None
            cur_ws = ws
            traded_this_window = False
            entries_this_window = 0
            last_entry_at = 0.0
            if live and not open_order:
                real = broker.fetch_balance()
                if real is not None:
                    if abs(real - broker.balance) > 0.01:
                        print(f"   balance now ${real:.2f} "
                              f"(was tracking ${broker.balance:.2f})")
                    broker.balance = real
            if time.time() - last_clock_check > 3600:
                last_clock_check = time.time()
                try:
                    sk = botstate.clock_skew()
                    if abs(sk) > 20:
                        print(f"   clock drifted to {sk:+.1f}s, stopping")
                        risk.s.halted = f"clock drift {sk:+.1f}s"
                        botstate.save(con, broker.balance, risk, open_order, mode)
                        return 2
                except Exception:
                    pass
            botstate.save(con, broker.balance, risk, open_order, mode)
            d = datetime.fromtimestamp(ws, timezone.utc)
            try:
                ev = gw.event(event_slug(d))
                terms = parse_terms(ev) if ev else None
                # Long window first so the blend has a long-run anchor.
                prints = gw.index_range(ws - 3500, now)
                vals = np.array([prints[t] for t in sorted(prints)], float)
                sigma = blended_sigma(vals[-600:], vals,
                                      cfg.vol_shrink) * cfg.vol_safety
            except Exception as e:
                print(f"  window setup failed: {e}", flush=True)
                terms = None
            print(f"[{d:%H:%M}z] strike "
                  f"{terms['price_to_beat'] if terms else 'pending'}  "
                  f"bal ${broker.balance:.2f}", flush=True)

        # priceToBeat is published a few seconds after the window opens, so
        # the first fetch at the boundary usually returns null. Keep asking
        # until it appears instead of sitting out the whole window.
        if not terms or terms.get("price_to_beat") is None:
            if loop - last_terms_try > 3.0:
                last_terms_try = loop
                try:
                    d = datetime.fromtimestamp(ws, timezone.utc)
                    ev = gw.event(event_slug(d))
                    t2 = parse_terms(ev) if ev else None
                    if t2 and t2.get("price_to_beat") is not None:
                        terms = t2
                        print(f"   strike published: "
                              f"{terms['price_to_beat']}", flush=True)
                except Exception:
                    pass
            if not terms or terms.get("price_to_beat") is None:
                time.sleep(1.0)
                continue

        # Do not trust the published strike. Every trade is priced off the
        # distance from it, so a stale or wrong value poisons everything
        # downstream silently. We hold the same per-second index prints the
        # venue averages, so reconstruct it and compare. A cent of drift is
        # normal rounding; dollars mean something is wrong.
        if not terms.get("_strike_checked"):
            ours, n = twap_at(prints, ws)
            if ours is None or n < 55:
                if loop - ws > 90:
                    print(f"   only {n}/60 index prints for the strike; "
                          f"sitting out this window", flush=True)
                    terms["_strike_checked"] = "no data"
                    terms["price_to_beat"] = None
                time.sleep(1.0)
                continue
            diff = abs(ours - terms["price_to_beat"])
            terms["_strike_checked"] = True
            if diff > 1.0:
                print(f"   published strike {terms['price_to_beat']} but our "
                      f"own average of the same 60 prints is {ours} "
                      f"(${diff:.2f} apart). Sitting out this window.",
                      flush=True)
                terms["price_to_beat"] = None
                continue
            if diff > 0.02:
                print(f"   strike check: ours {ours} vs theirs "
                      f"{terms['price_to_beat']}, ${diff:.2f} apart",
                      flush=True)

        we = ws + WINDOW_SECS
        secs_left = we - loop

        try:
            prints.update(gw.index_candles(now - 10, now))
            md = (gw._get(f"/v1/markets/{terms['market_slug']}/book")
                  or {}).get("marketData") or {}
        except Exception:
            time.sleep(1.0)
            continue

        brti = prints.get(max(prints)) if prints else None
        if brti is None:
            time.sleep(1.0)
            continue
        gap = brti - terms["price_to_beat"]
        bids, offers = ladder(md.get("bids")), ladder(md.get("offers"))
        bb = max((p for p, _ in bids), default=None)
        ba = min((p for p, _ in offers), default=None)

        model_p = None
        if sigma == sigma and sigma > 0:
            win = [prints[t] for t in range(we - 60, now + 1) if t in prints]
            model_p = forecast_settlement(np.asarray(win), secs_left, brti,
                                          sigma).prob_up(terms["price_to_beat"])

        # An order already resting: see if the market came to us.
        if open_order:
            # Holding a filled position: check whether it is still worth having.
            if open_order["filled"] and model_p is not None:
                side = open_order["side"]
                fair = model_p if side == "UP" else 1.0 - model_p
                held = time.time() - open_order.get("fill_ts", loop)
                why = should_exit(fair, secs_left, held, cfg)
                if why:
                    out_bid = bb if side == "UP" else (
                        (1.0 - ba) if ba is not None else None)
                    if out_bid and out_bid > 0.01:
                        _close_early(con, broker, risk, open_order,
                                     out_bid, why)
                        open_order = None
                        botstate.save(con, broker.balance, risk, None, mode)
                        time.sleep(0.3)
                        continue
            if broker.name == "live" and not open_order["filled"]:
                try:
                    st = broker.order_status(open_order["venue_id"])
                    done = _filled_qty(st)
                    if done and done > 0:
                        # A partial fill leaves you holding less than you
                        # sized for. Book what actually filled, not what was
                        # asked for, or every downstream number is wrong.
                        if done < open_order["shares"] - 1e-9:
                            print(f"   partial fill {done:.2f} of "
                                  f"{open_order['shares']:.2f}", flush=True)
                            open_order["cost"] *= done / open_order["shares"]
                            open_order["shares"] = done
                        open_order["filled"] = True
                        open_order["fill_ts"] = time.time()
                        print("   venue reports filled", flush=True)
                except Exception as e:
                    print(f"   could not read order status: {e}", flush=True)
            if broker.name == "paper" and not open_order["filled"]:
                if broker.would_fill(open_order["side"],
                                     open_order["limit"], bids, offers):
                    open_order["filled"] = True
                    open_order["fill_ts"] = time.time()
                    print(f"   filled {open_order['side']} "
                          f"{open_order['shares']:.2f} @ "
                          f"{open_order['limit']:.2f}", flush=True)
            if (not open_order["filled"] and broker.name == "live"
                    and secs_left <= cfg.cancel_before_expiry
                    and open_order.get("venue_id")):
                try:
                    broker.cancel(open_order["venue_id"])
                    print("   unfilled, order cancelled", flush=True)
                except Exception as e:
                    print(f"   cancel failed: {e}", flush=True)
                open_order["cancelled"] = True
            if secs_left <= 1:
                _resolve(con, gw, broker, risk, open_order, prints)
                open_order = None
                botstate.save(con, broker.balance, risk, None, mode)
            time.sleep(max(0.1, 1.0 / poll_hz - (time.time() - loop)))
            continue

        blocked = ((STOP_FILE.exists() and "STOP file present")
                   or risk.blocked(working_balance(broker.balance, cfg,
                                                    risk.s.equity or None), ws)
                   or (traded_this_window and cfg.one_trade_per_window
                       and "already traded this window")
                   or (entries_this_window >= cfg.max_entries_per_window
                       and f"{entries_this_window} entries already this "
                           f"window")
                   or (last_entry_at
                       and loop - last_entry_at < cfg.entry_spacing_secs
                       and f"last entry {loop - last_entry_at:.0f}s ago, "
                           f"spacing is {cfg.entry_spacing_secs}s"))

        sig = evaluate(gap, secs_left, bb, ba, model_p, cfg,
                       min_edge=risk.min_edge())
        if (sig.take and ba is not None and sig.limit_price is not None
                and sig.limit_price >= ba - 1e-9):
            # This is a cross, not a rest. At small size the taker fee rounds
            # up to a whole cent and takes a third of the edge with it.
            probe = stake_for(
                working_balance(broker.balance, cfg, risk.s.equity or None),
                sig.win_rate, sig.limit_price, Config(**{**cfg.__dict__,
                                                        "bankroll": None}))
            if probe / max(sig.cost, 1e-9) < cfg.maker_only_below_contracts:
                sig = type(sig)(False,
                                f"would cross at {sig.limit_price:.2f} for "
                                f"only {probe / max(sig.cost, 1e-9):.2f} "
                                f"contracts; fee rounding eats the edge",
                                side=sig.side, limit_price=sig.limit_price,
                                win_rate=sig.win_rate, edge=sig.edge,
                                cost=sig.cost)
        con.execute("""INSERT INTO decisions(ts_ms,window_start,secs_left,gap,
                       best_bid,best_ask,model_p,took,reason) VALUES
                       (?,?,?,?,?,?,?,?,?)""",
                    (int(loop * 1000), ws, secs_left, gap, bb, ba, model_p,
                     int(bool(sig.take and not blocked)),
                     blocked or sig.reason))
        con.commit()

        if sig.take and not blocked:
            stake = stake_for(
                working_balance(broker.balance, cfg, risk.s.equity or None),
                sig.win_rate, sig.limit_price, Config(**{**cfg.__dict__,
                                                        "bankroll": None}))
            min_qty = terms.get("min_qty") or 0.01
            shares = stake / sig.cost if sig.cost else 0.0
            shares = round(shares - (shares % min_qty), 2)
            if shares < min_qty or stake < cfg.min_stake:
                print(f"   {sig.reason} but balance too small "
                      f"(${stake:.2f} buys {shares:.2f} contracts)", flush=True)
            else:
                cost = shares * sig.cost
                print(f"   {secs_left:.0f}s gap ${gap:+.0f}  -> {sig.reason}, "
                      f"{shares:.2f} contracts, ${cost:.2f}", flush=True)
                venue_id = None
                if broker.name == "live":
                    try:
                        r = broker.place(terms["market_slug"], sig.side,
                                         sig.limit_price, shares)
                        venue_id = str((r or {}).get("orderId") or "")
                    except Exception as e:
                        print(f"   order rejected: {e}", flush=True)
                        time.sleep(1.0)
                        continue
                open_order = {
                    "ws": ws, "slug": terms["market_slug"], "side": sig.side,
                    "limit": sig.limit_price, "shares": shares, "cost": cost,
                    "win_rate": sig.win_rate, "model_p": sig.model_p,
                    "edge": sig.edge, "gap": gap, "secs_left": secs_left,
                    "ptb": terms["price_to_beat"], "we": we,
                    "filled": False, "venue_id": venue_id,
                    "fill_ts": time.time(),
                }
                traded_this_window = True
                entries_this_window += 1
                last_entry_at = time.time()
                botstate.save(con, broker.balance, risk, open_order, mode)

        time.sleep(max(0.1, 1.0 / poll_hz - (time.time() - loop)))

    botstate.save(con, broker.balance, risk, open_order, mode)
    if open_order and open_order.get("filled"):
        print("note: a position is still open; it will be settled on restart")
    print(f"\nfinal balance ${broker.balance:.2f}")
    print(f"{risk.summary()}")
    con.close()
    return 0


def _filled_qty(status) -> float | None:
    """Contracts actually filled, from an order status payload."""
    def walk(o):
        if isinstance(o, dict):
            yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    for d in walk(status):
        for k in ("filledQuantity", "filled_qty", "cumQty", "executedQuantity",
                  "filledQty"):
            if k in d:
                try:
                    return float(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def _close_early(con, broker, risk: Risk, order: dict, bid: float,
                 why: str) -> None:
    """Sell out before settlement and book the result."""
    from btc15.fees import taker_fee
    proceeds = order["shares"] * bid - taker_fee(order["shares"], bid)
    pnl = proceeds - order["cost"]
    broker.balance += pnl
    if risk.s.equity:
        risk.s.equity += pnl
    note = risk.record(order["win_rate"], pnl > 0, order["ws"])
    con.execute("""INSERT INTO orders(ts_ms,window_start,market_slug,side,
        limit_price,shares,stake,win_rate,model_p,edge,gap,secs_left,mode,
        status,fill_price,outcome,pnl,balance_after)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time() * 1000), order["ws"], order["slug"], order["side"],
         order["limit"], order["shares"], order["cost"], order["win_rate"],
         order["model_p"], order["edge"], order["gap"], order["secs_left"],
         broker.name, "closed early", bid, "EXIT", pnl, broker.balance))
    con.commit()
    print(f"   exit: {why} sold at {bid:.2f} for ${pnl:+.2f}  "
          f"balance ${broker.balance:.2f}", flush=True)
    if note:
        print(f"   review: {note}", flush=True)


def _resolve(con, gw: Gateway, broker, risk: Risk, order: dict, prints) -> None:
    """Settle a finished window and book the result."""
    if not order.get("filled"):
        con.execute("""INSERT INTO orders(ts_ms,window_start,market_slug,side,
            limit_price,shares,stake,win_rate,model_p,edge,gap,secs_left,mode,
            status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(time.time() * 1000), order["ws"], order["slug"], order["side"],
             order["limit"], order["shares"], order["cost"], order["win_rate"],
             order["model_p"], order["edge"], order["gap"], order["secs_left"],
             broker.name, "unfilled"))
        con.commit()
        print("   not filled, no position", flush=True)
        return

    outcome = None
    for _ in range(6):
        try:
            d = datetime.fromtimestamp(order["ws"], timezone.utc)
            t = parse_terms(gw.event(event_slug(d)) or {})
            if t and t.get("settlement") is not None:
                outcome = "UP" if t["settlement"] >= t["price_to_beat"] else "DOWN"
                break
        except Exception:
            pass
        time.sleep(5)
    if outcome is None:
        print("   settlement not published yet; left open", flush=True)
        return

    won = outcome == order["side"]
    pnl = broker.settle(order["shares"], order["cost"], won)
    if risk.s.equity:
        risk.s.equity += pnl
    note = risk.record(order["win_rate"], won, order["ws"])
    con.execute("""INSERT INTO orders(ts_ms,window_start,market_slug,side,
        limit_price,shares,stake,win_rate,model_p,edge,gap,secs_left,mode,
        status,fill_price,outcome,pnl,balance_after,venue_order_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time() * 1000), order["ws"], order["slug"], order["side"],
         order["limit"], order["shares"], order["cost"], order["win_rate"],
         order["model_p"], order["edge"], order["gap"], order["secs_left"],
         broker.name, "filled", order["limit"], outcome, pnl,
         broker.balance, order["venue_id"]))
    con.commit()
    print(f"   {outcome}: {'won' if won else 'lost'} ${pnl:+.2f}  "
          f"balance ${broker.balance:.2f}", flush=True)
    print(f"   {risk.summary()}", flush=True)
    if note:
        print(f"   review: {note}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/bot.db")
    ap.add_argument("--balance", type=float,
                    default=float(os.environ.get("START_BALANCE", "100")))
    ap.add_argument("--poll-hz", type=float, default=1.0)
    ap.add_argument("--bankroll", type=float,
                    help="cap what the bot may risk, whatever the account "
                         "holds. With $10 loaded, --bankroll 5 keeps the "
                         "other $5 out of reach.")
    ap.add_argument("--gap-min", type=float)
    ap.add_argument("--gap-max", type=float)
    ap.add_argument("--min-edge", type=float)
    ap.add_argument("--max-fraction", type=float)
    ap.add_argument("--take", action="store_true",
                    help="cross the spread instead of resting a limit order")
    ap.add_argument("--profile", default="turnover", choices=sorted(PROFILES),
                    help="turnover = sell into strength, with a stop "
                         "(default); income = hold to settlement; manual = "
                         "narrower gap band")
    a = ap.parse_args()

    cfg = profile(a.profile)
    for k in ("gap_min", "gap_max", "min_edge", "max_fraction", "bankroll"):
        if getattr(a, k) is not None:
            setattr(cfg, k, getattr(a, k))
    if a.take:
        cfg.maker = False

    live = os.environ.get("LIVE") == "1"
    if live:
        print("LIVE MODE. This will place real orders with real money.")
        print(f"Stake is up to {cfg.max_fraction:.0%} of balance per trade.")
        if input("Type YES to continue: ").strip() != "YES":
            print("aborted")
            sys.exit(1)
    sys.exit(run(cfg, a.db, a.balance, live, a.poll_hz))
