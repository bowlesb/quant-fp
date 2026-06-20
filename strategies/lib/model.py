"""Back-compat shim: the single-name decision core was RE-HOMED to
`quantlib.strategy_core.models.single_name` so the battery + the live containers share ONE import.

The live containers still import `from strategies.lib.model import Model, MockMLModel, Prediction`;
this re-export keeps those imports working UNCHANGED (zero behavior change), while new code imports
from `quantlib.strategy_core.models`. See docs/STRATEGY_BATTERY_PORTABILITY.md.
"""
from __future__ import annotations

from quantlib.strategy_core.models.single_name import (
    MockMLModel,
    Model,
    Prediction,
    _hash_to_unit_interval,
)

__all__ = ["MockMLModel", "Model", "Prediction", "_hash_to_unit_interval"]
