"""Read the already-downloaded ``/store/raw`` minute bars + tick tape into the engine input frames.

The ACQUIRE stage (``quantlib.data.raw_backfill``) downloads raw minute bars, trades, and quotes ONCE into
``/store/raw/<bars|trades|quotes>/symbol=<S>/date=<YYYY-MM-DD>/data.parquet``. The MATERIALIZE stage reads
that raw substrate instead of re-fetching from Alpaca — download-once, compute-many, with a clean
segregation between acquiring the tape and computing features from it.

The bar frame is cell-identical in schema to ``backfill_bars`` (the prior re-fetch path), and the
tick-enriched ``minute_agg`` / ``trades`` frames are cell-identical to what ``tick_capture`` aggregates
live, so the feature engine cannot tell which source produced them — parity by construction. The tick
aggregation here is the SAME logic ``parity_audit`` exercises (already proven to reproduce the live tick
columns); centralising it here lets the durable backfill materialize feed the order-flow groups
(trade_flow / quote_spread / liquidity / signed_trade_ratio / tick_runlength / microstructure_burst).
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl

from quantlib.data.raw_backfill import partition_dir
from quantlib.features.backfill_bars import BARS_SCHEMA

TRADES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "price": pl.Float64,
    "size": pl.Float64,
}


def load_raw_minute_agg(raw_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """Minute bars for ``symbols`` on ``day`` read from the ``/store/raw/bars`` partitions.

    Returns the SAME ``(symbol, minute, open, close, high, low, volume)`` shape as
    ``backfill_bars`` — ``minute`` is the raw ``ts`` column and ``volume`` is cast Int64 → Float64.
    Symbols with no partition on disk are absent from the result (mirroring how Alpaca returns no rows
    for a no-data symbol), so the caller never needs to special-case missing raw days.
    """
    target_day = dt.date.fromisoformat(day)
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        path = os.path.join(partition_dir(raw_root, "bars", symbol, target_day), "data.parquet")
        if not os.path.exists(path):
            continue
        raw = pl.read_parquet(path)
        frame = raw.select(
            pl.col("symbol"),
            pl.col("ts").alias("minute"),
            pl.col("open"),
            pl.col("close"),
            pl.col("high"),
            pl.col("low"),
            pl.col("volume").cast(pl.Float64),
        ).cast(BARS_SCHEMA)
        if frame.height:
            frames.append(frame)
    if not frames:
        return pl.DataFrame(schema=BARS_SCHEMA)
    return pl.concat(frames, how="vertical")


def _read_raw_partition(raw_root: str, tier: str, symbol: str, day: dt.date) -> pl.DataFrame | None:
    """One symbol-day raw partition (``bars``/``trades``/``quotes``), or None if absent/empty."""
    path = os.path.join(partition_dir(raw_root, tier, symbol, day), "data.parquet")
    if not os.path.exists(path):
        return None
    frame = pl.read_parquet(path)
    return frame if frame.height else None


def _tick_minute_columns(trades: pl.DataFrame, quotes: pl.DataFrame | None) -> pl.DataFrame:
    """Per-(symbol, minute) tick aggregates matching ``loaders._MINUTE_AGG_SQL`` / ``tick_capture``.

    ``signed_volume`` uses the tick rule with the sign carried across zero-ticks (state threaded WITHIN a
    symbol over the whole day, exactly as the live aggregator); the per-minute group_by then sums the
    signed sizes. ``n_trades`` is the per-minute trade count. The quote aggregates (mean spread in bps,
    size imbalance, mean book sizes) reproduce the columns the quote_spread group reads. This is the same
    aggregation ``parity_audit`` runs — kept identical so the order-flow backfill is parity-true.
    """
    trades = trades.sort(["symbol", "ts"])
    signed = trades.with_columns(
        pl.when(pl.col("price") > pl.col("price").shift(1).over("symbol"))
        .then(1)
        .when(pl.col("price") < pl.col("price").shift(1).over("symbol"))
        .then(-1)
        .otherwise(None)
        .alias("_raw_sign")
    ).with_columns(pl.col("_raw_sign").fill_null(strategy="forward").over("symbol").fill_null(1).alias("_sign"))
    trade_agg = signed.group_by(["symbol", pl.col("ts").dt.truncate("1m").alias("minute")]).agg(
        pl.len().cast(pl.Float64).alias("n_trades"),
        (pl.col("_sign") * pl.col("size")).sum().alias("signed_volume"),
    )
    if quotes is None or quotes.height == 0:
        return trade_agg
    mid = (pl.col("bid_price") + pl.col("ask_price")) / 2.0
    depth = pl.col("bid_size") + pl.col("ask_size")
    quote_agg = (
        quotes.with_columns(
            pl.when((mid > 0) & (pl.col("ask_price") >= pl.col("bid_price")))
            .then((pl.col("ask_price") - pl.col("bid_price")) / mid * 10000.0)
            .otherwise(None)
            .alias("_spread_bps"),
            pl.when(depth > 0)
            .then((pl.col("bid_size") - pl.col("ask_size")) / depth)
            .otherwise(None)
            .alias("_imbalance"),
        )
        .group_by(["symbol", pl.col("ts").dt.truncate("1m").alias("minute")])
        .agg(
            pl.col("_spread_bps").mean().fill_null(0.0).alias("mean_spread_bps"),
            pl.col("_imbalance").mean().fill_null(0.0).alias("quote_imbalance"),
            pl.col("bid_size").mean().alias("mean_bid_size"),
            pl.col("ask_size").mean().alias("mean_ask_size"),
        )
    )
    return trade_agg.join(quote_agg, on=["symbol", "minute"], how="full", coalesce=True)


def load_raw_tick_enriched_minute_agg(raw_root: str, day: str, symbols: list[str], bars: pl.DataFrame) -> pl.DataFrame:
    """Enrich the bar ``minute_agg`` with the real per-minute tick columns (n_trades, signed_volume,
    spread, imbalance, book sizes) aggregated from ``/store/raw/trades`` + ``/store/raw/quotes``, so the
    trade_flow / quote_spread / liquidity / signed_trade_ratio groups run on REAL tick inputs (left-join:
    null where a symbol had no ticks that minute — honest sparsity, not a fabricated value)."""
    target = dt.date.fromisoformat(day)
    trade_frames: list[pl.DataFrame] = []
    quote_frames: list[pl.DataFrame] = []
    for symbol in symbols:
        trades = _read_raw_partition(raw_root, "trades", symbol, target)
        if trades is not None:
            trade_frames.append(trades.select("symbol", "ts", "price", "size"))
        quotes = _read_raw_partition(raw_root, "quotes", symbol, target)
        if quotes is not None:
            quote_frames.append(quotes.select("symbol", "ts", "bid_price", "ask_price", "bid_size", "ask_size"))
    if not trade_frames:
        return bars
    trades_all = pl.concat(trade_frames, how="vertical")
    quotes_all = pl.concat(quote_frames, how="vertical") if quote_frames else None
    ticks = _tick_minute_columns(trades_all, quotes_all)
    return bars.join(ticks, on=["symbol", "minute"], how="left")


def load_raw_trades(raw_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """The raw per-trade ``trades`` frame (symbol, ts, price, size) the tick_runlength /
    microstructure_burst groups declare as input — read straight from ``/store/raw/trades``."""
    target = dt.date.fromisoformat(day)
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        trades = _read_raw_partition(raw_root, "trades", symbol, target)
        if trades is not None:
            frames.append(trades.select("symbol", "ts", "price", "size"))
    if not frames:
        return pl.DataFrame(schema=TRADES_SCHEMA)
    return pl.concat(frames, how="vertical").sort(["symbol", "ts"])
