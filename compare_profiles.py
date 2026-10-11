#!/usr/bin/env python3
"""Race the discretionary method against the backtested one on real windows.

The discretionary method takes the quoted price. The patient one rests below the
market and often never gets filled. Those are different bets and the only
fair comparison is on the same windows with the same money and the same
safeties, which is what this does.

Market prices come from the venue's own published high and low per window,
paired with the index gap at that exact second. They are extremes rather
than a continuous book, so the median of them is a fair read of the price
level and the cheap quartile is optimistic. The patient profile is given the
cheap quartile and a fill probability; the manual profiles pay the median,
which is what taking the offer costs.
"""

from __future__ import annotations

import argparse

import numpy as np

from btc15.fees import THETA_MAKER_REBATE, THETA_TAKER
from btc15.strategy import PROFILES, Risk, WIN_RATE_TABLE, profile, stake_for
from tune import build_paths

# Measured from the recorded order book, not inferred from window extremes.
# The inferred version was 10-20 cents too pessimistic and wrongly concluded
# that taking the offer could never work.
from btc15.prices import load as _load_prices

_ASK = _load_prices()
# (price a taker pays, price a resting order might get)
MKT = {k: (v, round(v - 0.06, 3)) for k, v in _ASK.items()}


def bucket(gap):
    for lo, hi, wr in WIN_RATE_TABLE:
        if lo <= abs(gap) < hi:
            return (lo, hi), wr
    return None, None


def run(paths, cfg, fill_rate, seed, start=5.0):
    rng = np.random.default_rng(seed)
    risk = Risk(cfg)
    bal = start
    risk.s.equity = start
    risk.note_balance(bal)
    wins = losses = skipped = 0
    peak = start

    for idx, w in enumerate(paths):
        if risk.blocked(bal, idx * 900):
            skipped += 1
            continue
        pts = {sl: (g, pu) for sl, g, pu in w["pts"]}
        entry = None
        for sl in range(240, 119, -1):
            if sl not in pts:
                continue
            g, pu = pts[sl]
            key, wr = bucket(g)
            if key not in MKT or not (cfg.gap_min <= abs(g) < cfg.gap_max):
                continue
            med, cheap = MKT[key]
            if cfg.maker:
                limit = round(wr - cfg.min_edge, 2)
                if limit <= 0 or limit > med:
                    continue
                cost = limit - THETA_MAKER_REBATE * limit * (1 - limit)
            else:
                limit = med                      # taking the offer
                if limit > cfg.max_price:
                    continue
                cost = limit + THETA_TAKER * limit * (1 - limit)
            if wr - cost < cfg.min_edge:
                continue
            entry = (sl, "UP" if g > 0 else "DOWN", wr, limit, cost)
            break
        if entry is None:
            continue
        sl0, side, wr, limit, cost = entry
        if cfg.maker and limit < MKT[bucket(pts[sl0][0])[0]][1] \
                and rng.random() > fill_rate:
            continue

        stake = stake_for(bal, wr, limit, cfg)
        if stake < cfg.min_stake:
            continue
        shares = stake / cost

        settled = None
        for sl in range(sl0 - 20, 24, -1):
            if sl not in pts:
                continue
            fair = pts[sl][1] if side == "UP" else 1 - pts[sl][1]
            if cfg.exit_enabled and fair <= cfg.exit_fair_floor:
                px = max(0.01, fair - 0.005)
                settled = shares * px - THETA_TAKER * shares * px * (1 - px)
                break
        if settled is None:
            settled = shares if side == w["outcome"] else 0.0
        pnl = settled - stake
        bal += pnl
        risk.s.equity = bal
        peak = max(peak, bal)
        risk.note_balance(bal)
        risk.record(wr, pnl > 0, idx * 900)
        (wins := wins + 1) if pnl > 0 else (losses := losses + 1)
        if bal < 0.05:
            break
    n = wins + losses
    return {"bal": bal, "n": n, "wr": wins / n if n else 0.0,
            "peak": peak, "halted": bool(risk.s.halted)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fill-rate", type=float, default=0.10)
    ap.add_argument("--start", type=float, default=5.0)
    a = ap.parse_args()

    paths = build_paths("data/harvest.db")
    days = len(paths) * 15 / 60 / 24
    print(f"{len(paths)} real windows, {days:.1f} days, "
          f"starting at ${a.start:.2f} each\n")
    print(f"{'profile':>16}{'trades':>8}{'win rate':>10}{'median end':>12}"
          f"{'worst':>9}{'best':>9}{'stopped out':>13}")
    print("-" * 77)
    for name in ("manual", "manual_capped", "patient"):
        cfg = profile(name, bankroll=a.start)
        rs = [run(paths, cfg, a.fill_rate, s, a.start) for s in range(14)]
        bals = sorted(r["bal"] for r in rs)
        print(f"{name:>16}{np.mean([r['n'] for r in rs]):>8.0f}"
              f"{np.mean([r['wr'] for r in rs]):>9.0%}"
              f"{bals[len(bals) // 2]:>12.2f}{bals[0]:>9.2f}{bals[-1]:>9.2f}"
              f"{np.mean([r['halted'] for r in rs]):>12.0%}")
