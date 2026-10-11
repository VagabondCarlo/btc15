#!/usr/bin/env python3
"""Backtest a discretionary gap rule on real completed windows.

The rule: when BTC is MIN_GAP dollars or more away from the price to beat,
with ENTRY_SECONDS or fewer left, bet the side the price is currently on.

Two things get measured.

1. Win rate by gap size and time remaining, converted to the highest price
   you could pay and still break even after the 0.0695 taker fee. That is
   the number that sets MAX_PRICE. A rule that wins 70% of the time is still
   a losing rule if the market charges 75 cents to take that side.

2. Whether strategy B's probability model is calibrated. For every sample it
   compares the model's predicted win chance against what actually happened.
   A model that says 90% and delivers 70% will size you into ruin, so this
   check matters more than the headline win rate.

Uses only public data. No credentials, no orders, no money.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys

import numpy as np

from btc15.fees import THETA_TAKER
from btc15.harvest import open_db
from btc15.twap import forecast_settlement, realized_sigma_per_sec

VOL_LOOKBACK = 600          # handoff: std dev of 1s changes over last 10 min
VOL_SAFETY = 1.25           # handoff: inflate vol 25% to be conservative


def breakeven_price(win_rate: float, theta: float = THETA_TAKER) -> float:
    """Highest price payable at this win rate for EV >= 0.

    Solves p + theta*p*(1-p) = win_rate for p.
    """
    if win_rate <= 0:
        return 0.0
    if win_rate >= 1:
        return 1.0
    a, b, c = theta, -(1.0 + theta), win_rate
    disc = b * b - 4 * a * c
    if disc < 0:
        return 1.0
    return (-b - math.sqrt(disc)) / (2 * a)


def load_windows(con: sqlite3.Connection, require_full: bool = True):
    q = """SELECT * FROM windows
           WHERE outcome IS NOT NULL AND price_to_beat IS NOT NULL"""
    if require_full:
        # Only windows whose reconstruction reproduces the venue's own
        # published values. Index history before 2026-09-25 is restated by
        # about a cent and would quietly corrupt the strike.
        q += (" AND n_prints >= 890 AND n_open = 60 AND n_close = 60"
              " AND ABS(recon_open - price_to_beat) < 0.005"
              " AND ABS(recon_close - settlement) < 0.005")
    return con.execute(q + " ORDER BY window_start").fetchall()


def print_series(con: sqlite3.Connection, a: int, b: int) -> dict[int, float]:
    return {r[0]: r[1] for r in con.execute(
        "SELECT ts,value FROM prints WHERE ts BETWEEN ? AND ?", (a, b))}


def run(db_path: str, min_gap: float, entry_secs: int, min_secs: int,
        vol_safety: float, sample_every: int) -> int:
    con = open_db(db_path)
    wins = load_windows(con)
    if not wins:
        print("No complete windows harvested yet. Run: "
              "python -m btc15.harvest 1400")
        return 1

    lo = wins[0]["window_start"] - VOL_LOOKBACK - 70
    hi = wins[-1]["window_end"] + 5
    prints = print_series(con, lo, hi)

    days = (wins[-1]["window_end"] - wins[0]["window_start"]) / 86400
    print(f"{len(wins)} complete windows over {days:.1f} days")
    up = sum(1 for w in wins if w["outcome"] == "UP")
    print(f"base rate: UP {up / len(wins):.1%}  DOWN {1 - up / len(wins):.1%}"
          f"   (ties resolve UP)\n")

    rows = []          # every qualifying sample
    first_only = []    # strategy A: one trade per window, first trigger

    for w in wins:
        ws, we = w["window_start"], w["window_end"]
        ptb, outcome = w["price_to_beat"], w["outcome"]

        hist = np.array([prints[t] for t in range(ws - VOL_LOOKBACK, ws)
                         if t in prints], dtype=float)
        sig = realized_sigma_per_sec(hist)
        if not (sig == sig) or sig <= 0:
            continue
        sig *= vol_safety

        taken = False
        for t in range(ws, we + 1, sample_every):
            secs_left = we - t
            if not (min_secs <= secs_left <= entry_secs):
                continue
            px = prints.get(t)
            if px is None:
                continue
            gap = px - ptb
            if abs(gap) < min_gap:
                continue

            side = "UP" if gap > 0 else "DOWN"
            won = int(side == outcome)

            window_prints = [prints[s] for s in range(we - 60, t + 1)
                             if s in prints and s >= we - 60]
            f = forecast_settlement(np.asarray(window_prints), secs_left,
                                    px, sig)
            p_up = f.prob_up(ptb)
            model_p = p_up if side == "UP" else 1.0 - p_up
            model_p = min(max(model_p, 0.02), 0.98)

            rec = (abs(gap), secs_left, won, model_p, side)
            rows.append(rec)
            if not taken:
                first_only.append(rec)
                taken = True

    if not rows:
        print(f"No sample met gap>={min_gap} with <={entry_secs}s left.")
        return 1

    arr_gap = np.array([r[0] for r in rows])
    arr_sec = np.array([r[1] for r in rows])
    arr_won = np.array([r[2] for r in rows])
    arr_mp = np.array([r[3] for r in rows])

    # ---- 1. the headline rule --------------------------------------
    fo_won = np.array([r[2] for r in first_only])
    n, wr = len(fo_won), fo_won.mean()
    se = math.sqrt(wr * (1 - wr) / n)
    print(f"STRATEGY A  (gap>=${min_gap:.0f}, <={entry_secs}s left, "
          f"first trigger per window)")
    print(f"  trades {n}   win rate {wr:.1%}  +/- {1.96 * se:.1%} (95%)")
    print(f"  break-even price {breakeven_price(wr):.3f}  "
          f"-> pay less than {breakeven_price(wr) * 100:.1f} cents or lose money")
    print(f"  coverage: triggered in {n}/{len(wins)} windows "
          f"({n / len(wins):.0%})\n")

    # ---- 2. the grid that sets the parameters -----------------------
    gap_edges = [min_gap, 40, 60, 80, 120, 200, 1e9]
    gap_edges = sorted({g for g in gap_edges if g >= min_gap})
    sec_edges = [(0, 30), (30, 60), (60, 120), (120, 240), (240, 420), (420, 900)]

    print("WIN RATE  /  BREAK-EVEN PRICE   by gap and time left")
    print("(cells with fewer than 40 samples are omitted)\n")
    hdr = f"{'gap':>12} | " + " | ".join(
        f"{f'{a}-{b}s':>13}" for a, b in sec_edges if b <= entry_secs or a < entry_secs)
    print(hdr)
    print("-" * len(hdr))
    for i in range(len(gap_edges) - 1):
        g0, g1 = gap_edges[i], gap_edges[i + 1]
        label = f"${g0:.0f}+" if g1 > 1e8 else f"${g0:.0f}-{g1:.0f}"
        cells = []
        for a, b in sec_edges:
            if not (b <= entry_secs or a < entry_secs):
                continue
            m = (arr_gap >= g0) & (arr_gap < g1) & (arr_sec >= a) & (arr_sec < b)
            k = int(m.sum())
            if k < 40:
                cells.append(f"{'-':>13}")
            else:
                w_ = arr_won[m].mean()
                cells.append(f"{w_:>6.1%}/{breakeven_price(w_):>6.3f}")
        print(f"{label:>12} | " + " | ".join(cells))

    # ---- 3. is the model honest? ------------------------------------
    print("\nCALIBRATION of strategy B's probability model")
    print("If the model is trustworthy, predicted and actual should match.\n")
    print(f"{'predicted':>12} {'actual':>9} {'n':>8} {'error':>9}")
    bins = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90),
            (0.90, 0.95), (0.95, 0.99), (0.99, 1.01)]
    worst = 0.0
    for a, b in bins:
        m = (arr_mp >= a) & (arr_mp < b)
        k = int(m.sum())
        if k < 40:
            continue
        pred, act = arr_mp[m].mean(), arr_won[m].mean()
        worst = max(worst, abs(pred - act))
        print(f"{a:.2f}-{b:.2f}  {pred:>9.1%} {act:>9.1%} {k:>8,} "
              f"{act - pred:>+9.1%}")
    print(f"\n  largest miss: {worst:.1%}")
    brier = float(np.mean((arr_mp - arr_won) ** 2))
    base = arr_won.mean()
    print(f"  Brier score {brier:.4f} vs {np.mean((base - arr_won) ** 2):.4f} "
          f"for always guessing the base rate")

    con.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/harvest.db")
    ap.add_argument("--min-gap", type=float, default=40.0)
    ap.add_argument("--entry-seconds", type=int, default=420)
    ap.add_argument("--min-seconds", type=int, default=5)
    ap.add_argument("--vol-safety", type=float, default=VOL_SAFETY)
    ap.add_argument("--sample-every", type=int, default=5,
                    help="seconds between samples; 1 is exhaustive")
    a = ap.parse_args()
    sys.exit(run(a.db, a.min_gap, a.entry_seconds, a.min_seconds,
                 a.vol_safety, a.sample_every))
