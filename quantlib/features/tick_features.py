"""Per-minute tick-tape PRIMITIVES — the enrich-step computation the clean tick groups read as derived bar
columns (the input-contract half of the #63 port).

The 8 clean tick groups (``clean_groups_tick``) read 21 derived bar columns: the 3 WINDOWED groups read a
per-minute primitive they then reduce over a trailing window (``_hhi`` / ``_gap_fano`` / the 6 size bins
``_sz_c0.._sz_c5``); the 5 ATOMIC groups read their FINAL per-minute feature value straight off the bar. This
module computes all 21 from a minute's ``trades`` tape, FAITHFULLY — the windowed primitives use the same
within-minute expression the legacy group does, and the atomic features are produced by running the legacy
group's own ``compute()`` on the minute's trades (byte-identical to legacy by construction).

``compute_tick_primitives(trades)`` returns a per-(symbol, minute) frame the enrich step joins onto the bar
rows. A symbol-minute with no trades simply has no row (the honest "no trades"); the enrich join leaves those
columns null on the bar → the clean group propagates NaN, matching the legacy nan_policy.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.groups.inter_arrival import InterArrivalGroup
from quantlib.features.groups.large_print_burst import LargePrintBurstGroup
from quantlib.features.groups.microstructure_burst import MicrostructureBurstGroup
from quantlib.features.groups.tick_runlength import TickRunLengthGroup
from quantlib.features.groups.trade_size_dist import TradeSizeDistGroup

# The ATOMIC groups whose per-minute compute() output IS the derived columns (the feature names ARE the columns).
_ATOMIC_GROUPS = (
    InterArrivalGroup(),
    LargePrintBurstGroup(),
    MicrostructureBurstGroup(),
    TickRunLengthGroup(),
    TradeSizeDistGroup(),
)
_N_SIZE_BINS = 6

# The 21 per-minute primitive columns this module emits — the WINDOWED scalars (_hhi / _gap_fano / 6 size bins)
# the windowed tick groups reduce, + the ATOMIC groups' final per-minute features. These are the derived bar
# columns the clean tick groups read; the enrich step (tick_capture) carries them on each bar.
TICK_PRIMITIVE_COLUMNS: tuple[str, ...] = (
    ("_hhi", "_gap_fano")
    + tuple(f"_sz_c{bin_index}" for bin_index in range(_N_SIZE_BINS))
    + tuple(spec.name for group in _ATOMIC_GROUPS for spec in group.declare())
)


def _windowed_primitives(trades: pl.DataFrame) -> pl.DataFrame:
    """The per-(symbol, minute) WINDOWED primitives — the within-minute scalars the windowed clean groups reduce:
    ``_hhi`` (notional Herfindahl), ``_gap_fano`` (inter-trade-gap Fano), ``_sz_c0.._sz_c5`` (size-bin counts).
    Same within-minute expressions the legacy ``print_hhi`` / ``subminute_gap_fano`` / ``size_entropy`` groups use.
    """
    per_trade = trades.with_columns(
        pl.col("ts").dt.truncate("1m").alias("minute"),
        (pl.col("price") * pl.col("size")).alias("_notional"),
        pl.col("size").log10().floor().clip(0, _N_SIZE_BINS - 1).cast(pl.Int64).alias("_bin"),
    )
    # _hhi: Σnotional² / (Σnotional)², null on a zero-notional minute (Guard 2: square of a non-negative sum).
    hhi = (
        per_trade.group_by(["symbol", "minute"])
        .agg(
            pl.col("_notional").pow(2).sum().alias("_sumsq"),
            pl.col("_notional").sum().alias("_sum"),
        )
        .with_columns(
            pl.when(pl.col("_sum") > 0.0)
            .then(pl.col("_sumsq") / pl.col("_sum").pow(2))
            .otherwise(None)
            .alias("_hhi")
        )
        .select(["symbol", "minute", "_hhi"])
    )
    # 6 size-bin counts (linear — the clean group windowed-SUMs these, then assembles entropy).
    bins = per_trade.group_by(["symbol", "minute"]).agg(
        *[
            (pl.col("_bin") == bin_index).sum().cast(pl.Float64).alias(f"_sz_c{bin_index}")
            for bin_index in range(_N_SIZE_BINS)
        ]
    )
    # _gap_fano: var/mean of within-minute microsecond gaps (ordered by ts, first trade's gap null), ddof=1.
    gaps = per_trade.sort(["symbol", "minute", "ts"]).with_columns(
        pl.col("ts").diff().over(["symbol", "minute"]).dt.total_microseconds().alias("_gap_us")
    )
    fano = (
        gaps.group_by(["symbol", "minute"])
        .agg(
            pl.col("_gap_us").drop_nulls().var().alias("_gap_var"),
            pl.col("_gap_us").drop_nulls().mean().alias("_gap_mean"),
        )
        .with_columns(
            pl.when(pl.col("_gap_mean") > 0.0)
            .then(pl.col("_gap_var") / pl.col("_gap_mean"))
            .otherwise(None)
            .alias("_gap_fano")
        )
        .select(["symbol", "minute", "_gap_fano"])
    )
    return hhi.join(bins, on=["symbol", "minute"], how="full", coalesce=True).join(
        fano, on=["symbol", "minute"], how="full", coalesce=True
    )


def compute_tick_primitives(trades: pl.DataFrame) -> pl.DataFrame:
    """Per-(symbol, minute) frame of the 21 tick primitives — the windowed scalars (``_hhi`` / ``_gap_fano`` /
    ``_sz_c0.._sz_c5``) + the 5 atomic groups' final per-minute features. The atomic columns are produced by the
    legacy groups' own ``compute()`` on the minute's tape (byte-identical to legacy). An empty tape → an empty,
    correctly-typed frame."""
    if trades.height == 0:
        return pl.DataFrame(schema={"symbol": pl.String, "minute": pl.Datetime("us", "UTC")})
    ctx = BatchContext(frames={"trades": trades})
    result = _windowed_primitives(trades)
    for group in _ATOMIC_GROUPS:
        atomic = group.compute(
            ctx
        )  # (symbol, minute, <the group's per-minute features>) — IS the legacy math
        result = result.join(atomic, on=["symbol", "minute"], how="full", coalesce=True)
    return result.sort(["symbol", "minute"])
