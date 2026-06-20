"""Shared strategy decision cores — the ONE home both the battery and the live containers import.

Re-homed from `strategies/lib/` so the backtest (battery) and live containers share a single
implementation of each decision (parity-by-construction at the strategy layer; see
docs/STRATEGY_BATTERY_PORTABILITY.md). The cores are bus-free (typed against `FeatureRow`, not the
redis-backed `FeatureVector`), so importing them never pulls the bus into the battery.

  - single_name:    `Model`/`Prediction`/`MockMLModel` — the per-vector decision contract.
  - vwap_reversion: `VwapReversionModel` — below-VWAP -> long reversion probability.
  - overnight_beta: `OvernightBetaModel`/`BetaLegs`/`compute_beta` — the certified high-minus-low-beta
                    overnight L/S leg selection (the cross-sectional `select_legs` decide).
"""
from __future__ import annotations

from quantlib.strategy_core.models.overnight_beta import (
    BetaLegs,
    OvernightBetaModel,
    compute_beta,
)
from quantlib.strategy_core.models.single_name import MockMLModel, Model, Prediction
from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel

__all__ = [
    "BetaLegs",
    "MockMLModel",
    "Model",
    "OvernightBetaModel",
    "Prediction",
    "VwapReversionModel",
    "compute_beta",
]
