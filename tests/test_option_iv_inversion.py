"""Black-Scholes round-trip + greek sign tests for the option-IV reconstruction."""

from __future__ import annotations

import math

import pytest

from quantlib.data.option_iv_backfill import _bs_price, _greeks, _implied_vol


@pytest.mark.parametrize("sigma", [0.10, 0.25, 0.45, 0.80])
@pytest.mark.parametrize("is_call", [True, False])
def test_iv_roundtrip(sigma: float, is_call: bool) -> None:
    spot, strike, time_yr, rate = 100.0, 105.0, 30 / 365.0, 0.045
    price = _bs_price(spot, strike, time_yr, rate, sigma, is_call)
    recovered = _implied_vol(price, spot, strike, time_yr, rate, is_call)
    assert recovered is not None
    assert recovered == pytest.approx(sigma, abs=1e-3)


def test_below_model_floor_returns_none() -> None:
    # a call priced below the model's near-zero-vol floor has no implied vol (no-arb)
    spot, strike, time_yr, rate = 120.0, 100.0, 30 / 365.0, 0.045
    floor = _bs_price(spot, strike, time_yr, rate, 1e-4, True)
    assert _implied_vol(floor - 1.0, spot, strike, time_yr, rate, True) is None


def test_call_delta_positive_put_delta_negative() -> None:
    spot, strike, time_yr, rate, sigma = 100.0, 100.0, 30 / 365.0, 0.045, 0.30
    call = _greeks(spot, strike, time_yr, rate, sigma, True)
    put = _greeks(spot, strike, time_yr, rate, sigma, False)
    assert 0.0 < call["delta"] < 1.0
    assert -1.0 < put["delta"] < 0.0
    # gamma/vega are right-agnostic and positive; put/call gamma equal
    assert call["gamma"] > 0 and call["vega"] > 0
    assert call["gamma"] == pytest.approx(put["gamma"], rel=1e-9)


def test_atm_call_delta_near_half() -> None:
    spot, strike, time_yr, rate, sigma = 100.0, 100.0, 30 / 365.0, 0.045, 0.30
    call = _greeks(spot, strike, time_yr, rate, sigma, True)
    assert call["delta"] == pytest.approx(0.5, abs=0.08)


def test_put_call_parity_holds() -> None:
    spot, strike, time_yr, rate, sigma = 100.0, 95.0, 45 / 365.0, 0.045, 0.35
    call = _bs_price(spot, strike, time_yr, rate, sigma, True)
    put = _bs_price(spot, strike, time_yr, rate, sigma, False)
    # C - P == S - K*exp(-rT)
    assert call - put == pytest.approx(spot - strike * math.exp(-rate * time_yr), abs=1e-6)
