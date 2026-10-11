#!/usr/bin/env python3
"""Test scalping: buy, take a few cents, get out, repeat.

This is a different bet from holding to settlement. Holding asks "will this
side win?". Scalping asks "will the price move my way before it moves
against me?". The second question can be answered from the BRTI path alone,
because the contract price tracks the probability, and the probability is a
function of the gap and the time left. So no order book is needed.

The shape of the trade matters. A scalp caps the win at the target but the
loss runs to wherever you cut it, and you pay the taker fee twice. That means
the break-even hit rate is well above half even when the target and the stop
are the same size.
"""

from __future__ import annotations

import argparse

import numpy as np

from btc15.fees import THETA_TAKER
from btc15.harvest import open_db
from btc15.twap import forecast_settlement, realized_sigma_per_sec

VOL_LOOKBACK = 600


def fair_path(prints, ws, we, ptb, sigma):
    """Fair price of the UP side at each second of the window."""
    out = {}
    for t in range(ws, we + 1):
        px = prints.get(t)
        if px is None:
            continue
        win = [prints[s] for s in range(we - 60, t + 1) if s in prints]
        out[t] = forecast_settlement(np.asarray(win), we - t, px,
                                     sigma).prob_up(ptb)
    return out


def run(db, target, stop, entry_lo, entry_hi, max_hold, theta):
    con = open_db(db)
    P = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    W = con.execute("""SELECT * FROM windows WHERE outcome IS NOT NULL
        AND ABS(recon_open-price_to_beat)<0.005
        AND ABS(recon_close-settlement)<0.005
        AND n_open=60 AND n_close=60 ORDER BY window_start""").fetchall()

    trades = []
    for w in W:
        ws, we, ptb = w["window_start"], w["window_end"], w["price_to_beat"]
        hist = np.array([P[t] for t in range(ws - VOL_LOOKBACK, ws) if t in P])
        sigma = realized_sigma_per_sec(hist)
        if not (sigma == sigma) or sigma <= 0:
            continue
        path = fair_path(P, ws, we, ptb, sigma * 1.25)
        if not path:
            continue

        t = ws
        while t < we - 30:
            p = path.get(t)
            if p is None:
                t += 1
                continue
            # Trade whichever side the move favours, priced as that side.
            side_p = p if p >= 0.5 else 1.0 - p
            if not (entry_lo <= side_p <= entry_hi):
                t += 1
                continue

            up = p >= 0.5
            entry = side_p
            tgt, stp = entry + target, entry - stop
            exit_p, held, how = None, 0, "timeout"
            for u in range(t + 1, min(t + max_hold, we) + 1):
                q = path.get(u)
                if q is None:
                    continue
                cur = q if up else 1.0 - q
                held = u - t
                if cur >= tgt:
                    exit_p, how = tgt, "target"
                    break
                if cur <= stp:
                    exit_p, how = stp, "stop"
                    break
            if exit_p is None:
                last = max((u for u in range(t + 1, min(t + max_hold, we) + 1)
                            if u in path), default=None)
                if last is None:
                    t += 1
                    continue
                exit_p = path[last] if up else 1.0 - path[last]
                held = last - t

            fee = (THETA_TAKER * entry * (1 - entry)
                   + THETA_TAKER * exit_p * (1 - exit_p))
            trades.append((entry, exit_p, exit_p - entry - fee, how, held))
            t += max_hold          # one scalp at a time
        # end window
    return trades


def report(trades, target, stop, theta):
    if not trades:
        print("no trades")
        return
    a = np.array([t[2] for t in trades])
    hits = sum(1 for t in trades if t[3] == "target")
    stops = sum(1 for t in trades if t[3] == "stop")
    outs = len(trades) - hits - stops
    entry = np.mean([t[0] for t in trades])
    fee = 2 * theta * entry * (1 - entry)
    be = (stop + fee) / (target + stop)

    print(f"  trades {len(trades):>6}   avg entry {entry:.3f}   "
          f"avg hold {np.mean([t[4] for t in trades]):.0f}s")
    print(f"  target hit {hits / len(trades):>6.1%}   "
          f"stopped {stops / len(trades):.1%}   timed out {outs / len(trades):.1%}")
    print(f"  round-trip fee {fee * 100:.2f}c   "
          f"break-even hit rate {be:.1%}")
    print(f"  EV per contract {a.mean() * 100:>+6.2f}c   "
          f"total {a.sum() * 100:+.0f}c over {len(trades)} trades")
    print(f"  verdict: {'PROFITABLE' if a.mean() > 0 else 'LOSES MONEY'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/harvest.db")
    ap.add_argument("--theta", type=float, default=THETA_TAKER)
    a = ap.parse_args()

    print("SCALPING: buy, take a few cents, get out.\n")
    print("Entry band 0.55-0.80 on the favoured side, one position at a time.\n")
    for target, stop, hold in ((0.05, 0.05, 120), (0.10, 0.10, 180),
                               (0.05, 0.10, 180), (0.10, 0.05, 180),
                               (0.15, 0.15, 300)):
        print(f"take +{target * 100:.0f}c / cut -{stop * 100:.0f}c, "
              f"max hold {hold}s")
        report(run(a.db, target, stop, 0.55, 0.80, hold, a.theta),
               target, stop, a.theta)
        print()
