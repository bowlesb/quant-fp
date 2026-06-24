"""Core abstractions for the feature platform.

A ``FeatureGroup`` is the unit of extension: a cohesive batch of related features that share
inputs and a single vectorized compute over all ``(symbol, minute)`` cells. Live and backfill
call the identical ``compute()`` — parity by construction (FEATURE_PLATFORM.md §3.1). Individual
features are the named, addressable outputs of a group (``declare()``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import polars as pl

KEY_COLUMNS: tuple[str, ...] = ("symbol", "minute")
MIN_DESCRIPTION_CHARS = 40


class RawLayer(str, Enum):
    """A raw-tape layer in ``/store/raw`` — the source-data substrate a feature group reads from.

    These are exactly the three raw tiers the acquire stage writes (``quantlib.data.raw_store``):
    ``bars`` (minute OHLCV), ``trades`` (per-trade tape), ``quotes`` (NBBO tape). A feature group
    DECLARES which of these its inputs ultimately derive from (``FeatureGroup.required_raw_layers``),
    so a backfill can ENSURE those raw inputs are in the store before computing the feature
    (docs/SOURCE_DATA_DEPENDENCY.md). The string values match the on-disk tier names verbatim, so a
    layer drops straight into ``partition_dir(store, layer.value, ...)`` and the manifest ``tier``.
    """

    BARS = "bars"
    TRADES = "trades"
    QUOTES = "quotes"


class Source(str, Enum):
    """A declarable INPUT SOURCE a feature group's data ultimately derives from — the superset of the raw
    market layers PLUS the alt-data sources (news, EDGAR filings).

    The three market values (``bars``/``trades``/``quotes``) are the ``RawLayer`` tiers in ``/store/raw``;
    ``news`` is the ``/store/news`` article tape (date-partitioned parquet + manifest); ``edgar`` is the
    Postgres ``filings`` event store (db/init/08_filings.sql). A group DECLARES which of these its inputs
    derive from (``FeatureGroup.required_sources``), so a backfill can ENSURE every source is current before
    computing — not just the market layers (docs/SOURCE_DATA_DEPENDENCY.md). The market values match the
    ``RawLayer`` values verbatim (``Source.BARS.value == RawLayer.BARS.value``) so the two enums interoperate
    by value: ``source_inputs`` routes a market ``Source`` through the existing ``ensure_inputs`` path and a
    news/edgar ``Source`` through its own per-source adapter + fetcher.
    """

    BARS = "bars"
    TRADES = "trades"
    QUOTES = "quotes"
    NEWS = "news"
    EDGAR = "edgar"

    @property
    def is_market_layer(self) -> bool:
        """True for ``bars``/``trades``/``quotes`` — the sources backed by a ``RawLayer`` in ``/store/raw``
        (ensured via the existing market hole-detection); False for ``news``/``edgar`` (their own adapters).
        """
        return self in _MARKET_SOURCES

    def as_raw_layer(self) -> RawLayer:
        """The ``RawLayer`` for a market source (``bars``/``trades``/``quotes``). Raises for news/edgar —
        they are NOT ``/store/raw`` tiers, so calling this on them is a programming error to surface."""
        if not self.is_market_layer:
            raise ValueError(f"{self.value} is not a /store/raw market layer (no RawLayer)")
        return RawLayer(self.value)


_MARKET_SOURCES: frozenset[Source] = frozenset({Source.BARS, Source.TRADES, Source.QUOTES})


def raw_layers_as_sources(layers: frozenset[RawLayer]) -> frozenset[Source]:
    """Lift a set of raw market ``RawLayer`` values into the ``Source`` enum (by matching value).

    Lets ``required_sources`` default-derive the market sources from a group's existing
    ``required_raw_layers`` declaration, so a bar/trade/quote group needs no edit to also be source-aware."""
    return frozenset(Source(layer.value) for layer in layers)


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
    layer: str = (
        "A"  # data layer (PARITY_PLAYBOOK §2): "A" minute bars | "B" minute tick-agg | "C" sub-minute ticks
    )
    parity_method: str = "tolerance"  # "tolerance" (cell rel-tol) | "distributional" (quantile match)
    storage: str | None = (
        None  # on-disk dtype, e.g. "Float32"|"UInt8"|"Int16"; None = derive (storage_dtype)
    )


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


# Default raw-layer requirement DERIVED from a group's family type, so the existing groups need no edit.
# A bar-derived group needs only the ``bars`` tape. A trade-flow group's per-minute tick columns
# (n_trades, signed_volume) are aggregated from the ``trades`` tape on top of the bar grid, so it needs
# ``bars`` + ``trades``. A quote/spread/microstructure group additionally reads the ``quotes`` tape (the
# tick-enriched minute_agg joins all three). The set is INCLUSIVE — a group always needs ``bars`` for its
# minute grid; richer layers add to that set. Mirrors the layer routing in ``settle_lag_for_group`` but
# returns the FULL set of layers (not just the deepest), because ``ensure_inputs`` must fetch ALL of them.
_TYPE_RAW_LAYERS: dict[FeatureType, frozenset[RawLayer]] = {
    FeatureType.TRADE_FLOW: frozenset({RawLayer.BARS, RawLayer.TRADES}),
    FeatureType.QUOTE_SPREAD: frozenset({RawLayer.BARS, RawLayer.TRADES, RawLayer.QUOTES}),
    FeatureType.MICROSTRUCTURE: frozenset({RawLayer.BARS, RawLayer.TRADES, RawLayer.QUOTES}),
}
_DEFAULT_RAW_LAYERS: frozenset[RawLayer] = frozenset({RawLayer.BARS})

# Default ALT-DATA source requirement DERIVED from a group's family type — the news/edgar analogue of
# ``_TYPE_RAW_LAYERS``. Empty by default: alt-data consumers are sparse and family-ambiguous (the REFERENCE
# family holds BOTH the news-sentiment group [news] AND the edgar-filing-frequency group [edgar]), so a
# group's alt-data source is DECLARED on the group via a ``required_sources`` override rather than guessed
# from its type. The mapping is kept (not deleted) so a future family that uniformly consumes one alt source
# can default-derive it here without re-plumbing — mirroring how a new market family would extend
# ``_TYPE_RAW_LAYERS``.
_TYPE_ALT_SOURCES: dict[FeatureType, frozenset[Source]] = {}


class FeatureGroup(ABC):
    """Base class for a feature group. Subclasses set the class attributes and implement
    ``declare()`` (the output contract) and ``compute()`` (the vectorized batch computation)."""

    name: str
    version: str
    owner: str
    type: FeatureType
    inputs: tuple[InputSpec, ...] = ()

    @property
    def session_cache(self) -> SessionCache:
        """The group's per-instance, engine-owned per-session memo for its Class-A (intraday-invariant)
        derived frame (see ``SessionCache``). Lazily created per instance — a Class-A group routes its
        once-per-session computation through ``self.session_cache.get(witness, compute)`` instead of
        hand-rolling a ``_daily_cache`` field + token check."""
        cache = self.__dict__.get("_session_cache")
        if cache is None:
            cache = SessionCache()
            self.__dict__["_session_cache"] = cache
        return cache

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
        through whole.

        NOTE on positional ``shift(k)`` at the window edge: this minute-cutoff slice does NOT retain the bar
        before the window, so a group whose deepest-window feature reads a per-bar ``close.shift(1).over(...)``
        return at the EARLIEST in-window bar must size ``lookback_minutes`` to also cover that bar's predecessor
        — or, for a sparse symbol whose predecessor sits an arbitrary gap back, handle the window edge itself
        (see momentum_run's ``compute_latest``, which derives the per-bar returns over the WHOLE buffer before
        the window slice). The generic parity test guards the dense case; the real-data audit guards the sparse
        case."""
        sliced = {name: self._slice_to_window(frame, lookback_minutes) for name, frame in ctx.frames.items()}
        out = self.compute(BatchContext(frames=sliced))
        if out.height == 0:
            return out
        return out.filter(pl.col("minute") == out["minute"].max())

    @staticmethod
    def _slice_to_window(frame: pl.DataFrame, lookback_minutes: int) -> pl.DataFrame:
        """Keep only the trailing ``lookback_minutes`` of ``frame`` (by its own latest minute). Empty frames
        pass through unchanged. A bar-grid frame keyed by ``minute`` slices on that column; a raw-tape frame
        keyed by ``ts`` (the trades feed — no ``minute`` column) slices on the trade timestamp, cutting at
        the START of the minute ``lookback_minutes`` before the last trade's minute so the WHOLE last minute
        (and every trailing window minute) is kept intact. A frame with neither column passes through whole.
        """
        if frame.height == 0:
            return frame
        if "minute" in frame.columns:
            cutoff = pl.col("minute").max() - pl.duration(minutes=lookback_minutes)
            return frame.filter(pl.col("minute") >= cutoff)
        if "ts" in frame.columns:
            # The tape's own-minute / windowed groups bucket by ``ts.truncate("1m")``; slice from the start
            # of the minute ``lookback_minutes`` before the last trade so no in-window minute is half-dropped.
            last_minute = frame.select(pl.col("ts").max().dt.truncate("1m")).item()
            cutoff = last_minute - pl.duration(minutes=lookback_minutes)
            return frame.filter(pl.col("ts") >= cutoff)
        return frame

    def compute_latest_point_in_time(self, ctx: BatchContext) -> pl.DataFrame:
        """Parity-true fast ``compute_latest`` for a POINT-IN-TIME group — one whose ``compute`` is a per-row
        function of the minute (calendar/session fields, a static reference join, round-number distances): run
        the IDENTICAL ``compute`` on ``minute_agg`` pre-filtered to the LATEST minute, instead of computing
        every buffered minute's row and discarding all but the last (the default ``compute_latest``).

        Correct iff the group reads NO cross-minute window (each output row depends only on its own minute's
        inputs) — then restricting the input to the latest minute cannot change that minute's output. The
        generic parity test (tests/test_fp_latest.py) guards it: ``compute_latest`` must equal
        ``compute().filter(last minute)``, so a group that secretly reads history fails loudly. Only
        ``minute_agg`` is narrowed; static reference frames are passed through whole (they carry no minute).
        """
        minute_agg = ctx.frame("minute_agg")
        latest = minute_agg["minute"].max()
        scoped = BatchContext(
            frames={**ctx.frames, "minute_agg": minute_agg.filter(pl.col("minute") == latest)}
        )
        return self.compute(scoped)

    @property
    def feature_names(self) -> list[str]:
        return [spec.name for spec in self.declare()]

    def required_raw_layers(self) -> frozenset[RawLayer]:
        """The raw ``/store/raw`` layers (bars/trades/quotes) this group's inputs ultimately derive from.

        This is the SOURCE-DATA DEPENDENCY a backfill DECLARES (docs/SOURCE_DATA_DEPENDENCY.md): before
        computing this group over a [date_range]×symbols horizon, ``ensure_inputs`` guarantees these raw
        layers are present in the store (patching any holes ONCE) so the feature backfill reads source
        EXCLUSIVELY from the store and never re-downloads from Alpaca.

        DEFAULT derives from ``self.type`` (``_TYPE_RAW_LAYERS``), so the existing groups need no change:
        a bar-derived group needs ``{bars}``; a trade_flow group ``{bars, trades}``; a quote_spread /
        microstructure group ``{bars, trades, quotes}``. A group whose true requirement differs from its
        family default OVERRIDES this (the same self-declaration pattern as ``reduce_buffer_minutes`` /
        ``up_to_date``) — the declaration lives with the group, not in a backfill-side lookup table."""
        return _TYPE_RAW_LAYERS.get(self.type, _DEFAULT_RAW_LAYERS)

    def required_sources(self) -> frozenset[Source]:
        """The full set of INPUT SOURCES this group's data derives from — market layers (bars/trades/quotes)
        AND alt-data sources (news, EDGAR filings). The superset of ``required_raw_layers`` extended to the
        non-``/store/raw`` sources (docs/SOURCE_DATA_DEPENDENCY.md).

        DEFAULT lifts the group's declared ``required_raw_layers`` into the ``Source`` enum (so every existing
        market group is source-aware with no edit), then ADDS any alt-data source its ``type`` maps to
        (``_TYPE_ALT_SOURCES``). A group whose true requirement differs OVERRIDES this — the news_sentiment
        group declares ``{news}`` and edgar_filing_frequency declares ``{edgar}`` (each consumes ONLY its
        alt-data tape on a bar minute-grid that is itself reconstructable, so they override to the alt source
        alone rather than carry the unused ``bars`` market layer). The declaration lives WITH the group."""
        market = raw_layers_as_sources(self.required_raw_layers())
        return market | _TYPE_ALT_SOURCES.get(self.type, frozenset())

    def source_lookback_days(self, source: Source) -> int:
        """How far back (in calendar days) this group reads ``source`` to compute one session day correctly —
        the amount a backfill must EXPAND its window so the source is present for this group's trailing
        windows (docs/SOURCE_DATA_DEPENDENCY.md). 0 (the default) for a group that does not consume ``source``,
        or for a market source whose presence is per-symbol-day (the market path expands nothing). An alt-data
        group OVERRIDES this for the source it consumes (news_sentiment reads news back its deepest window;
        edgar_filing_frequency reads filings back the 365-day burst baseline) so the horizon lives WITH the
        group, not in a backfill-side constant."""
        return 0

    def reduce_buffer_minutes(self) -> int | None:
        """The deepest trailing-window (in minutes) this group needs to compute its latest minute
        correctly off a trimmed buffer, or ``None`` when unknown (caller must then keep the full
        buffer). DECLARED, not hardcoded by callers — the reader's reduce path uses the max of these
        over the reduce groups to bound its minimal close+volume buffer. Default ``None`` (full buffer)
        is safe for any group that hasn't declared its depth; groups with a known longest window
        override it (``ReductionGroup`` derives it from its declared windows)."""
        return None

    def up_to_date(self, buffer: pl.DataFrame | None) -> bool:
        """The RunningState contract at the GROUP level — the SINGLE self-healing rule for both held-state
        features AND the hot-swap applier (quantlib/features/running_state.py).

        Returns True iff this group can compute ``buffer``'s latest minute and emit a value EQUAL to the backfill
        recompute WITHOUT first rebuilding its state. DEFAULT True: a stateless group (every batch reduction with
        FP_INCREMENTAL off, every declarative / candlestick / calendar / cross-sectional group, every Class-A
        cache group that recomputes on a miss) re-derives from the shared ring each minute, so it is ALWAYS up to
        date — nothing to reseed. A group that carries cross-minute state (swing's leg-state, an armed incremental
        engine, a StatefulGroup accumulator) OVERRIDES this to delegate to its running-state object, so it reports
        False when cold / after a hot-swap / across a session boundary / on a gap, which makes the caller rebuild.
        ``buffer`` may be None (no history available) — a stateless group is still up to date; a stateful override
        reports False (it cannot verify freshness without history), so the caller escalates if it cannot reseed.

        THE APPLIER USES THIS to stay KIND-AGNOSTIC: it swaps the code, then ``if not group.up_to_date(buffer):
        group.rebuild_from_history(buffer)`` — no DIRECT/RESEED/ESCALATE classification. DIRECT = the default True;
        RESEED = a stateful override returns False → self-rebuild; irreducible = ``rebuild_from_history`` raises.
        """
        return True

    def rebuild_from_history(self, buffer: pl.DataFrame | None) -> None:
        """The RunningState lazy reseed at the GROUP level. DEFAULT no-op: a stateless group has no carried state
        to rebuild (it recomputes from the ring every minute). A held-state group OVERRIDES this to reseed its
        running-state object from ``buffer`` (the SAME history backfill recomputes over), so that immediately
        after, ``up_to_date(buffer)`` is True and the live state == the backfill state by construction. A group
        whose state CANNOT be cheaply restored to parity (an irreducible change) raises here — which is exactly
        how the applier detects "not real-time-swappable" and escalates, with no separate kind classifier."""
        return None


