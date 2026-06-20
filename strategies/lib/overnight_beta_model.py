"""Back-compat shim: the overnight-beta decision core was RE-HOMED to
`quantlib.strategy_core.models.overnight_beta` so the battery + the live container share ONE import.
The overnight-beta container's `from strategies.lib.overnight_beta_model import OvernightBetaModel`
keeps working UNCHANGED. See docs/STRATEGY_BATTERY_PORTABILITY.md.
"""
from __future__ import annotations

from quantlib.strategy_core.models.overnight_beta import (
    BetaLegs,
    OvernightBetaModel,
    compute_beta,
)

__all__ = ["BetaLegs", "OvernightBetaModel", "compute_beta"]
