"""Swing / ZigZag structure features from per-minute close (family: TREND_QUALITY, Layer A).

The up-down-up-down ("Fibonacci-style") swing structure of the close series, and a flag for when it resolves
into a clean directional move. A ZigZag filter ignores moves smaller than ``theta`` and marks PIVOTS (confirmed
local extrema) where price reverses by >= theta from the running leg extreme; between pivots price runs one
direction (a LEG). The fold is O(1) per bar — a per-symbol state machine, NO buffer re-scan.

THE LOOK-AHEAD PROPERTY (the whole point): a standard ZigZag REPAINTS — it confirms a pivot using FUTURE bars,
so it cannot be used point-in-time. This version is POINT-IN-TIME: at minute T it uses ONLY bars <= T, so a
pivot is confirmed only once the theta-reversal has ACTUALLY occurred by T; the current leg is PROVISIONAL (its
extreme can still extend). That makes live == backfill by construction (fold == reseed), and the value at T over
a buffer ending at T is identical whether or not bars after T exist (tests/test_fp_swing.py asserts both).

The sequential fold lives in the Rust ``quant_tick.swing_fold`` kernel (each bar's contribution depends on the
running leg state left by the prior bar — not vectorizable in Polars), called identically from the live tape and
the backfill through this ONE group, so parity holds by construction; a pure-Python reference pins the Rust
output cell-for-cell (tests/test_fp_swing.py).
"""
from __future__ import annotations

import polars as pl
import quant_tick

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

# Deterministic for parity: a fixed reversal threshold as a fractional return (0.5%). A volatility-multiple
# theta would need a parity-true point-in-time vol — out of scope for v1; the fixed return keeps fold == reseed.
THETA: float = 0.005
RING_K: int = 8  # confirmed pivots kept per symbol for the persistence / alternation / resolved reads
DAY_SECS: int = 86_400
# fib_retracement degenerate guard: when the PRIOR completed leg's range is a near-zero fraction of price
# (a confirmed micro-leg), the (c - end)/(start - end) ratio explodes (seen LIVE up to 450 on thin names like
# BBN/PVL). The basis is then meaningless — same situation as a flat-window bb_position. Beyond the declared
# valid_range we treat fib as UNDEFINED (null), not a finite reading. Applied identically on the one fold path
# (swing_fold_frame), so live == backfill cell-for-cell; mirrored in the pure-Python parity reference.
FIB_MAX_ABS: float = 10.0

_FEATURE_COLS: tuple[str, ...] = (
    "swing_dir",
    "swing_steepness",
    "swing_len_pct",
    "minutes_since_pivot",
    "n_pivots_today",
    "n_alternations",
    "swing_persistence",
    "fib_retracement",
    "trend_resolved",
)

_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    **{name: pl.Float64 for name in _FEATURE_COLS},
}


