"""Per-minute tick aggregation for the live capture path — the consumer for Monday's trade/quote flow.

Turns each minute's buffered trades and quotes into the ``minute_agg`` tick columns the ``trade_flow``
and ``quote_spread`` groups consume (``loaders._MINUTE_AGG_SQL``). Built ENTIRELY on the parity-true
``quantlib.aggregates`` primitives: ``TickState`` is threaded per symbol across minutes so the live,
minute-by-minute aggregation is identical to a single batch pass over the same ordered ticks — the SAME
guarantee the historical backfiller relies on. This is the in-process tick state manager, unified with
backfill by construction (not a separate live-only path).
"""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
)

# The raw per-trade frame the tick_runlength / microstructure_burst groups consume (InputSpec name="trades").
# SAME schema + column order the backfill loader produces (loaders.TICK_SCHEMA) — the one tick shape both
# the live worker and the historical backfill feed those groups, so Layer-C parity holds by construction.
TRADES_SCHEMA: dict[str, pl.PolarsDataType] = {
    "symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64,
}

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


def trades_frame(trades_by_symbol: dict[str, list[TradeTick]]) -> pl.DataFrame:
    """Build the raw ``trades`` frame (symbol, ts, price, size) for ONE minute's bucketed trades — the
    InputSpec the ``tick_runlength`` / ``microstructure_burst`` groups declare. ``ts`` is reconstructed
    as a UTC datetime from each tick's epoch seconds; the schema + column order match the backfill loader
    (``loaders.TICK_SCHEMA``) so the SAME group code runs on live and backfill (Layer-C parity). An empty
    minute (no subscribed trades) yields an empty, correctly-typed frame — the groups return no rows for
    it, which is the honest 'no trades this minute', not a fabricated zero."""
    rows = [
        {"symbol": symbol, "ts": datetime.fromtimestamp(tick.ts_epoch, tz=timezone.utc),
         "price": tick.price, "size": tick.size}
        for symbol, ticks in trades_by_symbol.items()
        for tick in ticks
    ]
    if not rows:
        return pl.DataFrame(schema=TRADES_SCHEMA)
    return pl.DataFrame(rows, schema=TRADES_SCHEMA)


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
