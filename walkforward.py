#!/usr/bin/env python3
"""Does the tuning survive on data it was not fitted to?

tune.py picked the best of 144 parameter combinations on 8.6 days. Picking a
winner out of 144 tries on one stretch of history will always look good,
whether or not the edge is real. This splits the history in two, chooses
settings using only the first half, and then runs those settings on the
second half, which the chooser never saw.

The gap between the two numbers is how much of the result was fitting.
"""

from __future__ import annotations

import argparse
import itertools

import numpy as np

from tune import build_paths, trial


def best_on(paths, grid, fill_rate, seeds=6):
    scored = []
    for (glo, ghi), edge, xfloor, cap in grid:
        b = [trial(paths, glo, ghi, 120, 240, edge, xfloor, fill_rate, cap,
                   np.random.default_rng(s))["bal"] for s in range(seeds)]
        scored.append((float(np.median(b)), (glo, ghi), edge, xfloor, cap))
    scored.sort(reverse=True)
    return scored


def report(paths, params, fill_rate, label, seeds=12):
    (glo, ghi), edge, xfloor, cap = params
    rs = [trial(paths, glo, ghi, 120, 240, edge, xfloor, fill_rate, cap,
                np.random.default_rng(100 + s)) for s in range(seeds)]
    bals = sorted(r["bal"] for r in rs)
    wr = float(np.mean([r["wr"] for r in rs]))
    n = float(np.mean([r["n"] for r in rs]))
    days = len(paths) * 15 / 60 / 24
    med = bals[len(bals) // 2]
    daily = (med / 100.0) ** (1 / days) - 1 if med > 0 and days else 0.0
    print(f"  {label:<26} ${med:>8.2f}  worst ${bals[0]:>7.2f}  "
          f"win {wr:>4.0%}  {n:>5.0f} trades  {daily:>+7.1%}/day")
    return med, daily


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fill-rate", type=float, default=0.35)
    a = ap.parse_args()

    paths = build_paths("data/harvest.db")
    half = len(paths) // 2
    first, second = paths[:half], paths[half:]
    print(f"{len(paths)} windows: first half {len(first)}, "
          f"second half {len(second)}\n")

    grid = list(itertools.product(
        [(25.0, 75.0), (25.0, 50.0), (25.0, 100.0), (50.0, 100.0)],
        [0.02, 0.03, 0.05], [0.0, 0.30, 0.40, 0.50], [0.03, 0.05, 0.08]))

    scored = best_on(first, grid, a.fill_rate)
    top = scored[0]
    params = (top[1], top[2], top[3], top[4])
    print(f"Chosen on the FIRST half only: gap ${params[0][0]:.0f}-"
          f"{params[0][1]:.0f}, edge {params[1] * 100:.0f}c, "
          f"exit {params[2] or 'none'}, stake {params[3]:.0%}\n")

    print("Results:")
    report(first, params, a.fill_rate, "first half (fitted)")
    m2, d2 = report(second, params, a.fill_rate, "second half (unseen)")

    print("\nFor contrast, the same settings chosen by looking at everything:")
    allbest = best_on(paths, grid, a.fill_rate)[0]
    report(paths, (allbest[1], allbest[2], allbest[3], allbest[4]),
           a.fill_rate, "whole period (fitted)")

    print("\nSpread of the 144 combinations on the unseen half:")
    outs = []
    for sc in scored[:40]:
        p = (sc[1], sc[2], sc[3], sc[4])
        b = [trial(second, p[0][0], p[0][1], 120, 240, p[1], p[2],
                   a.fill_rate, p[3], np.random.default_rng(s))["bal"]
             for s in range(4)]
        outs.append(float(np.median(b)))
    outs = np.array(outs)
    print(f"  of the top 40 settings from the first half, "
          f"{np.mean(outs > 100):.0%} were still profitable on the second")
    print(f"  median ${np.median(outs):.2f}, worst ${outs.min():.2f}, "
          f"best ${outs.max():.2f}")
