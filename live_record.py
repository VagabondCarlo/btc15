#!/usr/bin/env python3
"""Record the live order book through each 15-minute window. Trades nothing.

The historical study answered what the market charges on average but could
not answer two things, because the venue only publishes four price points
per past window:

  1. The full bid/ask path, so what a taker would really pay at the moment
     a rule triggers, including the spread.
  2. Whether a resting limit order below the market actually gets filled,
     and whether those fills are adversely selected. The historical table
     says making is the only version of this rule with a plausible edge, and
     that claim can only be tested with live book data.

Needs no credentials and risks no money. Leave it running for a week, then
re-run the analysis against what it collected.
"""

from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone

import numpy as np

from btc15.gateway import (Gateway, WINDOW_SECS, event_slug, market_slug,
                           parse_terms, twap_at)
from btc15.twap import blended_sigma, forecast_settlement

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS live_book (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    window_start INTEGER NOT NULL,
    secs_left REAL NOT NULL,
    brti REAL, price_to_beat REAL, gap REAL,
    best_bid REAL, best_ask REAL,
    bid_depth REAL, ask_depth REAL,
    last_px REAL, shares_traded REAL,
    bid5_qty REAL, ask5_qty REAL,
    model_p_up REAL, model_sd REAL, locked_frac REAL,
    state TEXT
);
CREATE INDEX IF NOT EXISTS ix_live ON live_book(window_start, ts_ms);
CREATE TABLE IF NOT EXISTS live_windows (
    window_start INTEGER PRIMARY KEY,
    market_slug TEXT, price_to_beat REAL, settlement REAL,
    outcome TEXT, min_qty REAL, fee_coefficient REAL, closed_at INTEGER
);
"""

STOP = False


def _stop(*_):
    global STOP
    STOP = True
    print("\nstopping after this sample...", flush=True)


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money(d, k):
    return f((d.get(k) or {}).get("value")) if isinstance(d.get(k), dict) else None


def ladder(levels):
    """[(price, qty), ...] from a /book bids or offers array."""
    out = []
    for lv in levels or []:
        p = money(lv, "px")
        q = f(lv.get("qty"))
        if p is not None:
            out.append((p, q or 0.0))
    return out


def prune(con, keep_days: int = 30) -> int:
    """Drop samples older than keep_days.

    At two rows a second this table grows by about 170,000 rows a day. On a
    Pi that is an SD card filling up quietly over a couple of months.
    """
    cut = int((time.time() - keep_days * 86400) * 1000)
    n = con.execute("DELETE FROM live_book WHERE ts_ms < ?", (cut,)).rowcount
    if n:
        con.commit()
        con.execute("VACUUM")
    return n


def run(db_path: str, hz_far: float, hz_near: float, near_secs: int,
        keep_days: int = 30) -> int:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    gw = Gateway()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    cur_ws = None
    terms = None
    last_terms_try = 0.0
    prints: dict[int, float] = {}
    sigma = float("nan")
    n = 0
    print("recording. ctrl-c to stop.", flush=True)

    while not STOP:
        loop = time.time()
        now = int(loop)
        ws = now // WINDOW_SECS * WINDOW_SECS

        if ws != cur_ws:
            d = datetime.fromtimestamp(ws, timezone.utc)
            try:
                ev = gw.event(event_slug(d))
                terms = parse_terms(ev) if ev else None
            except Exception as e:
                print(f"  event fetch failed: {e}", flush=True)
                terms = None
            cur_ws = ws
            if terms:
                con.execute(
                    """INSERT OR REPLACE INTO live_windows
                       (window_start,market_slug,price_to_beat,min_qty,
                        fee_coefficient) VALUES (?,?,?,?,?)""",
                    (ws, terms["market_slug"], terms["price_to_beat"],
                     terms["min_qty"], terms["fee_coefficient"]))
                con.commit()
                print(f"\n[{d:%H:%M}z] {terms['market_slug']}  "
                      f"strike={terms['price_to_beat']}", flush=True)
            # refresh vol history and settle the window that just ended
            try:
                prints = gw.index_range(ws - 3500, now)
                hist = np.array([prints[t] for t in sorted(prints)], float)
                sigma = blended_sigma(hist[-600:], hist)
            except Exception as e:
                print(f"  index refresh failed: {e}", flush=True)
            _close_prev(con, gw, ws)
            if ws % 86400 < WINDOW_SECS:      # once a day
                dropped = prune(con, keep_days)
                if dropped:
                    print(f"  pruned {dropped:,} old samples", flush=True)

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
                        con.execute(
                            """INSERT OR REPLACE INTO live_windows
                               (window_start,market_slug,price_to_beat,min_qty,
                                fee_coefficient) VALUES (?,?,?,?,?)""",
                            (ws, terms["market_slug"], terms["price_to_beat"],
                             terms["min_qty"], terms["fee_coefficient"]))
                        con.commit()
                        print(f"  strike published: {terms['price_to_beat']}",
                              flush=True)
                except Exception:
                    pass
            if not terms or terms.get("price_to_beat") is None:
                time.sleep(1.0)
                continue

        we = ws + WINDOW_SECS
        secs_left = we - loop
        try:
            fresh = gw.index_candles(now - 15, now)
            prints.update(fresh)
            md = gw._get(f"/v1/markets/{terms['market_slug']}/book")
            md = (md or {}).get("marketData") or {}
        except Exception:
            time.sleep(1.0)
            continue

        brti = prints.get(max(prints)) if prints else None
        stats = md.get("stats") or {}
        # /book carries the full ladder; bestBid/bestAsk live on /bbo only.
        bids = ladder(md.get("bids"))
        offers = ladder(md.get("offers"))
        bb = max((p for p, _ in bids), default=None)
        ba = min((p for p, _ in offers), default=None)
        bid_sz = sum(q for p, q in bids if p == bb) if bb is not None else None
        ask_sz = sum(q for p, q in offers if p == ba) if ba is not None else None
        bid5 = sum(q for p, q in sorted(bids, reverse=True)[:5])
        ask5 = sum(q for p, q in sorted(offers)[:5])

        mp = sd = lf = None
        if brti is not None and sigma == sigma and sigma > 0:
            win = [prints[t] for t in range(we - 60, int(loop) + 1)
                   if t in prints]
            fc = forecast_settlement(np.asarray(win), secs_left, brti, sigma)
            mp, sd, lf = fc.prob_up(terms["price_to_beat"]), fc.sd, fc.locked_fraction

        con.execute(
            """INSERT INTO live_book(ts_ms,window_start,secs_left,brti,
                  price_to_beat,gap,best_bid,best_ask,bid_depth,ask_depth,
                  last_px,shares_traded,bid5_qty,ask5_qty,
                  model_p_up,model_sd,locked_frac,state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(loop * 1000), ws, secs_left, brti, terms["price_to_beat"],
             (brti - terms["price_to_beat"]) if brti else None, bb, ba,
             bid_sz, ask_sz,
             money(md, "lastTradePx") or money(stats, "lastTradePx"),
             f(stats.get("sharesTraded")), bid5, ask5,
             mp, sd, lf, md.get("state")))
        n += 1
        if n % 20 == 0:
            con.commit()
            g = (brti - terms["price_to_beat"]) if brti else 0.0
            print(f"  {secs_left:>5.0f}s  gap ${g:>+8.2f}  "
                  f"bid {bb if bb is not None else '-':<6} "
                  f"ask {ba if ba is not None else '-':<6} "
                  f"model {mp:.3f}" if mp else "", flush=True)

        hz = hz_near if secs_left <= near_secs else hz_far
        time.sleep(max(0.05, 1.0 / hz - (time.time() - loop)))

    con.commit()
    print(f"\nstored {n} samples in {db_path}")
    con.close()
    return 0


