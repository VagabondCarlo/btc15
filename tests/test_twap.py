"""Tests for the settlement maths."""
import numpy as np
import pytest

from btc15.fees import breakeven_prob, kelly_fraction, taker_fee
from btc15.twap import (forecast_settlement, observed_count,
                        realized_sigma_per_sec, remaining_offsets,
                        simulate_settlement)


def test_offsets_inside_final_minute():
    assert len(remaining_offsets(30)) == 30
    assert observed_count(30) == 30
    assert list(remaining_offsets(3)) == [1, 2, 3]


def test_offsets_before_final_minute():
    assert len(remaining_offsets(300)) == 60
    assert observed_count(300) == 0
    assert remaining_offsets(60)[0] == 1


def test_nothing_left_at_expiry():
    assert len(remaining_offsets(0)) == 0
    f = forecast_settlement(np.full(60, 100.0), 0, 100.0, 5.0)
    assert f.sd == 0.0
    assert f.prob_up(99.0) == 1.0
    assert f.prob_up(101.0) == 0.0


def test_tie_resolves_up():
    f = forecast_settlement(np.full(60, 100.0), 0, 100.0, 5.0)
    assert f.prob_up(100.0) == 1.0


def test_closed_form_matches_monte_carlo():
    rng = np.random.default_rng(0)
    for k in (10, 30, 120):
        obs = 100_000 + np.cumsum(rng.normal(0, 9, max(observed_count(k), 1)))
        f = forecast_settlement(obs, k, 100_000.0, 9.0)
        _, s = simulate_settlement(obs, k, 100_000.0, 9.0, 100_000.0,
                                   n_paths=40000, seed=1)
        assert f.sd == pytest.approx(s.std(), rel=0.06)


def test_averaging_shrinks_uncertainty():
    """Half the settlement average is fixed at 30s, so SD must be well under
    the plain diffusion a spot watcher would assume."""
    obs = np.full(30, 100_000.0)
    f = forecast_settlement(obs, 30, 100_000.0, 9.0)
    assert f.sd < 9.0 * np.sqrt(30) / 3
    assert f.locked_fraction == 0.5


def test_missing_prints_do_not_bias():
    full = forecast_settlement(np.full(30, 100.0), 30, 100.0, 1.0)
    short = forecast_settlement(np.full(20, 100.0), 30, 100.0, 1.0)
    assert full.mean == pytest.approx(short.mean)


def test_sigma_ignores_single_spike():
    """A lone glitch must not blow up the estimate. Checked on a smooth
    series, where the median filter alone collapses, and on a noisy one."""
    smooth = np.concatenate([np.full(200, 100.0) + np.arange(200) * 0.01,
                             [5000.0]])
    # A flat feed must report "no estimate", never zero volatility.
    assert np.isnan(realized_sigma_per_sec(smooth))

    flat = np.full(500, 83000.0)
    assert np.isnan(realized_sigma_per_sec(flat))

    rng = np.random.default_rng(3)
    noisy = 100_000 + np.cumsum(rng.normal(0, 9.0, 600))
    clean = realized_sigma_per_sec(noisy)
    spiked = realized_sigma_per_sec(np.append(noisy, noisy[-1] + 50_000))
    assert clean == pytest.approx(9.0, rel=0.25)
    assert spiked == pytest.approx(clean, rel=0.15)


def test_fee_and_breakeven():
    assert taker_fee(1000, 0.50) == pytest.approx(17.38, abs=0.01)
    assert breakeven_prob(0.50) == pytest.approx(0.5174, abs=1e-4)
    assert breakeven_prob(0.95) < breakeven_prob(0.50) + 0.5


def test_kelly_is_zero_without_edge():
    assert kelly_fraction(0.50, 0.50) == 0.0
    assert kelly_fraction(0.40, 0.60) == 0.0
    assert 0 < kelly_fraction(0.90, 0.80) < 1


def test_blended_sigma_sits_between_its_inputs():
    """Blending exists to stop a quiet ten minutes from making the model
    overconfident, so the result must be pulled toward the long run."""
    from btc15.twap import blended_sigma
    rng = np.random.default_rng(11)
    calm = np.cumsum(rng.normal(0, 1.0, 600)) + 86000
    busy = np.cumsum(rng.normal(0, 8.0, 3000)) + 86000
    b = blended_sigma(calm, busy, shrink=0.5)
    assert realized_sigma_per_sec(calm) < b < realized_sigma_per_sec(busy)


def test_blended_sigma_endpoints():
    from btc15.twap import blended_sigma
    rng = np.random.default_rng(12)
    a = np.cumsum(rng.normal(0, 1.0, 800)) + 86000
    b = np.cumsum(rng.normal(0, 6.0, 800)) + 86000
    assert blended_sigma(a, b, 0.0) == pytest.approx(realized_sigma_per_sec(a))
    assert blended_sigma(a, b, 1.0) == pytest.approx(realized_sigma_per_sec(b))


def test_blended_sigma_survives_one_dead_input():
    """A stale feed must not silently become zero volatility."""
    from btc15.twap import blended_sigma
    rng = np.random.default_rng(13)
    live = np.cumsum(rng.normal(0, 3.0, 800)) + 86000
    dead = np.full(800, 86000.0)
    assert blended_sigma(live, dead) == pytest.approx(
        realized_sigma_per_sec(live))
    assert np.isnan(blended_sigma(dead, dead))
