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
    dtype: str  # polars dtype name, e.g. "Float64"
    valid_range: tuple[float | None, float | None] | None = None
    nan_policy: str = "none"  # "none" | "warmup" | "sparse"
    tolerance: float = 1e-6  # cell-level RELATIVE tolerance used by parity: |a-b| <= 1e-12 + tol*|b|
    layer: str = "A"  # data layer (PARITY_PLAYBOOK §2): "A" minute bars | "B" minute tick-agg | "C" sub-minute ticks
    parity_method: str = "tolerance"  # "tolerance" (cell rel-tol) | "distributional" (quantile match)


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
        (symbol, minute) with exactly one column per declared feature."""

    @property
    def feature_names(self) -> list[str]:
        return [spec.name for spec in self.declare()]


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
