"""Quantify the TWAP lock-in effect. Validates closed form against Monte Carlo."""
import numpy as np
from btc15.twap import _norm_sf
from btc15.twap import (forecast_settlement, simulate_settlement,
                        naive_spot_sd, remaining_offsets)

SPOT = 100_000.0
ANN_VOL = 0.50
SIG = SPOT * ANN_VOL / np.sqrt(365 * 24 * 3600)   # per-second dollars

print(f"BTC ${SPOT:,.0f} at {ANN_VOL:.0%} annualised")
print(f"per-second sigma = ${SIG:.2f}   per-15min = ${SIG*np.sqrt(900):.0f}\n")

print("A. Closed form vs Monte Carlo (settlement SD, dollars)")
print(f"{'k':>5} {'locked':>7} {'closed':>9} {'MC':>9} {'naive spot':>11} {'ratio':>7}")
rng = np.random.default_rng(7)
for k in (900, 300, 120, 60, 45, 30, 20, 10, 5, 2):
    obs = SPOT + np.cumsum(rng.normal(0, SIG, 60))[:60 - len(remaining_offsets(k))]
    f = forecast_settlement(obs, k, SPOT, SIG)
    _, s = simulate_settlement(obs, k, SPOT, SIG, SPOT, n_paths=40000, seed=1)
    naive = naive_spot_sd(k, SIG)
    print(f"{k:>5} {f.locked_fraction:>6.0%} {f.sd:>9.2f} {s.std():>9.2f} "
          f"{naive:>11.2f} {naive/f.sd if f.sd else float('inf'):>7.2f}x")

print("\nB. The mispricing. Index sits D dollars above strike, k seconds left.")
print("   TWAP-aware P(Up) vs what a spot-watcher computes.\n")
print(f"{'k':>4} {'D':>7} {'E[S]-K':>8} {'true P':>8} {'naive P':>8} {'gap':>7}")
for k in (45, 30, 20, 10):
    for D in (10, 25, 50, 100):
        # index stepped to K+D at window start and held: realistic "drifted then flat"
        n_obs = 60 - len(remaining_offsets(k))
        obs = np.full(n_obs, SPOT + D)
        f = forecast_settlement(obs, k, SPOT + D, SIG)
        K = SPOT
        true_p = f.prob_up(K)
        naive_p = float(_norm_sf((K - (SPOT + D)) / naive_spot_sd(k, SIG)))
        print(f"{k:>4} {D:>7.0f} {f.mean-K:>8.1f} {true_p:>8.1%} {naive_p:>8.1%} "
              f"{true_p-naive_p:>+7.1%}")

print("\nC. Fat tails: Student-t increments vs Gaussian, same variance")
print(f"{'k':>4} {'D':>7} {'gauss':>8} {'t(4)':>8} {'t(3)':>8}")
for k in (30, 15):
    for D in (25, 50, 100):
        n_obs = 60 - len(remaining_offsets(k))
        obs = np.full(n_obs, SPOT + D)
        g, _ = simulate_settlement(obs, k, SPOT+D, SIG, SPOT, 60000, None, 3)
        t4, _ = simulate_settlement(obs, k, SPOT+D, SIG, SPOT, 60000, 4.0, 3)
        t3, _ = simulate_settlement(obs, k, SPOT+D, SIG, SPOT, 60000, 3.0, 3)
        print(f"{k:>4} {D:>7.0f} {g:>8.2%} {t4:>8.2%} {t3:>8.2%}")
