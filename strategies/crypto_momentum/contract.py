"""The crypto-momentum strategy's declared feature contract — its single (name, version) dependency on
the crypto feature bus.

The consumed feature is ``CryptoMomentumModel.feature_name`` (``ret_{window_m}m``), so the contract is
DERIVED FROM the constructed model — it cannot drift from what the model reads. The version is pinned to
the ``price_returns`` group version the strategy was built against; CI asserts the pin matches the current
schema. The strategy publishes this to ``strategy:features:cryptomomentum`` at startup so the pre-deploy
compat gate checks what is actually running. See docs/BUS_FEATURE_ACCESS.md §2.6.
"""

from __future__ import annotations

from quantlib.bus.compat import FeatureReq
from quantlib.strategy_core.models.crypto_momentum import CryptoMomentumModel

STRATEGY_NAME = "cryptomomentum"

# ret_* lives in the price_returns group; pin frozen at build time (CI asserts it still matches).
RET_VERSION = "1.0.0"


def contract_for(model: CryptoMomentumModel) -> tuple[FeatureReq, ...]:
    """The crypto-momentum contract for a constructed model — exactly the feature its ``predict`` reads."""
    return (FeatureReq(model.feature_name, RET_VERSION),)
