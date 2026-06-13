"""Feature groups — one module per group. Importing this package self-registers every group.

To add a feature, add ONE module here decorated with ``@register`` and import it below. No edits to
base/registry/engine/store/parity/introspect (FEATURE_PLATFORM.md §3.7).
"""
from quantlib.features.groups import (  # noqa: F401
    asset_flags,
    calendar,
    calendar_events,
    candlestick,
    cross_sectional_rank,
    distribution,
    efficiency,
    market_beta,
    market_context,
    microstructure_burst,
    momentum,
    multi_day,
    ohlc_vol,
    price_levels,
    price_returns,
    price_volume,
    prior_day,
    quote_spread,
    return_dynamics,
    sector,
    technical,
    trade_flow,
    trend_quality,
    volatility,
    volume,
)
