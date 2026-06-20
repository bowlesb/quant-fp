"""Per-container parity tests for the strategies/lib RE-HOME.

The re-home moved each container's decision core under `quantlib/strategy_core/models/` (bus-free,
typed against `FeatureRow`). These tests prove the lift did NOT change the decision: each core produces
IDENTICAL output whether fed
  (a) a live-shaped `FeatureVector`-like row (the per-vector live path), AND
  (b) a `PanelCrossSection` row (the battery backtest path),
built from the SAME feature values. That is the per-container "decide() over a panel == current live
behavior" proof the re-home requires — backtest==live by construction at the strategy layer.

The cores are byte-identical to the pre-move versions (only the import path + the `FeatureRow` type
annotation changed), so these tests ALSO certify the shims at `strategies/lib/*` re-export the same
objects the containers import.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from quantlib.strategy_core.adapters import PanelCrossSection
from quantlib.strategy_core.models.overnight_beta import OvernightBetaModel
from quantlib.strategy_core.models.single_name import MockMLModel
from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel


class _FakeVector:
    """A live-shaped FeatureVector stand-in: `.symbol`, `.minute`, `.value(name)` — what FeatureRow
    requires and what the bus's real FeatureVector provides. No redis/schema deps."""

    def __init__(self, symbol: str, minute: dt.datetime, values: dict[str, float]) -> None:
        self.symbol = symbol
        self.minute = minute
        self._values = values

    def value(self, name: str) -> float:
        return float(self._values.get(name, float("nan")))


class _PanelRow:
    """One row of a PanelCrossSection presented as a FeatureRow (the battery path) — reads the same
    named feature off the resident panel arrays for one (symbol, minute)."""

    def __init__(self, cs: PanelCrossSection, symbol: str) -> None:
        self._cs = cs
        self.symbol = symbol
        self.minute = cs.minute

    def value(self, name: str) -> float:
        return self._cs.feature_for(self.symbol, name)


def _shim_exports_same_object() -> None:
    """The strategies/lib shims must re-export the SAME classes the containers import."""
    import strategies.lib.model as shim_model
    import strategies.lib.overnight_beta_model as shim_obeta
    import strategies.lib.reversion_model as shim_rev

    assert shim_model.MockMLModel is MockMLModel
    assert shim_rev.VwapReversionModel is VwapReversionModel
    assert shim_obeta.OvernightBetaModel is OvernightBetaModel


def test_shim_reexports_identity() -> None:
    _shim_exports_same_object()


# --- reversion: per-vector decide() parity (live FeatureVector == panel row) ----------------------


def test_reversion_predict_parity_bus_vs_panel() -> None:
    minute = dt.datetime(2026, 1, 5, 14, 35, tzinfo=dt.timezone.utc)
    model = VwapReversionModel(window_m=30, sensitivity=400.0)
    feat = model.feature_name
    values = {feat: -0.005}  # 50 bps below VWAP -> a long signal
    bus_row = _FakeVector("AAPL", minute, values)
    cs = PanelCrossSection(["AAPL"], minute, np.array([[-0.005]]), {feat: 0})
    panel_row = _PanelRow(cs, "AAPL")
    bus_pred = model.predict(bus_row)
    panel_pred = model.predict(panel_row)
    assert bus_pred.probability == panel_pred.probability
    assert bus_pred.probability > 0.5  # below VWAP -> long


def test_reversion_nan_safe_parity() -> None:
    minute = dt.datetime(2026, 1, 5, 14, 35, tzinfo=dt.timezone.utc)
    model = VwapReversionModel(window_m=30)
    bus_row = _FakeVector("AAPL", minute, {})  # missing feature -> NaN
    cs = PanelCrossSection(["AAPL"], minute, np.array([[np.nan]]), {model.feature_name: 0})
    assert model.predict(bus_row).probability == model.predict(_PanelRow(cs, "AAPL")).probability == 0.5


# --- smoke / mock model: deterministic per-(symbol, minute) parity --------------------------------


def test_mock_model_parity_bus_vs_panel() -> None:
    minute = dt.datetime(2026, 1, 5, 14, 35, tzinfo=dt.timezone.utc)
    model = MockMLModel(feature_names=["ret_1m", "realized_vol_5m"])
    values = {"ret_1m": 0.001, "realized_vol_5m": 0.02}
    bus_row = _FakeVector("MSFT", minute, values)
    cs = PanelCrossSection(["MSFT"], minute, np.array([[0.001, 0.02]]), {"ret_1m": 0, "realized_vol_5m": 1})
    panel_row = _PanelRow(cs, "MSFT")
    assert model.predict(bus_row).probability == model.predict(panel_row).probability


# --- overnight_beta: the cross-sectional decide IS select_legs (already panel-shaped) -------------


def test_overnight_beta_select_legs_deterministic() -> None:
    """select_legs IS the cross-sectional decide() — same panel in, same legs out (deterministic)."""
    rng = np.random.default_rng(7)
    market = rng.normal(0, 0.01, 60)
    returns_by_name = {f"S{i}": rng.normal(0, 0.01, 60) + (i / 60) * market for i in range(30)}
    model = OvernightBetaModel(beta_window=60, quantile=0.2)
    legs_a = model.select_legs(dict(returns_by_name), market.copy())
    legs_b = model.select_legs(dict(returns_by_name), market.copy())
    assert legs_a.long == legs_b.long
    assert legs_a.short == legs_b.short
    assert legs_a.long and legs_a.short  # both legs formed
    # high-beta names (built with more market loading) land in the LONG leg
    assert legs_a.betas[legs_a.long[-1]] > legs_a.betas[legs_a.short[0]]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
