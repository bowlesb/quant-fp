"""Feature groups — one module per group. Importing this package self-registers every group.

To add a feature, add ONE module here decorated with ``@register`` and import it below. No edits to
base/registry/engine/store/parity/introspect (FEATURE_PLATFORM.md §3.7).
"""
from quantlib.features.groups import (  # noqa: F401
    calendar,
    microstructure_burst,
    multi_day,
    price_levels,
    price_returns,
    quote_spread,
    trade_flow,
    volatility,
    volume,
)
