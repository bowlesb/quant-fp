"""Core abstractions for the feature platform.

A ``FeatureGroup`` is the unit of extension: a cohesive batch of related features that share
inputs and a single vectorized compute over all ``(symbol, minute)`` cells. Live and backfill
call the identical ``compute()`` — parity by construction (FEATURE_PLATFORM.md §3.1). Individual
features are the named, addressable outputs of a group (``declare()``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import polars as pl

KEY_COLUMNS: tuple[str, ...] = ("symbol", "minute")
MIN_DESCRIPTION_CHARS = 40


class FeatureType(str, Enum):
    """Family taxonomy (FEATURE_PLATFORM.md §11). Used for catalog organization + breadth checks."""

    PRICE = "price"
    VOLUME = "volume"
    TRADE_FLOW = "trade_flow"
    QUOTE_SPREAD = "quote_spread"
    MICROSTRUCTURE = "microstructure"
    VOLATILITY = "volatility"
    MOMENTUM = "momentum"
    TECHNICAL = "technical"
    CALENDAR = "calendar"
    CROSS_SECTIONAL = "cross_sectional"
    MULTI_DAY = "multi_day"
    CANDLESTICK = "candlestick"
    PRICE_VOLUME = "price_volume"
    TREND_QUALITY = "trend_quality"
    REFERENCE = "reference"


@dataclass(frozen=True)
class InputSpec:
    """A declared data dependency: a named input frame and the columns required from it."""

    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class FeatureSpec:
    """The contract for one feature output. The engine enforces dtype + range + nan policy, so a
    group can never silently emit an undeclared column, a wrong dtype, or an out-of-range value."""

    name: str
    description: str
    dtype: str  # polars COMPUTE dtype, e.g. "Float64" (the math is always done in this width)
    valid_range: tuple[float | None, float | None] | None = None
    nan_policy: str = "none"  # "none" | "warmup" | "sparse"
    tolerance: float = 1e-6  # cell-level RELATIVE tolerance used by parity: |a-b| <= 1e-12 + tol*|b|
    layer: str = "A"  # data layer (PARITY_PLAYBOOK §2): "A" minute bars | "B" minute tick-agg | "C" sub-minute ticks
    parity_method: str = "tolerance"  # "tolerance" (cell rel-tol) | "distributional" (quantile match)
    storage: str | None = None  # on-disk dtype, e.g. "Float32"|"UInt8"|"Int16"; None = derive (storage_dtype)


# Integer-semantics features whose range/name don't match the flag rule — explicit smallest-int storage.
_INT_STORAGE_OVERRIDES: dict[str, pl.DataType] = {
    "day_of_week": pl.UInt8,  # 1-7
    "week_of_month": pl.UInt8,  # 1-5
    "minute_of_day_et": pl.UInt16,  # 0-1440
    "minutes_since_open": pl.Int16,  # signed, -570..870
}
# Flag-style features: a true 0/1 indicator carries one of these name prefixes AND a [~0, ~1] range.
_FLAG_NAME_PREFIXES: tuple[str, ...] = ("is_", "sector_is_", "pattern_", "above_", "outperforming_")


def storage_dtype(spec: FeatureSpec) -> pl.DataType:
    """The on-disk dtype for a feature — half (or less) the Float64 compute width.

    A feature can DECLARE its storage dtype explicitly (``FeatureSpec(storage="UInt8")``) — the
    standardized, self-documenting way. When it doesn't, we DERIVE a sensible default from the contract,
    so the 519 existing declarations need no change and a new group only declares ``storage`` when the
    default would be wrong. Every feature is computed in Float64 but NONE needs double precision on disk:
    parity compares STORED-vs-STORED (both sides round identically, diff ~0) and ML trains on Float32. So:
      • true 0/1 flags (flag-prefixed name + [~0,~1] range) -> UInt8 (polars-NULLABLE: holds the warmup/
        sparse nulls these features legitimately emit, which a numpy uint8 could not);
      • the four integer calendar features -> smallest signed/unsigned int that covers their range;
      • everything else (the real-valued bulk) -> Float32.
    """
    if spec.storage is not None:
        return getattr(pl, spec.storage)  # explicit declaration wins
    if spec.name in _INT_STORAGE_OVERRIDES:
        return _INT_STORAGE_OVERRIDES[spec.name]
    low, high = spec.valid_range or (None, None)
    is_binary_range = low is not None and high is not None and -0.01 <= low <= 0.0 and 1.0 <= high <= 1.01
    if is_binary_range and spec.name.startswith(_FLAG_NAME_PREFIXES):
        return pl.UInt8
    return pl.Float32


@dataclass
class BatchContext:
    """Inputs + scope for one compute call. ``frames`` maps an input name to a Polars frame keyed
    by ``(symbol, minute)``. Live: minute = the current minute; backfill: minutes = a historical
    range. The same context shape feeds both paths, which is what makes compute parity-true."""

    frames: dict[str, pl.DataFrame]

    def frame(self, name: str) -> pl.DataFrame:
        if name not in self.frames:
            raise KeyError(f"input frame '{name}' was not provided to BatchContext")
        return self.frames[name]


class FeatureGroup(ABC):
    """Base class for a feature group. Subclasses set the class attributes and implement
    ``declare()`` (the output contract) and ``compute()`` (the vectorized batch computation)."""

    name: str
    version: str
    owner: str
    type: FeatureType
    inputs: tuple[InputSpec, ...] = ()

    @abstractmethod
    def declare(self) -> list[FeatureSpec]:
        """The named feature outputs and their contracts."""

    @abstractmethod
    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """Vectorized over all requested (symbol, minute) cells. Returns a frame keyed by
        (symbol, minute) with exactly one column per declared feature. This is the BACKFILL form
        (whole-history rolling) and the source of truth."""

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE form for the live path: emit only the most recent minute's row per symbol.

        Default = ``compute()`` filtered to the last minute (always correct, but does the full rolling
        work). Groups OVERRIDE this with a fast aggregate-at-T form (a windowed group_by → one row per
        symbol, ~window× less work). Any override is guarded by the generic parity test
        (tests/test_fp_latest.py): ``compute_latest`` MUST equal ``compute().filter(last minute)`` — so a
        fast live form can never silently diverge from the backfill rolling form."""
        out = self.compute(ctx)
        if out.height == 0:
            return out
        return out.filter(pl.col("minute") == out["minute"].max())

    def compute_latest_on_window(self, ctx: BatchContext, lookback_minutes: int) -> pl.DataFrame:
        """Parity-true fast ``compute_latest`` for a BOUNDED-window group: run the IDENTICAL ``compute()`` on
        the input sliced to the trailing ``lookback_minutes`` it actually reads, then emit T's row per symbol.

        This is NOT a second formulation — it is the same rolling ``compute()`` on the minimal input window, so
        live == backfill by construction (the dropped older bars cannot influence a window that ends at T and
        spans <= ``lookback_minutes``). ``lookback_minutes`` must be the group's deepest declared window plus
        warmup slack (e.g. the 1-bar lag a return needs at the window's start); slice conservatively — the
        generic parity test (tests/test_fp_latest.py) fails loudly if it is too tight, which is the guard.

        Each input frame is filtered to its own ``minute >= (frame latest minute) - lookback_minutes``. The live
        buffer ends every input at the same current minute T, so per-frame slicing keeps every bar inside the
        trailing window the group reads (a sparse symbol still keeps its real bars — the window is by wall-clock
        minute, not row count). A frame with no ``minute`` column (e.g. a static reference frame) is passed
        through whole."""
        sliced = {
            name: self._slice_to_window(frame, lookback_minutes)
            for name, frame in ctx.frames.items()
        }
        out = self.compute(BatchContext(frames=sliced))
        if out.height == 0:
            return out
        return out.filter(pl.col("minute") == out["minute"].max())

    @staticmethod
    def _slice_to_window(frame: pl.DataFrame, lookback_minutes: int) -> pl.DataFrame:
        """Keep only the trailing ``lookback_minutes`` of ``frame`` (by its own latest minute). Frames with no
        ``minute`` column or no rows pass through unchanged."""
        if "minute" not in frame.columns or frame.height == 0:
            return frame
        cutoff = pl.col("minute").max() - pl.duration(minutes=lookback_minutes)
        return frame.filter(pl.col("minute") >= cutoff)

    @property
    def feature_names(self) -> list[str]:
        return [spec.name for spec in self.declare()]

    def reduce_buffer_minutes(self) -> int | None:
        """The deepest trailing-window (in minutes) this group needs to compute its latest minute
        correctly off a trimmed buffer, or ``None`` when unknown (caller must then keep the full
        buffer). DECLARED, not hardcoded by callers — the reader's reduce path uses the max of these
        over the reduce groups to bound its minimal close+volume buffer. Default ``None`` (full buffer)
        is safe for any group that hasn't declared its depth; groups with a known longest window
        override it (``ReductionGroup`` derives it from its declared windows)."""
        return None


def lagged(frame: pl.DataFrame, value: str, minutes: int, alias: str) -> pl.DataFrame:
    """Attach ``alias`` = ``value`` as of (minute − ``minutes``), via a TIME-BASED self-join per
    symbol. Time-based (not positional) so it is correct on a gappy grid and point-in-time: a cell
    is null when the bar exactly ``minutes`` ago is absent, rather than silently using a closer bar.
    Requires ``minute`` to be a Datetime column.
    """
    lag = frame.select(
        pl.col("symbol"),
        (pl.col("minute") + pl.duration(minutes=minutes)).alias("minute"),
        pl.col(value).alias(alias),
    )
    return frame.join(lag, on=["symbol", "minute"], how="left")
