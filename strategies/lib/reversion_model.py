"""Back-compat shim: `VwapReversionModel` was RE-HOMED to
`quantlib.strategy_core.models.vwap_reversion` so the battery + the live container share ONE import.
The reversion container's `from strategies.lib.reversion_model import VwapReversionModel` keeps working
UNCHANGED. See docs/STRATEGY_BATTERY_PORTABILITY.md.
"""
from __future__ import annotations

from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel

__all__ = ["VwapReversionModel"]