def daily_snapshot_token(source: pl.DataFrame) -> tuple[int, int, object, float]:
    """A cheap content witness for a daily snapshot, used to key per-session daily-feature caches. The
    snapshot is fixed all day in production, so its derived daily features are identical every minute and
    can be memoized; keying on ``id`` ALONE is unsafe because a garbage-collected frame's address can be
    recycled by a different frame (the singleton-group + fresh-frame unit-test corner), returning a stale
    cache. Pairing ``id`` with ``(height, last date, close-sum)`` makes a collision require an identical
    content shape — robust, and O(rows) cheap relative to the rolling daily OLS the cache avoids."""
    last_date = source["date"].max() if "date" in source.columns and source.height else None
    close_sum = float(source["close"].sum()) if "close" in source.columns and source.height else 0.0
    return (id(source), source.height, last_date, close_sum)


class SessionCache:
    """The ONE engine-owned per-session memo for a Class-A (intraday-invariant) group's derived frame.

    The unified form of the per-group bespoke ``_daily_cache`` boilerplate (daily_beta #238 / overnight_beta
    #262 / liquidity_rank #264 / prior_day / multi_day_* / overnight_intraday_split / return_dispersion): a
    Class-A group's per-(symbol, date) features are a pure function of a per-session-CONSTANT snapshot
    (``daily`` / ``reference`` / ``universe``), so they are computed ONCE per session and broadcast every
    minute. Hold one ``SessionCache`` per group instance and call ``get(witness, compute)``: when ``witness``
    (a content token — typically ``daily_snapshot_token(source)``, paired with any extra dependency witness
    such as the universe membership) is unchanged the cached frame is returned; when it changes (a new
    session / new snapshot / new universe) ``compute`` is re-run and re-cached. NO stale serve across a
    changed witness — the same invariant every bespoke copy proved.

    Value-identical to recompute-every-minute by construction: pure memoization keyed on the input's content;
    only WHEN ``compute`` runs changes, never WHAT it returns."""

    __slots__ = ("_witness", "_value")

    def __init__(self) -> None:
        self._witness: object = None
        self._value: pl.DataFrame | None = None

    def get(self, witness: object, compute: Callable[[], pl.DataFrame]) -> pl.DataFrame:
        if self._value is not None and self._witness == witness:
            return self._value
        value = compute()
        self._witness = witness
        self._value = value
        return value


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
