"""Back-compat / convenience shim: ``CryptoMomentumModel`` lives in
``quantlib.strategy_core.models.crypto_momentum`` so the battery + the live container share ONE import.
The crypto-momentum container imports it from here, mirroring how reversion imports VwapReversionModel.
See docs/STRATEGY_BATTERY_PORTABILITY.md.
"""

from __future__ import annotations

from quantlib.strategy_core.models.crypto_momentum import CryptoMomentumModel

__all__ = ["CryptoMomentumModel"]
