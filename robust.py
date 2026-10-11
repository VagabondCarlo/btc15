#!/usr/bin/env python3
"""Find settings that work consistently, not settings that work best once.

The walk-forward test showed the problem. Choosing the single best of 144
combinations on half the history gave +50% a day on that half and +13% on
the next, and most of the other good-looking combinations lost money out of
sample. That is the failure mode where a backtest looks wonderful and the
live account does not.

So this does not look for a peak. It splits the history into four blocks and
keeps only the settings that make money in every one, then ranks what is
left by its *worst* block rather than its average. A setting that is mildly
positive in all four is worth more than one that is spectacular in two and
awful in two, because only the first kind is likely to still be there next
week.
"""

from __future__ import annotations

import argparse
import itertools

import numpy as np

from tune import build_paths, trial

GRID = list(itertools.product(
    [(25.0, 75.0), (25.0, 50.0), (25.0, 100.0), (50.0, 100.0), (20.0, 60.0)],
    [0.02, 0.03, 0.05, 0.07],
    [0.0, 0.30, 0.40, 0.50],
    [0.03, 0.05, 0.08],
))


def blocks(paths, n=4):
    k = len(paths) // n
    return [paths[i * k:(i + 1) * k] for i in range(n)]


def score(block, params, fill_rate, seeds=5):
    (glo, ghi), edge, xfloor, cap = params
    rs = [trial(block, glo, ghi, 120, 240, edge, xfloor, fill_rate, cap,
                np.random.default_rng(s)) for s in range(seeds)]
    bals = [r["bal"] for r in rs]
    days = len(block) * 15 / 60 / 24
    med = float(np.median(bals))
    return {
        "bal": med,
        "daily": (med / 100.0) ** (1 / days) - 1 if med > 0 else -1.0,
        "wr": float(np.mean([r["wr"] for r in rs])),
        "n": float(np.mean([r["n"] for r in rs])),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fill-rate", type=float, default=0.35)
    ap.add_argument("--blocks", type=int, default=4)
    a = ap.parse_args()

    paths = build_paths("data/harvest.db")
    bl = blocks(paths, a.blocks)
    print(f"{len(paths)} windows split into {a.blocks} blocks of "
          f"{len(bl[0])} ({len(bl[0]) * 15 / 60 / 24:.1f} days each)\n")
    print(f"testing {len(GRID)} settings on every block...\n")

    results = []
    for params in GRID:
        ss = [score(b, params, a.fill_rate) for b in bl]
        dailies = [s["daily"] for s in ss]
        results.append({
            "params": params,
            "worst": min(dailies),
            "median": float(np.median(dailies)),
            "all_positive": all(d > 0 for d in dailies),
            "wr": float(np.mean([s["wr"] for s in ss])),
            "n": float(np.mean([s["n"] for s in ss])),
            "dailies": dailies,
        })

    survivors = [r for r in results if r["all_positive"]]
    print(f"{len(survivors)} of {len(results)} settings made money in all "
          f"{a.blocks} blocks ({len(survivors) / len(results):.0%})\n")

    if not survivors:
        print("Nothing survived. That is the answer: there is no setting")
        print("here that works consistently, and any backtest number you")
        print("pick is a result of choosing after the fact.")
        raise SystemExit(0)

    survivors.sort(key=lambda r: -r["worst"])
    print("RANKED BY WORST BLOCK, which is the number that matters\n")
    print(f"{'gap':>12}{'edge':>6}{'exit':>7}{'stake':>7}"
          f"{'worst blk':>11}{'median blk':>12}{'win':>6}{'trades':>8}")
    print("-" * 72)
    for r in survivors[:10]:
        (glo, ghi), edge, xf, cap = r["params"]
        print(f"{f'${glo:.0f}-{ghi:.0f}':>12}{edge * 100:>5.0f}c"
              f"{(f'{xf:.2f}' if xf else 'none'):>7}{cap:>6.0%}"
              f"{r['worst']:>+10.1%}{r['median']:>+11.1%}"
              f"{r['wr']:>5.0%}{r['n']:>8.0f}")

    best = survivors[0]
    (glo, ghi), edge, xf, cap = best["params"]
    print(f"\nMost consistent setting: gap ${glo:.0f}-{ghi:.0f}, "
          f"edge {edge * 100:.0f}c, exit {xf or 'none'}, stake {cap:.0%}")
    print(f"  per block: " + "  ".join(f"{d:+.1%}" for d in best["dailies"]))
    wk = (1 + best["worst"]) ** 7 - 1
    md = (1 + best["median"]) ** 7 - 1
    print(f"\n  worst block implies {best['worst']:+.1%}/day = {wk:+.0%}/week")
    print(f"  median block implies {best['median']:+.1%}/day = {md:+.0%}/week")
    print(f"  win rate {best['wr']:.0%}, about "
          f"{best['n'] / (len(bl[0]) * 15 / 60 / 24):.0f} trades a day")
