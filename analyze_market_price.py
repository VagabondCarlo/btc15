#!/usr/bin/env python3
"""Did the market charge more than the gap rule is worth?

The backtest says the rule wins about 86% of the time. That only makes money
if you can buy the side for less than the break-even price. This pairs every
real contract trade price the venue reports with the BRTI gap at that exact
second, keeping only the ones inside the rule's entry window.

Each window reports four timestamped prices: open, high, low and last. High
and low are selected extremes, so they bracket the answer rather than give
it. If even the low is unprofitable the rule is dead; if the high is
profitable the rule is strong. Last trade is the fairest single estimate.
"""

from __future__ import annotations

import argparse
import math

from btc15.fees import THETA_TAKER, taker_fee
from btc15.harvest import open_db


def summarise(label, rows, theta):
    if len(rows) < 25:
        print(f"  {label:<12} {len(rows):>5} samples  (too few)")
        return
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    wr = wins / n
    avg_px = sum(r["px"] for r in rows) / n
    # EV per contract: win pays 1, cost is price plus taker fee
    ev = sum(r["won"] - r["px"] - taker_fee(1, r["px"], theta) for r in rows) / n
    risked = sum(r["px"] + taker_fee(1, r["px"], theta) for r in rows) / n
    se = math.sqrt(wr * (1 - wr) / n)
    roi = ev / risked if risked else 0.0
    print(f"  {label:<12} {n:>5}  win {wr:>5.1%}+/-{1.96*se:<5.1%} "
          f"avg price {avg_px:>5.3f}  EV/contract {ev:>+6.3f}  ROI {roi:>+6.2%}")


def run(db_path, min_gap, entry_secs, min_secs, theta):
    con = open_db(db_path)
    prints = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}

    wins = con.execute("""
        SELECT w.*, b.open_px,b.open_ts,b.high_px,b.high_ts,b.low_px,b.low_ts,
               b.last_px,b.last_ts,b.notional_traded
        FROM windows w JOIN book_stats b USING(window_start)
        WHERE w.outcome IS NOT NULL AND w.price_to_beat IS NOT NULL
          AND ABS(w.recon_open - w.price_to_beat) < 0.005
          AND ABS(w.recon_close - w.settlement) < 0.005
          AND w.n_open = 60 AND w.n_close = 60""").fetchall()
    if not wins:
        print("No windows with both price stats and clean reconstruction.")
        return 1

    notional = [w["notional_traded"] for w in wins if w["notional_traded"]]
    notional.sort()
    print(f"{len(wins)} windows with contract price data")
    if notional:
        print(f"median traded per window: ${notional[len(notional)//2]:,.0f}"
              f"   (your size will not move this)\n")

    buckets: dict[str, list] = {k: [] for k in ("open", "high", "low", "last")}
    for w in wins:
        ptb, we, outcome = w["price_to_beat"], w["window_end"], w["outcome"]
        for kind in buckets:
            px_up, ts = w[f"{kind}_px"], w[f"{kind}_ts"]
            if px_up is None or ts is None:
                continue
            secs_left = we - ts
            if not (min_secs <= secs_left <= entry_secs):
                continue
            brti = prints.get(ts)
            if brti is None:
                continue
            gap = brti - ptb
            if abs(gap) < min_gap:
                continue
            side = "UP" if gap > 0 else "DOWN"
            # The reported price is the UP (Yes) side. Down costs 1 - up.
            px = px_up if side == "UP" else 1.0 - px_up
            if not (0.01 <= px <= 0.99):
                continue
            buckets[kind].append({
                "px": px, "won": int(side == outcome),
                "gap": abs(gap), "secs": secs_left})

    print(f"Trades priced inside the rule's window "
          f"(gap>=${min_gap:.0f}, <={entry_secs}s left), fee theta={theta}:\n")
    for kind, label in (("low", "best entry"), ("last", "last trade"),
                        ("open", "window open"), ("high", "worst entry")):
        summarise(label, buckets[kind], theta)

    ref = buckets["last"]
    if len(ref) >= 60:
        print("\nLast-trade entries split by price paid:")
        print(f"  {'price band':<14}{'n':>6}{'win rate':>11}{'EV/contract':>14}"
              f"{'ROI':>9}")
        for lo, hi in ((0.0, 0.70), (0.70, 0.80), (0.80, 0.85),
                       (0.85, 0.90), (0.90, 0.95), (0.95, 1.0)):
            sel = [r for r in ref if lo <= r["px"] < hi]
            if len(sel) < 15:
                continue
            n = len(sel)
            wr = sum(r["won"] for r in sel) / n
            ev = sum(r["won"] - r["px"] - taker_fee(1, r["px"], theta)
                     for r in sel) / n
            risk = sum(r["px"] + taker_fee(1, r["px"], theta) for r in sel) / n
            print(f"  {lo:.2f}-{hi:.2f}    {n:>6}{wr:>11.1%}{ev:>+14.3f}"
                  f"{ev/risk:>+9.2%}")
    con.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/harvest.db")
    ap.add_argument("--min-gap", type=float, default=40.0)
    ap.add_argument("--entry-seconds", type=int, default=420)
    ap.add_argument("--min-seconds", type=int, default=5)
    ap.add_argument("--theta", type=float, default=THETA_TAKER)
    a = ap.parse_args()
    raise SystemExit(run(a.db, a.min_gap, a.entry_seconds, a.min_seconds,
                         a.theta))
