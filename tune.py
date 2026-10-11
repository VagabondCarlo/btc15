#!/usr/bin/env python3
"""Find the settings that produce the best result on the real data.

Sweeps the entry band, the edge required, and the exit floor across 720
harvested windows, scoring each combination on compounded growth rather than
on win rate alone, because a compounding account is hurt by big swings even
when the average is fine.

Reports win rate alongside, since that is what a discretionary trader optimises for,
so the tradeoff between the two is visible rather than argued about.
"""

from __future__ import annotations

import argparse
import itertools
import pickle
from pathlib import Path

import numpy as np

from btc15.fees import THETA_MAKER_REBATE, THETA_TAKER, taker_fee
from btc15.harvest import open_db
from btc15.strategy import WIN_RATE_TABLE
from btc15.twap import forecast_settlement, realized_sigma_per_sec

CACHE = Path("data/tune_paths_full.pkl")
HALF_SPREAD = 0.005
# Observed market price at 2-4 min, by gap: (median, cheapest quartile)
MKT = {(25.0, 50.0): (0.925, 0.780), (50.0, 75.0): (0.990, 0.910),
       (75.0, 100.0): (0.990, 0.990), (100.0, 150.0): (0.990, 0.990)}


def table_wr(gap):
    for lo, hi, wr in WIN_RATE_TABLE:
        if lo <= abs(gap) < hi:
            return (lo, hi), wr
    return None, None


def build_paths(db):
    """Per-second fair value for every window. Cached, it is slow."""
    if CACHE.exists():
        return pickle.loads(CACHE.read_bytes())
    con = open_db(db)
    P = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    W = con.execute("""SELECT * FROM windows WHERE outcome IS NOT NULL
        AND ABS(recon_open-price_to_beat)<0.005
        AND ABS(recon_close-settlement)<0.005
        AND n_open=60 AND n_close=60 ORDER BY window_start""").fetchall()
    out = []
    for i, w in enumerate(W):
        ws, we, ptb = w["window_start"], w["window_end"], w["price_to_beat"]
        hist = np.array([P[t] for t in range(ws - 600, ws) if t in P])
        sig = realized_sigma_per_sec(hist)
        if not (sig == sig) or sig <= 0:
            continue
        sig *= 1.25
        rec = {"outcome": w["outcome"], "we": we, "ptb": ptb, "pts": []}
        # Full window, not just the last five minutes. The earlier cache
        # stopped at 300s, which silently capped every entry-timing sweep:
        # 780s and 420s produced identical results because neither existed.
        for t in range(ws, we + 1):
            px = P.get(t)
            if px is None:
                continue
            win = [P[s] for s in range(we - 60, t + 1) if s in P]
            pu = forecast_settlement(np.asarray(win), we - t, px,
                                     sig).prob_up(ptb)
            rec["pts"].append((we - t, px - ptb, pu))
        out.append(rec)
        if i % 150 == 0:
            print(f"  building paths {i}/{len(W)}", flush=True)
    CACHE.write_bytes(pickle.dumps(out))
    return out


