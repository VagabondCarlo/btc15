#!/usr/bin/env python3
"""What a 25% bet size does, and why these prices make sizing treacherous.

Two separate questions get mixed up when people argue about bet size.

  1. Given a known edge, how much should you bet? Kelly answers that.
  2. How wrong is Kelly when the win rate you fed it is an estimate?

For contracts priced near a dollar the second question dominates, because
the payoff is tiny relative to the stake. Buying at 95 cents risks 95 to win
5. A win rate that is off by two points does not shave the edge, it reverses
it, and the Kelly stake swings from huge to negative.
"""

from __future__ import annotations

import numpy as np

from btc15.fees import THETA_TAKER, kelly_fraction


def cost(p, theta=THETA_TAKER):
    return p + theta * p * (1 - p)


print("1. HOW SENSITIVE IS THE RIGHT BET SIZE TO A SMALL ERROR?\n")
print(f"{'price':>7} {'break-even':>11} {'assumed win':>12} {'Kelly':>8}"
      f" {'if 3pts worse':>14} {'if 5pts worse':>14}")
for px, assumed in ((0.70, 0.78), (0.80, 0.85), (0.90, 0.93),
                    (0.95, 0.975), (0.99, 0.999)):
    be = cost(px)
    k0 = kelly_fraction(assumed, px)
    k3 = kelly_fraction(assumed - 0.03, px)
    k5 = kelly_fraction(assumed - 0.05, px)
    print(f"{px:>7.2f} {be:>11.3f} {assumed:>12.1%} {k0:>8.1%}"
          f" {k3:>14.1%} {k5:>14.1%}")
print("\n  A few points of error wipes out the whole position. At 99 cents")
print("  the gap between 'bet most of the account' and 'do not bet' is one")
print("  tenth of one percent of win rate, which nobody can measure.\n")

print("2. GROWTH AT DIFFERENT BET FRACTIONS, WITH A GENUINE EDGE\n")
print("  Buying at 0.80 with a true 85% win rate. That is a real edge of")
print(f"  {0.85 - cost(0.80):+.3f} per contract. Full Kelly here is "
      f"{kelly_fraction(0.85, 0.80):.0%}.\n")
rng = np.random.default_rng(42)
PATHS, TRADES, P, PX = 4000, 500, 0.85, 0.80
c = cost(PX)
print(f"{'bet size':>10} {'median result':>15} {'worst 5%':>12}"
      f" {'chance of losing':>18}")
for frac in (0.01, 0.02, 0.05, 0.10, 0.25, 0.50):
    bank = np.ones(PATHS)
    for _ in range(TRADES):
        stake = bank * frac
        won = rng.random(PATHS) < P
        bank = bank + np.where(won, stake * (1 - c) / c, -stake)
        bank = np.maximum(bank, 1e-12)
    med = np.median(bank)
    p5 = np.percentile(bank, 5)
    lose = np.mean(bank < 1.0)
    print(f"{frac:>10.0%} {med:>15,.2f}x {p5:>11,.3f}x {lose:>17.1%}")

print("\n3. THE SAME BET SIZES WHEN THE EDGE IS NOT REAL\n")
print("  Identical setup, except the true win rate is 80%, not 85%. The")
print("  market said 0.80 and the market was right. Nothing else changes.\n")
print(f"{'bet size':>10} {'median result':>15} {'worst 5%':>12}"
      f" {'chance of losing':>18}")
for frac in (0.01, 0.05, 0.10, 0.25, 0.50):
    bank = np.ones(PATHS)
    for _ in range(TRADES):
        stake = bank * frac
        won = rng.random(PATHS) < 0.80
        bank = bank + np.where(won, stake * (1 - c) / c, -stake)
        bank = np.maximum(bank, 1e-12)
    print(f"{frac:>10.0%} {np.median(bank):>15,.4f}x "
          f"{np.percentile(bank, 5):>11,.4f}x {np.mean(bank < 1.0):>17.1%}")
