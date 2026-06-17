"""Unit tests for OvernightBetaModel — the certified W11 overnight-beta signal as a pure function.

Hand-built return panels with KNOWN betas lock in the OLS beta + the high-minus-low-quintile leg selection.
"""
from __future__ import annotations

import numpy as np
import pytest

from strategies.lib.overnight_beta_model import BetaLegs, OvernightBetaModel, compute_beta


def test_compute_beta_recovers_known_slope() -> None:
    rng = np.random.default_rng(0)
    mkt = rng.normal(0, 0.01, 80)
    # name = 1.5 * market + small idiosyncratic noise -> beta ~ 1.5
    name = 1.5 * mkt + rng.normal(0, 0.0005, 80)
    assert compute_beta(name, mkt) == pytest.approx(1.5, abs=0.1)


def test_compute_beta_nan_on_insufficient_or_degenerate() -> None:
    assert np.isnan(compute_beta(np.array([0.01, 0.02]), np.array([0.01, 0.02])))  # <20 obs
    # zero-variance market -> undefined
    assert np.isnan(compute_beta(np.full(40, 0.01), np.full(40, 0.005)))


def test_select_legs_high_minus_low_beta() -> None:
    rng = np.random.default_rng(1)
    mkt = rng.normal(0, 0.01, 60)
    # 10 names with betas 0.2,0.4,...,2.0 by construction
    target_betas = {f"S{i}": 0.2 * (i + 1) for i in range(10)}
    returns = {s: b * mkt + rng.normal(0, 0.0003, 60) for s, b in target_betas.items()}
    model = OvernightBetaModel(beta_window=60, quantile=0.2)
    legs = model.select_legs(returns, mkt)
    assert isinstance(legs, BetaLegs)
    # quintile of 10 = 2 names: long the 2 highest-beta (S8,S9), short the 2 lowest (S0,S1)
    assert set(legs.long) == {"S8", "S9"}
    assert set(legs.short) == {"S0", "S1"}
    # estimated betas are close to the constructed ones
    assert legs.betas["S9"] > legs.betas["S0"]
    assert legs.betas["S9"] == pytest.approx(2.0, abs=0.15)


def test_select_legs_drops_nan_beta_names() -> None:
    rng = np.random.default_rng(2)
    mkt = rng.normal(0, 0.01, 60)
    returns = {f"S{i}": (0.3 * (i + 1)) * mkt + rng.normal(0, 0.0003, 60) for i in range(8)}
    returns["BAD"] = np.full(60, np.nan)  # no finite beta -> dropped
    model = OvernightBetaModel(beta_window=60, quantile=0.25)
    legs = model.select_legs(returns, mkt)
    assert "BAD" not in legs.betas
    assert "BAD" not in legs.long and "BAD" not in legs.short


def test_select_legs_empty_when_too_few_names() -> None:
    mkt = np.random.default_rng(3).normal(0, 0.01, 60)
    legs = OvernightBetaModel().select_legs({"A": mkt, "B": mkt}, mkt)
    assert legs.long == () and legs.short == ()


def test_deterministic() -> None:
    rng = np.random.default_rng(4)
    mkt = rng.normal(0, 0.01, 60)
    returns = {f"S{i}": (0.2 * (i + 1)) * mkt + rng.normal(0, 0.0003, 60) for i in range(10)}
    model = OvernightBetaModel()
    a = model.select_legs(returns, mkt)
    b = model.select_legs(returns, mkt)
    assert a.long == b.long and a.short == b.short


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