def trial(paths, gap_lo, gap_hi, s_lo, s_hi, min_edge, exit_floor,
          fill_rate, kelly_cap, rng):
    bal, peak = 100.0, 100.0
    wins = losses = exits = 0
    cooldown_until = -1
    for idx, w in enumerate(paths):
        if idx < cooldown_until:
            continue
        pts = {sl: (g, pu) for sl, g, pu in w["pts"]}
        entry = None
        for sl in range(s_hi, s_lo - 1, -1):
            if sl not in pts:
                continue
            g, pu = pts[sl]
            key, wr = table_wr(g)
            if key not in MKT or not (gap_lo <= abs(g) < gap_hi):
                continue
            med, cheap = MKT[key]
            limit = round(wr - min_edge, 2)
            if limit <= 0 or limit > med:
                continue
            entry = (sl, "UP" if g > 0 else "DOWN", wr, limit)
            break
        # One fill decision per window, not one per second. Rolling every
        # second let a 2% fill rate produce a trade in 91% of windows, which
        # made the whole fill-rate sweep meaningless.
        if entry is not None and entry[3] < MKT[table_wr(
                pts[entry[0]][0])[0]][1] and rng.random() > fill_rate:
            entry = None
        if entry is None:
            continue
        sl0, side, wr, limit = entry
        cost = limit - THETA_MAKER_REBATE * limit * (1 - limit)
        stake = min(bal * kelly_cap, bal * 0.5)
        if stake < 0.05:
            continue
        shares = stake / cost

        # hold, with the deep exit
        settled = None
        for sl in range(sl0 - 20, 24, -1):
            if sl not in pts:
                continue
            _, pu = pts[sl]
            fair = pu if side == "UP" else 1.0 - pu
            if fair <= exit_floor:
                px = max(0.01, fair - HALF_SPREAD)
                settled = shares * px - taker_fee(shares, px)
                exits += 1
                break
        if settled is None:
            settled = shares if side == w["outcome"] else 0.0
        pnl = settled - stake
        bal += pnl
        peak = max(peak, bal)
        if pnl > 0:
            wins += 1
            cooldown_until = -1
        else:
            losses += 1
            cooldown_until = idx + 2
        if bal < peak * 0.35 or bal < 1:
            break
    n = wins + losses
    return {"bal": bal, "n": n, "wins": wins, "exits": exits,
            "wr": wins / n if n else 0.0}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/harvest.db")
    ap.add_argument("--fill-rate", type=float, default=0.35)
    a = ap.parse_args()

    print("building per-second fair value paths...", flush=True)
    paths = build_paths(a.db)
    print(f"{len(paths)} windows ready\n")

    grid = list(itertools.product(
        [(25.0, 75.0), (25.0, 50.0), (25.0, 100.0), (50.0, 100.0)],
        [0.02, 0.03, 0.05],
        [0.0, 0.30, 0.40, 0.50],
        [0.03, 0.05, 0.08],
    ))
    rows = []
    for (glo, ghi), edge, xfloor, cap in grid:
        bals, wrs, ns, exs = [], [], [], []
        for seed in range(10):
            rng = np.random.default_rng(seed)
            r = trial(paths, glo, ghi, 120, 240, edge, xfloor,
                      a.fill_rate, cap, rng)
            bals.append(r["bal"]); wrs.append(r["wr"])
            ns.append(r["n"]); exs.append(r["exits"])
        rows.append(((glo, ghi), edge, xfloor, cap,
                     float(np.median(bals)), float(np.percentile(bals, 10)),
                     float(np.mean(wrs)), float(np.mean(ns))))

    rows.sort(key=lambda r: -r[4])
    print("TOP SETTINGS, scored on compounded balance from $100 over 8.6 days\n")
    print(f"{'gap band':>14}{'edge':>7}{'exit':>7}{'stake':>7}"
          f"{'median':>10}{'worst 10%':>11}{'win rate':>10}{'trades':>8}")
    print("-" * 76)
    for g, e, x, c, med, p10, wr, n in rows[:12]:
        print(f"{f'${g[0]:.0f}-{g[1]:.0f}':>14}{e * 100:>6.0f}c"
              f"{(f'{x:.2f}' if x else 'none'):>7}{c:>6.0%}"
              f"{med:>10.2f}{p10:>11.2f}{wr:>9.0%}{n:>8.0f}")
    print("\nWORST SETTINGS, for contrast\n")
    for g, e, x, c, med, p10, wr, n in rows[-4:]:
        print(f"{f'${g[0]:.0f}-{g[1]:.0f}':>14}{e * 100:>6.0f}c"
              f"{(f'{x:.2f}' if x else 'none'):>7}{c:>6.0%}"
              f"{med:>10.2f}{p10:>11.2f}{wr:>9.0%}{n:>8.0f}")
