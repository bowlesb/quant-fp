"""The reversion strategy's declared feature contract — its single (name, version) dependency on the bus.

The consumed feature is ``VwapReversionModel.feature_name`` (``vwap_deviation_{window_m}m``), so the
contract is DERIVED FROM the constructed model — it cannot drift from what the model reads. The version is
pinned to the ``price_volume`` group version the strategy was built against; CI asserts the pin matches the
current schema. The strategy publishes this to ``strategy:features:reversion`` at startup so the pre-deploy
compat gate checks what is actually running. See docs/BUS_FEATURE_ACCESS.md §2.6.
"""

from __future__ import annotations

from quantlib.bus.compat import FeatureReq
from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel

STRATEGY_NAME = "reversion"

# vwap_deviation_* lives in the price_volume group; pin frozen at build time (CI asserts it still matches).
VWAP_DEVIATION_VERSION = "1.2.0"


def contract_for(model: VwapReversionModel) -> tuple[FeatureReq, ...]:
    """The reversion contract for a constructed model — exactly the feature its ``predict`` reads."""
    return (FeatureReq(model.feature_name, VWAP_DEVIATION_VERSION),)