def swing_fold_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Run the point-in-time swing/zigzag fold over EVERY (symbol, minute) in ``frame`` via the Rust kernel.

    Codes symbols to ints + sorts (symbol, minute) so the kernel folds each symbol's close series in order,
    emitting one row per input bar with that bar's POINT-IN-TIME swing state (only bars <= that bar were read).
    Returns a (symbol, minute, <swing features>) frame. The whole-history fold IS the parity reference: the
    live path takes the latest minute of the same fold, so live == backfill cell-for-cell."""
    base = frame.select(["symbol", "minute", "close"])
    if base.height == 0:
        return pl.DataFrame(schema=_SCHEMA)
    uniq = sorted(base["symbol"].unique().to_list())
    codes = pl.DataFrame(
        {"symbol": uniq, "_code": list(range(len(uniq)))},
        schema={"symbol": pl.String, "_code": pl.Int64},
    )
    coded = (
        base.join(codes, on="symbol", how="left")
        .with_columns(pl.col("minute").dt.epoch("s").alias("_mi"))
        .sort(["_code", "_mi"])
    )
    out = quant_tick.swing_fold(
        coded["_code"].to_numpy(),
        coded["_mi"].to_numpy(),
        coded.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1),
        THETA,
        DAY_SECS,
        RING_K,
    )
    result = coded.select(["symbol", "minute"])
    result = result.with_columns(
        [pl.Series(name, out[i], dtype=pl.Float64) for i, name in enumerate(_FEATURE_COLS)]
    )
    # The kernel's NaN sentinels (minutes_since_pivot/fib_retracement before the first pivot/leg) restore to
    # Polars null so the warmup nan_policy holds and parity treats them as MISSING, not a finite 0.
    result = result.with_columns(
        [pl.col(name).fill_nan(None) for name in ("minutes_since_pivot", "fib_retracement")]
    )
    # Degenerate-basis guard: a confirmed micro-leg gives fib a near-zero denominator and an explosive read.
    # Beyond the declared valid_range that value is undefined, not finite — null it (parity-safe: pure function
    # of this row's own fib, so live and backfill null the identical cells). Pre-existing nulls stay null.
    return result.with_columns(
        pl.when(pl.col("fib_retracement").abs() > FIB_MAX_ABS)
        .then(None)
        .otherwise(pl.col("fib_retracement"))
        .alias("fib_retracement")
    ).select(["symbol", "minute", *_FEATURE_COLS])


@register
class SwingGroup(FeatureGroup):
    name = "swing"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        warmup = [
            FeatureSpec(
                name="minutes_since_pivot",
                description="Minutes since the last CONFIRMED swing pivot (the current provisional leg's age); null before the first pivot.",
                dtype="Float64", valid_range=(0.0, 1e6), nan_policy="warmup", layer="A",
            ),
            FeatureSpec(
                name="fib_retracement",
                description="Where the close sits within the PRIOR completed leg's range (the 0/0.382/0.5/0.618/1 read), measured from the prior leg's end back toward its start; null until a leg completes, and null when the prior leg's range is a degenerate micro-leg (read beyond the valid_range).",
                dtype="Float64", valid_range=(-FIB_MAX_ABS, FIB_MAX_ABS), nan_policy="warmup", layer="A",
            ),
        ]
        plain = [
            FeatureSpec(
                name="swing_dir",
                description="Current swing leg direction: +1 in a (provisional) up-leg, -1 in a down-leg, 0 before any direction is established.",
                dtype="Float64", valid_range=(-1.0, 1.0), nan_policy="none", layer="A",
            ),
            FeatureSpec(
                name="swing_steepness",
                description="Slope of the current swing leg as a per-minute fractional return ((close-leg_start)/leg_start divided by minutes since leg start); 0 at a leg start.",
                dtype="Float64", valid_range=(-1.0, 1.0), nan_policy="none", layer="A",
            ),
            FeatureSpec(
                name="swing_len_pct",
                description="Current swing leg size as a signed fractional return from the leg's start price to the current close.",
                dtype="Float64", valid_range=(-10.0, 10.0), nan_policy="none", layer="A",
            ),
            FeatureSpec(
                name="n_pivots_today",
                description="Count of confirmed swing pivots so far on the current session day (resets at the day boundary).",
                dtype="Float64", valid_range=(0.0, 1e5), nan_policy="none", layer="A",
            ),
            FeatureSpec(
                name="n_alternations",
                description="Count of swing direction flips over the kept pivot ring (each confirmed pivot is one alternation; tight alternation = chop).",
                dtype="Float64", valid_range=(0.0, 1e7), nan_policy="none", layer="A",
            ),
            FeatureSpec(
                name="swing_persistence",
                description="Net signed leg progression over the last K legs: sum of signed leg returns plus the current provisional leg (same-signed legs accumulate, chop cancels toward 0).",
                dtype="Float64", valid_range=(-100.0, 100.0), nan_policy="none", layer="A",
            ),
            FeatureSpec(
                name="trend_resolved",
                description="1.0 when, after tight alternation, the current swing leg exceeds the recent legs in BOTH length AND steepness AND its direction persists; else 0.0 (a clean directional resolution).",
                dtype="Float64", valid_range=(0.0, 1.0), nan_policy="none", layer="A", storage="UInt8",
            ),
        ]
        return plain + warmup

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """BACKFILL (source of truth): the point-in-time swing fold over the whole buffer, one row per minute."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        return swing_fold_frame(frame)
