"""Per-minute tick aggregation for the live capture path — the consumer for Monday's trade/quote flow.

Turns each minute's buffered trades and quotes into the ``minute_agg`` tick columns the ``trade_flow``
and ``quote_spread`` groups consume (``loaders._MINUTE_AGG_SQL``). Built ENTIRELY on the parity-true
``quantlib.aggregates`` primitives: ``TickState`` is threaded per symbol across minutes so the live,
minute-by-minute aggregation is identical to a single batch pass over the same ordered ticks — the SAME
guarantee the historical backfiller relies on. This is the in-process tick state manager, unified with
backfill by construction (not a separate live-only path).
"""
from __future__ import annotations

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
)

# The minute_agg columns the trade_flow + quote_spread groups consume from the tick flow.
TICK_COLUMNS: tuple[str, ...] = (
    "n_trades", "signed_volume", "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size",
)


def aggregate_symbol_minute(trades: list[TradeTick], quotes: list[QuoteTick], state: TickState) -> dict[str, float]:
    """One symbol's tick columns for one minute. ``state`` is MUTATED (threaded into the next minute) so
    the trade-sign classification matches a batch pass — the live==backfill guarantee at the tick layer.
    A tradeless/quoteless minute is a real condition (all-zero aggregate), not missing data."""
    trade_agg = aggregate_trades(trades, state)
    quote_agg = aggregate_quotes(quotes)
    return {
        "n_trades": float(trade_agg.n_trades),
        "signed_volume": trade_agg.signed_volume,
        "mean_spread_bps": quote_agg.mean_spread_bps,
        "quote_imbalance": quote_agg.quote_imbalance,
        "mean_bid_size": quote_agg.mean_bid_size,
        "mean_ask_size": quote_agg.mean_ask_size,
    }


def enrich_bars_with_ticks(
    bars: list[dict],
    trades_by_symbol: dict[str, list[TradeTick]],
    quotes_by_symbol: dict[str, list[QuoteTick]],
    states: dict[str, TickState],
) -> list[dict]:
    """Merge each bar row with its symbol's aggregated trade/quote columns for the minute. ``states`` is
    a per-symbol ``{symbol: TickState}`` the CALLER owns and threads across minutes (so live == batch);
    a symbol seen for the first time gets a fresh state. Symbols with no ticks this minute get the
    all-zero aggregate — the bar still computes its price features, just with empty tick columns."""
    enriched = []
    for bar in bars:
        symbol = bar["S"]
        if symbol not in states:
            states[symbol] = TickState()
        trades = trades_by_symbol[symbol] if symbol in trades_by_symbol else []
        quotes = quotes_by_symbol[symbol] if symbol in quotes_by_symbol else []
        enriched.append({**bar, **aggregate_symbol_minute(trades, quotes, states[symbol])})
    return enriched