def _close_prev(con, gw: Gateway, cur_ws: int) -> None:
    """Fill in the outcome of windows we recorded that have since settled."""
    rows = con.execute(
        """SELECT window_start FROM live_windows
           WHERE outcome IS NULL AND window_start < ?""", (cur_ws,)).fetchall()
    for r in rows:
        d = datetime.fromtimestamp(r["window_start"], timezone.utc)
        try:
            ev = gw.event(event_slug(d))
            t = parse_terms(ev) if ev else None
        except Exception:
            continue
        if not t or t["settlement"] is None or t["price_to_beat"] is None:
            continue
        out = "UP" if t["settlement"] >= t["price_to_beat"] else "DOWN"
        con.execute(
            """UPDATE live_windows SET settlement=?,outcome=?,price_to_beat=?,
               closed_at=? WHERE window_start=?""",
            (t["settlement"], out, t["price_to_beat"], int(time.time()),
             r["window_start"]))
        print(f"  settled {d:%H:%M}z -> {out} "
              f"({t['price_to_beat']} -> {t['settlement']})", flush=True)
    con.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/live.db")
    ap.add_argument("--hz-far", type=float, default=0.5)
    ap.add_argument("--hz-near", type=float, default=2.0)
    ap.add_argument("--near-seconds", type=int, default=180)
    ap.add_argument("--keep-days", type=int, default=30)
    a = ap.parse_args()
    sys.exit(run(a.db, a.hz_far, a.hz_near, a.near_seconds, a.keep_days))
