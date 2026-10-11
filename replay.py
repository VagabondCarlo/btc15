#!/usr/bin/env python3
"""Replay the bot's strategy and risk rules over the harvested windows.

The one thing this cannot read from history is the price a resting limit
order would have been filled at, because the venue only publishes four price
points per past window. So it runs two scenarios and labels them honestly:

  cheap  - filled at the cheapest quarter of observed prices, which is where
           a patient limit order would sit if it gets filled at all
  median - filled at the typical traded price, which is what happens if the
           order only fills when the market is coming to meet it

The gap between those two outcomes is the whole question. Everything else,
the win rates and the fees, is measured.
"""

from __future__ import annotations

import argparse

from btc15.fees import THETA_MAKER_REBATE
from btc15.harvest import open_db
from btc15.strategy import Config, Risk, WIN_RATE_TABLE, stake_for

# Observed market prices at 2-4 minutes left: (median, cheapest quartile)
PRICES = {(25.0, 50.0): (0.925, 0.780), (50.0, 75.0): (0.990, 0.910)}
FILL_RATE_CHEAP = 0.35   # a resting order will not always be reached


def bucket(gap: float):
    for lo, hi, wr in WIN_RATE_TABLE:
        if lo <= abs(gap) < hi:
            return (lo, hi), wr
    return None, None


def run(db, scenario, start_balance, fill_rate, seed):
    import numpy as np
    rng = np.random.default_rng(seed)
    cfg = Config()
    con = open_db(db)
    P = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    W = con.execute("""SELECT * FROM windows WHERE outcome IS NOT NULL
        AND ABS(recon_open-price_to_beat)<0.005
        AND ABS(recon_close-settlement)<0.005
        AND n_open=60 AND n_close=60 ORDER BY window_start""").fetchall()

    risk = Risk(cfg)
    bal = start_balance
    risk.note_balance(bal)
    curve, skipped, unfilled = [bal], 0, 0

    for w in W:
        ws, we, ptb = w["window_start"], w["window_end"], w["price_to_beat"]
        blk = risk.blocked(bal, ws)
        if blk:
            skipped += 1
            continue

        entry = None
        for t in range(we - cfg.entry_secs_max, we - cfg.entry_secs_min):
            px = P.get(t)
            if px is None:
                continue
            gap = px - ptb
            key, wr = bucket(gap)
            if key not in PRICES:
                continue
            entry = (gap, wr, key, "UP" if gap > 0 else "DOWN")
            break
        if entry is None:
            continue

        gap, wr, key, side = entry
        med, cheap = PRICES[key]
        limit = cheap if scenario == "cheap" else med
        if scenario == "cheap" and rng.random() > fill_rate:
            unfilled += 1
            continue

        cost = limit - THETA_MAKER_REBATE * limit * (1 - limit)
        edge = wr - cost
        if edge < risk.min_edge():
            skipped += 1
            continue

        stake = stake_for(bal, wr, limit, cfg)
        if stake < cfg.min_stake:
            continue
        shares = stake / cost
        won = side == w["outcome"]
        bal += (shares - stake) if won else -stake
        risk.note_balance(bal)
        risk.record(wr, won, ws)
        curve.append(bal)

    return bal, curve, risk, skipped, unfilled, len(W)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/harvest.db")
    ap.add_argument("--balance", type=float, default=100.0)
    ap.add_argument("--fill-rate", type=float, default=FILL_RATE_CHEAP)
    a = ap.parse_args()

    print(f"Replaying 8.6 days of real windows, starting at "
          f"${a.balance:.2f}\n")
    for scen in ("cheap", "median"):
        bals = []
        for seed in range(12):
            bal, curve, risk, skipped, unfilled, n = run(
                a.db, scen, a.balance, a.fill_rate, seed)
            bals.append(bal)
        bals.sort()
        label = ("filled cheap (limit order reached)" if scen == "cheap"
                 else "filled at the typical price")
        print(f"{label}")
        print(f"  trades {risk.s.trades}   win rate "
              f"{risk.s.wins / max(risk.s.trades, 1):.0%}   "
              f"windows sat out {skipped}"
              + (f", orders never filled {unfilled}" if scen == "cheap" else ""))
        print(f"  final balance: median ${bals[len(bals) // 2]:,.2f}   "
              f"worst ${bals[0]:,.2f}   best ${bals[-1]:,.2f}")
        print(f"  that is {bals[len(bals) // 2] / a.balance:.2f}x over "
              f"8.6 days\n")
    print("The difference between those two lines is entirely whether a")
    print("resting order gets filled at a good price. That is what the live")
    print("recorder is measuring and it is the only thing still unknown.")
