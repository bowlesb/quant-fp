"""Tests for the overnight-beta strategy's PURE logic: the enter-gate (kill switch, close-auction window,
gross cap), the adverse-slippage math (the deliverable), and the order-fill reader. No network/DB.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from strategies.overnight_beta.position_store import slippage_bps
from strategies.overnight_beta.strategy import (
    OvernightBetaConfig,
    evaluate_enter_gate,
    order_filled,
)


def _config(**overrides: object) -> OvernightBetaConfig:
    base: dict[str, object] = {
        "notional_usd": 100.0,
        "max_names_per_leg": 20,
        "max_gross_notional_usd": 5000.0,
        "rebalance_days": 21,
        "beta_window": 60,
        "quantile": 0.2,
        "enabled": True,
        "exclude": ("COIN", "MARA"),
        "loop_sleep_sec": 60,
    }
    base.update(overrides)
    return OvernightBetaConfig(**base)  # type: ignore[arg-type]


def test_gate_allows_in_close_window() -> None:
    gate = evaluate_enter_gate(_config(), market_open=True, minutes_to_close=5.0, n_entered=0,
                               gross_notional=0.0, prospective_names=40)
    assert gate.allowed and gate.reason == "ok"


def test_gate_kill_switch() -> None:
    gate = evaluate_enter_gate(_config(enabled=False), market_open=True, minutes_to_close=5.0,
                               n_entered=0, gross_notional=0.0, prospective_names=40)
    assert not gate.allowed and gate.reason == "kill_switch_off"


def test_gate_only_in_close_auction_window() -> None:
    # mid-day (60 min to close) -> wait; closed -> wait
    assert not evaluate_enter_gate(_config(), True, 60.0, 0, 0.0, 40).allowed
    assert evaluate_enter_gate(_config(), True, 60.0, 0, 0.0, 40).reason == "not_close_auction_window"
    assert not evaluate_enter_gate(_config(), False, 5.0, 0, 0.0, 40).allowed


def test_gate_no_double_enter() -> None:
    gate = evaluate_enter_gate(_config(), True, 5.0, n_entered=10, gross_notional=1000.0, prospective_names=40)
    assert not gate.allowed and gate.reason == "already_entered_this_overnight"


def test_gate_gross_notional_cap() -> None:
    # 40 names * $100 = $4000 prospective; +$1500 existing = $5500 > $5000 cap
    gate = evaluate_enter_gate(_config(max_gross_notional_usd=5000.0), True, 5.0, 0,
                               gross_notional=1500.0, prospective_names=40)
    assert not gate.allowed and gate.reason == "max_gross_notional"


def test_slippage_bps_adverse_sign() -> None:
    # BUY filling ABOVE the reference close = adverse (positive cost)
    assert slippage_bps("buy", 100.0, 100.05) == pytest.approx(5.0)
    # BUY filling BELOW reference = favorable (negative cost)
    assert slippage_bps("buy", 100.0, 99.95) == pytest.approx(-5.0)
    # SELL filling BELOW reference = adverse (positive cost)
    assert slippage_bps("sell", 100.0, 99.95) == pytest.approx(5.0)
    # SELL filling ABOVE reference = favorable
    assert slippage_bps("sell", 100.0, 100.05) == pytest.approx(-5.0)
    assert slippage_bps("buy", 0.0, 100.0) == 0.0  # guard


@dataclass
class FakeOrder:
    filled_avg_price: float | None
    filled_qty: float | None


def test_order_filled() -> None:
    assert order_filled(FakeOrder(100.0, 0.5)) == (100.0, 0.5)
    assert order_filled(FakeOrder(None, None)) is None
    assert order_filled(FakeOrder(100.0, 0.0)) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
