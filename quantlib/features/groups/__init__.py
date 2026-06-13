"""Feature groups — one module per group. Importing this package self-registers every group.

To add a feature, add ONE module here decorated with ``@register`` and import it below. No edits to
base/registry/engine/store/parity/introspect (FEATURE_PLATFORM.md §3.7).
"""
from quantlib.features.groups import price_returns, trade_flow  # noqa: F401
