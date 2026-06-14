"""Per-symbol stateful feature kinds — the in-process state abstraction beyond windowed reductions.

``ReductionGroup`` + ``WindowedSumState`` already give the additive-window class one declaration with two
parity-true execution forms (rolling backfill, fold-and-read live). This module generalizes that to the two
NON-reduction per-symbol state KINDS that dominate the full-flow latency (docs/STATE_ABSTRACTION.md):

  * **Recursive (EMA)** — ``EMAState``: one running ``(num, den)`` accumulator per (symbol, span). polars'
    default ``ewm_mean(span, adjust=True, ignore_nulls=True)`` is the recursion ``num_t = x_t + (1-α)·num_{t-1}``,
    ``den_t = 1 + (1-α)·den_{t-1}``, ``ema_t = num_t/den_t`` (α = 2/(span+1)) over the symbol's present bars.
    Folding one minute updates the two accumulators in O(1); ``ema()`` reads ``num/den``. Used by ``technical``
    (MACD's 12/26/9-span EMAs).
  * **Lag / last-k** — ``LastKState``: a small per-symbol ring of recent minutes keyed by minute-epoch, so a
    TIME-based lag (``value as of minute − L``, null if that exact minute is absent — the ``base.lagged``
    contract) is read in O(1). Used by ``candlestick`` (the prior bar's OHLC for the two-candle patterns).

PARITY (the single invariant that makes the class parity-true): for every kind, ``seed(H); fold(m)`` equals
``seed(H + m)`` cell-for-cell. The live path seeds once then folds each minute; the backfill / batch path
reaches the SAME state by replaying seed+fold over the rolling history — and both evaluate the group's ONE
``assemble()`` over an identical per-symbol state frame. So live and backfill differ only in how the state is
OBTAINED, never in how outputs are DERIVED. Guarded per group by tests/test_fp_stateful.py.
"""
from __future__ import annotations

import os
from abc import abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import polars as pl
import quant_tick

from quantlib.features.base import BatchContext, FeatureGroup

# The per-symbol extrema/lag FOLD is the platform's stateful-emit hotspot at ~625 symbols/shard: the
# monotonic-deque / epoch-ring loops are pure Python and O(symbols) per minute. The Rust kernels
# (quant_tick.rolling_extrema / time_lag_gather) compute the SAME trailing (T−w, T] extrema and the SAME
# time-based lag fresh from the buffer each minute — parity-true by definition of the window/lag, no
# accumulator drift. The Python fold path stays available (FP_STATEFUL_PYTHON=1) as the parity reference.
_USE_RUST_STATEFUL = not os.environ.get("FP_STATEFUL_PYTHON")


@dataclass(frozen=True)
class EMASpec:
    """One recursive EMA the group needs: ``alias`` = ``ewm_mean(span)`` of its per-minute source over the
    symbol's present bars (polars default: adjust=True, ignore_nulls=True).

    The source is EITHER a plain column name (``source``, a scalar column in the group's prepared per-minute
    frame, e.g. ``"close"``) OR a CHAINED combination of already-emitted EMAs in the same minute (``combine``:
    a callable ``(emitted, prepared) -> (n_symbols,) array``, e.g. MACD line = ema12 − ema26). Exactly one of
    ``source`` / ``combine`` is set. ``combine`` lets the engine reproduce polars' sequential ewm-of-an-
    expression (MACD signal = ema9 of (ema12 − ema26)) by feeding this minute's combined value into the fold."""

    alias: str
    span: int
    source: str | None = None
    combine: Callable[[dict[str, np.ndarray], dict[str, np.ndarray]], np.ndarray] | None = None
    rolling: pl.Expr | None = None  # the per-minute series to ewm in the BACKFILL path (default: pl.col(source))


@dataclass(frozen=True)
class LagSpec:
    """One time-based lag the group needs: ``alias`` = ``source`` as of minute − ``minutes`` (null when that
    exact minute is absent — identical to ``base.lagged``). ``source`` is an at-T column in the prepared frame."""

    alias: str
    source: str
    minutes: int


@dataclass(frozen=True)
class ExtremaSpec:
    """One rolling extremum the group needs: ``alias`` = ``max``/``min`` of ``source`` over the trailing
    ``window`` minutes (window ``w`` covers minutes with epoch in ``(T − w·60, T]`` — a minute at exactly
    ``T − w`` is excluded, matching ``rolling_max_by``/``rolling_min_by`` and the Rust ``windowed_reduce``).
    ``op`` is ``"max"`` or ``"min"``."""

    alias: str
    source: str
    window: int
    op: str  # "max" | "min"


class _CodedBuffer:
    """A trailing buffer prepared ONCE per minute for the Rust stateful kernels: (symbol, minute)-sorted with
    an ascending integer symbol code (block order == sorted symbols) and minute-epoch, plus the symbol-source
    columns as numpy. The per-minute WHOLE-BUFFER sort is the stateful-emit's real cost (not the fold), so it
    is done once here and shared by every extrema/lag gather + the latest-row read, never re-sorted per group
    call. ``codes`` / ``minutes`` are the kernel's parallel arrays; ``column(source)`` is a source value array
    aligned to them."""

    def __init__(self, frame: pl.DataFrame, latest: object) -> None:
        uniq = sorted(frame["symbol"].unique().to_list())
        codes = pl.DataFrame(
            {"symbol": uniq, "_c": list(range(len(uniq)))}, schema={"symbol": pl.String, "_c": pl.Int64}
        )
        prepared = (
            frame.join(codes, on="symbol", how="left")
            .with_columns(pl.col("minute").dt.epoch("s").alias("_mi"))
            .sort(["_c", "_mi"])
        )
        self.symbols = uniq
        self.n = len(uniq)
        self.frame = prepared
        self.codes = prepared["_c"].to_numpy()
        self.minutes = prepared["_mi"].to_numpy()
        self.t = int(latest.timestamp())  # type: ignore[attr-defined]
        self._cols: dict[str, np.ndarray] = {}

    def column(self, source: str) -> np.ndarray:
        if source not in self._cols:
            self._cols[source] = self.frame.select(pl.col(source).cast(pl.Float64)).to_numpy().reshape(-1)
        return self._cols[source]


def coded_buffer(frame: pl.DataFrame, latest: object) -> _CodedBuffer:
    """Build the shared symbol-coded, (symbol, minute)-sorted buffer ONCE for a minute, to pass to every
    stateful group's ``step`` so the whole-buffer sort (the stateful-emit cost) is paid once, not per group.
    Valid only when the groups' ``prepare`` is identity over the bar columns (the current stateful groups);
    the buffer must carry every at-T column + extrema/lag source the groups read."""
    return _CodedBuffer(frame, latest)


def rust_extrema(frame: pl.DataFrame, specs: list[ExtremaSpec], latest: object) -> pl.DataFrame:
    """Trailing max/min for every ``ExtremaSpec`` at ``latest``, via ``quant_tick.rolling_extrema`` — one Rust
    backward pass per (symbol, source). Convenience wrapper that codes the buffer then gathers; the engine's
    hot path calls ``rust_extrema_from`` with a shared ``_CodedBuffer`` to avoid re-sorting per group."""
    return rust_extrema_from(_CodedBuffer(frame, latest), specs)


def rust_extrema_from(coded: _CodedBuffer, specs: list[ExtremaSpec]) -> pl.DataFrame:
    """Trailing max/min for every ``ExtremaSpec`` over a pre-coded buffer. Returns a symbol-keyed frame with
    one column per spec ``alias``, NaN where the window is empty (warmup / all-absent) restored to Polars null
    — parity-identical to the monotonic-deque fold's ``extremum()``."""
    data: dict[str, np.ndarray] = {}
    by_source: dict[str, list[ExtremaSpec]] = {}
    for spec in specs:
        by_source.setdefault(spec.source, []).append(spec)
    for source, source_specs in by_source.items():
        windows = sorted({spec.window for spec in source_specs})
        win_secs = [w * 60 for w in windows]
        _sym, _win, mx, mn = quant_tick.rolling_extrema(coded.codes, coded.minutes, coded.column(source), win_secs, coded.t)
        # (symbol, ascending-window) flattened; symbol si's window index wi is at si*nw + wi -> strided slice
        nw = len(windows)
        win_index = {w: i for i, w in enumerate(windows)}
        max_arr = np.asarray(mx, dtype=np.float64)
        min_arr = np.asarray(mn, dtype=np.float64)
        for spec in source_specs:
            wi = win_index[spec.window]
            picks = max_arr if spec.op == "max" else min_arr
            data[spec.alias] = picks[wi::nw]
    return _gathered_frame(coded.symbols, data)


def rust_lags(frame: pl.DataFrame, specs: list[LagSpec], latest: object) -> pl.DataFrame:
    """Time-based lag value for every ``LagSpec`` at ``latest`` via ``quant_tick.time_lag_gather``. Convenience
    wrapper; the engine hot path calls ``rust_lags_from`` with a shared ``_CodedBuffer``."""
    return rust_lags_from(_CodedBuffer(frame, latest), specs)


def rust_lags_from(coded: _CodedBuffer, specs: list[LagSpec]) -> pl.DataFrame:
    """Time-based lag value for every ``LagSpec`` over a pre-coded buffer — one Rust pass per symbol resolving
    each exact prior minute. Returns a symbol-keyed frame with one column per spec ``alias``, NaN where the
    lagged minute is absent restored to Polars null (the ``base.lagged`` self-join contract)."""
    sources = sorted({spec.source for spec in specs})
    lag_minutes = sorted({spec.minutes for spec in specs})
    lag_secs = [m * 60 for m in lag_minutes]
    vals_np = [coded.column(source) for source in sources]
    _sym, lag_cols = quant_tick.time_lag_gather(coded.codes, coded.minutes, vals_np, lag_secs, coded.t)
    nl = len(lag_minutes)
    source_index = {source: i for i, source in enumerate(sources)}
    lag_index = {m: i for i, m in enumerate(lag_minutes)}
    data: dict[str, np.ndarray] = {}
    for spec in specs:
        ci = source_index[spec.source]
        li = lag_index[spec.minutes]
        data[spec.alias] = np.asarray(lag_cols[ci * nl + li], dtype=np.float64)
    return _gathered_frame(coded.symbols, data)


def _gathered_frame(symbols: list[str], data: dict[str, np.ndarray]) -> pl.DataFrame:
    """A symbol-keyed frame of the gathered extrema/lag columns with the NaN sentinel (empty window /
    missing prior minute) restored to Polars null — so ``assemble`` sees the SAME null the rolling
    backfill / Python fold produces (null-propagation + ``when`` guards behave identically). One
    ``fill_nan(None)`` over the value columns (not a per-column ``when``) keeps the emit cheap."""
    return pl.DataFrame({"symbol": symbols, **data}).with_columns(
        pl.col(name).fill_nan(None) for name in data
    )


class ExtremaState:
    """Rolling-extrema KIND (docs/STATE_ABSTRACTION.md): a per-(symbol, spec) MONOTONIC DEQUE of recent
    ``(minute_epoch, value)`` so the trailing max/min is read in O(1) amortized. For a ``max`` deque the
    values are kept monotonically DECREASING front→back; folding a minute pops every back entry the new
    value dominates (``v >= back.value``), then appends ``(epoch, v)``, then evicts the front while it has
    left the window (``front.epoch <= T − w·60``). The current extremum is the FRONT value. ``min`` is the
    mirror (monotonically increasing, pop on ``v <= back.value``). Nulls (NaN) are not pushed, so the
    extremum is over PRESENT bars only — exactly what ``rolling_max_by``/``rolling_min_by`` compute (they
    ignore nulls). An empty deque (warmup / all-null window) reads NaN.

    PARITY (the kind invariant, tests/test_fp_rest_kinds.py): ``seed(H); fold(m)`` == ``seed(H + m)``,
    cell-for-cell, and both == the Rust ``windowed_reduce`` min/max == polars ``rolling_*_by`` over the
    present series."""

    def __init__(self, symbols: list[str], specs: list[ExtremaSpec]) -> None:
        self.symbols = list(symbols)
        self.n = len(self.symbols)
        self.specs = list(specs)
        # one deque per (spec, symbol): deque[(epoch, value)], monotonic per the spec's op
        self.deques: list[list[deque[tuple[int, float]]]] = [
            [deque() for _ in range(self.n)] for _ in self.specs
        ]
        self._latest_epoch: int | None = None

    def fold(self, minute_epoch: int, sources: dict[str, np.ndarray]) -> None:
        """Advance every extremum one minute: push the new value (dominating back entries popped), then
        evict entries that left each window. ``sources`` maps a spec's ``source`` to its ``(n_symbols,)``
        value column at this minute (NaN where the bar is absent — not pushed)."""
        epoch = int(minute_epoch)
        self._latest_epoch = epoch
        # Convert each source column to a Python list once (per-element numpy indexing dominates otherwise).
        source_lists = {source: sources[source].tolist() for source in {spec.source for spec in self.specs}}
        for si, spec in enumerate(self.specs):
            values = source_lists[spec.source]
            cutoff = epoch - spec.window * 60
            is_max = spec.op == "max"
            deques = self.deques[si]
            for sym_i, value in enumerate(values):
                dq = deques[sym_i]
                if value == value:  # not NaN
                    if is_max:
                        while dq and dq[-1][1] <= value:
                            dq.pop()
                    else:
                        while dq and dq[-1][1] >= value:
                            dq.pop()
                    dq.append((epoch, value))
                if dq and dq[0][0] <= cutoff:
                    while dq and dq[0][0] <= cutoff:
                        dq.popleft()

    def extremum(self, alias: str) -> np.ndarray:
        """The trailing extremum for ``alias`` at the latest folded minute (NaN where the window is empty)."""
        si = next(i for i, spec in enumerate(self.specs) if spec.alias == alias)
        deques = self.deques[si]
        out = np.full(self.n, np.nan, dtype=np.float64)
        for sym_i in range(self.n):
            dq = deques[sym_i]
            if dq:
                out[sym_i] = dq[0][1]
        return out


class EMAState:
    """Running adjusted-EWM accumulators per (symbol, EMA). State is two ``(n_emas, n_symbols)`` arrays
    ``num`` / ``den``; ``ema()`` returns ``num/den``. A null input value at a symbol leaves that symbol's
    accumulators untouched and emits NaN at that minute (matching polars ``ignore_nulls=True``), but in the
    minute_agg flow ``close`` is always present, so this is the no-null fast path.

    CHAINED EMAs (an EMA of an expression of other EMAs, e.g. MACD signal = ema9 of (ema12 − ema26)) declare a
    ``combine`` that builds this minute's input from the EMAs already emitted in the SAME fold, in declared
    order — exactly the sequential ewm-of-an-expression polars resolves."""

    def __init__(self, symbols: list[str], specs: list[EMASpec]) -> None:
        self.symbols = list(symbols)
        self.n = len(self.symbols)
        self.specs = list(specs)
        self.alpha = np.array([2.0 / (spec.span + 1.0) for spec in self.specs], dtype=np.float64)
        self.num = np.zeros((len(self.specs), self.n), dtype=np.float64)
        self.den = np.zeros((len(self.specs), self.n), dtype=np.float64)
        self.seen = np.zeros((len(self.specs), self.n), dtype=bool)  # any observation folded yet (per sym,ema)
        self._emitted: dict[str, np.ndarray] = {}  # the latest minute's emitted EMA values, by alias

    def fold(self, sources: dict[str, np.ndarray]) -> None:
        """Advance every EMA one minute. ``sources`` maps a plain EMA's ``source`` name to its ``(n_symbols,)``
        value column at this minute (NaN where the bar is absent). A CHAINED EMA's ``combine`` builds its input
        from the EMAs already emitted THIS minute (+ the prepared sources), so MACD-signal folds this minute's
        macd_line — the exact sequential ewm-of-an-expression polars resolves."""
        emitted: dict[str, np.ndarray] = {}
        for ei, spec in enumerate(self.specs):
            if spec.combine is not None:
                value = spec.combine(emitted, sources)
            else:
                assert spec.source is not None
                value = sources[spec.source]
            present = np.isfinite(value)
            one_minus = 1.0 - self.alpha[ei]
            new_num = np.where(present, value + one_minus * self.num[ei], self.num[ei])
            new_den = np.where(present, 1.0 + one_minus * self.den[ei], self.den[ei])
            self.num[ei] = new_num
            self.den[ei] = new_den
            self.seen[ei] |= present
            out = np.full(self.n, np.nan, dtype=np.float64)
            np.divide(self.num[ei], self.den[ei], out=out, where=self.seen[ei])
            out[~self.seen[ei]] = np.nan
            emitted[spec.alias] = out
        self._emitted = emitted

    def ema(self, alias: str) -> np.ndarray:
        """The latest emitted value of EMA ``alias`` (NaN where no observation has been folded yet)."""
        return self._emitted[alias]


class LastKState:
    """Per (symbol, lagged-column) ring of recent minute values keyed by minute-epoch. ``lag()`` returns the
    value as of ``minute − L`` (NaN when that exact minute was never folded), matching the TIME-based
    ``base.lagged`` self-join. Bounded to the deepest lag the group declares."""

    def __init__(self, symbols: list[str], specs: list[LagSpec]) -> None:
        self.symbols = list(symbols)
        self.index = {symbol: i for i, symbol in enumerate(self.symbols)}
        self.n = len(self.symbols)
        self.specs = list(specs)
        self.sources = sorted({spec.source for spec in self.specs})
        self.depth = max((spec.minutes for spec in self.specs), default=1)
        # history[source] : minute_epoch -> (n_symbols,) value column (NaN where the symbol had no bar)
        self.history: dict[str, dict[int, np.ndarray]] = {source: {} for source in self.sources}
        self._latest_epoch: int | None = None

    def fold(self, minute_epoch: int, sources: dict[str, np.ndarray]) -> None:
        """Record this minute's per-symbol source columns, then evict minutes older than the deepest lag."""
        self._latest_epoch = int(minute_epoch)
        for source in self.sources:
            self.history[source][int(minute_epoch)] = sources[source]
        cutoff = int(minute_epoch) - self.depth * 60
        for source in self.sources:
            stale = [epoch for epoch in self.history[source] if epoch < cutoff]
            for epoch in stale:
                del self.history[source][epoch]

    def lag(self, alias: str) -> np.ndarray:
        """The lagged column for ``alias`` at the latest folded minute (NaN where the lagged minute is absent)."""
        assert self._latest_epoch is not None
        spec = next(spec for spec in self.specs if spec.alias == alias)
        target = self._latest_epoch - spec.minutes * 60
        return self.history[spec.source].get(target, np.full(self.n, np.nan, dtype=np.float64))


class StatefulGroup(FeatureGroup):
    """Base for a per-symbol stateful (non-reduction) feature group. The group declares its state needs as
    KINDS — recursive EMAs (``ema_specs``), time-based lags (``lag_specs``), and/or rolling extrema
    (``extrema_specs``) over a prepared per-minute frame (``prepare``) — and writes ``assemble`` ONCE over a
    per-symbol STATE FRAME that carries, for the latest minute: the prepared at-T columns, each EMA's value
    (its ``alias``), each lag's value (its ``alias``), and each extremum's value (its ``alias``). ``compute``
    (backfill, source of truth) and ``compute_latest`` (live) are GENERATED and build that identical state
    frame two ways (rolling vs fold), so they cannot diverge — the parity the abstraction exists to guarantee.

    A group that ALSO needs windowed reductions (e.g. technical's RSI/Bollinger) overrides ``compute_latest``
    to join the reduction columns onto the state frame before ``assemble`` — those stay on the existing Rust
    reduction path; only the per-symbol recursive/lag state moves here."""

    @abstractmethod
    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Per-minute at-T columns derived from the input frame (no rolling/EMA): the EMA/lag SOURCES and any
        scalar columns ``assemble`` reads at T. Returns the frame with those columns added (keeps symbol/minute)."""

    def ema_specs(self) -> list[EMASpec]:
        """The recursive EMAs this group maintains (ordered so a chained EMA follows its source). Default none."""
        return []

    def lag_specs(self) -> list[LagSpec]:
        """The time-based lags this group maintains. Default none."""
        return []

    def extrema_specs(self) -> list[ExtremaSpec]:
        """The rolling max/min extrema this group maintains. Default none."""
        return []

    @abstractmethod
    def assemble(self) -> dict[str, pl.Expr]:
        """{feature: expr} over the state frame's columns (prepared at-T columns + each EMA/lag alias). The
        SAME expressions run in compute (backfill) and compute_latest (live)."""

    def reduction_columns(self, ctx: BatchContext) -> pl.DataFrame | None:
        """Optional WINDOWED-REDUCTION columns (one row per symbol at T) that ``assemble`` also reads — for a
        HYBRID group (e.g. technical's RSI/Bollinger/SMA) that keeps those on the Rust reduction path while its
        recursive/lag state moves to the StatefulEngine. Default none (a pure recursive/lag group). The engine
        joins these onto the state row before ``assemble``; the group's own ``compute_latest`` joins the SAME
        columns, so the fast and certified live forms agree."""
        return None

    def _input_columns(self) -> tuple[str, ...]:
        return self.inputs[0].columns

    def _state_frame_rolling(self, ctx: BatchContext) -> tuple[pl.DataFrame, object]:
        """The state frame at EVERY minute (backfill): prepared at-T columns + each EMA as a polars
        ``ewm_mean`` and each lag as a TIME-based self-join — the rolling source of truth the fold mirrors."""
        frame = ctx.frame(self.inputs[0].name).select(self._input_columns()).sort(["symbol", "minute"])
        frame = self.prepare(frame)
        # Add EMAs in declared order, each in its OWN with_columns so a CHAINED EMA's rolling expr can read the
        # prior EMA columns (the sequential dependency polars resolves left-to-right within a chained ewm).
        for spec in self.ema_specs():
            base = spec.rolling if spec.rolling is not None else pl.col(spec.source)
            frame = frame.with_columns(base.ewm_mean(span=spec.span).over("symbol").alias(spec.alias))
        for spec in self.lag_specs():
            lag = frame.select(
                pl.col("symbol"),
                (pl.col("minute") + pl.duration(minutes=spec.minutes)).alias("minute"),
                pl.col(spec.source).alias(spec.alias),
            )
            frame = frame.join(lag, on=["symbol", "minute"], how="left")
        extrema_cols: list[pl.Expr] = []
        for spec in self.extrema_specs():
            base = pl.col(spec.source)
            size = f"{spec.window}m"
            rolling = (
                base.rolling_max_by("minute", window_size=size)
                if spec.op == "max"
                else base.rolling_min_by("minute", window_size=size)
            )
            extrema_cols.append(rolling.over("symbol").alias(spec.alias))
        if extrema_cols:
            frame = frame.with_columns(extrema_cols)
        return frame, frame["minute"].max()

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated BACKFILL form (source of truth): build the rolling state frame, evaluate ``assemble``."""
        frame, _ = self._state_frame_rolling(ctx)
        feats = self.assemble()
        return frame.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()]).select(
            ["symbol", "minute", *self.feature_names]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated LIVE form: the same as ``compute`` filtered to T. Subclasses needing reduction columns
        override to join them onto the latest state row before ``assemble``; the default keeps full parity by
        deriving the EMAs/lags with the rolling exprs then taking T (the stateful engine is the FAST live form,
        guarded == this)."""
        frame, latest = self._state_frame_rolling(ctx)
        feats = self.assemble()
        out = frame.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
        return out.filter(pl.col("minute") == latest).select(["symbol", "minute", *self.feature_names])


class StatefulEngine:
    """The live FAST path for one ``StatefulGroup``: seed the EMA/lag state from a buffer, then fold one minute
    at a time (O(symbols × state)) and emit ``assemble`` from a per-symbol state frame — the per-symbol twin of
    ``IncrementalEngine`` for the recursive/lag kinds. Parity is by construction: ``prepare`` + ``assemble`` are
    the group's own (shared with backfill), and the state's ``fold == reseed`` invariant is the parity gate.

    Usage: ``seed(buffer)`` once (replays the buffer), then ``step(frame)`` each minute (``frame`` = trailing
    buffer incl. the new latest minute) -> the group's feature frame for the latest minute. Subclasses of
    StatefulGroup that also need reduction columns supply them via ``reduction_columns`` to ``step``."""

    def __init__(self, group: StatefulGroup, use_rust: bool | None = None) -> None:
        self.group = group
        # The EMA fold is O(symbols) vectorized numpy and stays; the extrema/lag folds (the per-symbol
        # deque/ring Python loops) move to the Rust kernels unless the Python reference is forced.
        self.use_rust = _USE_RUST_STATEFUL if use_rust is None else use_rust
        self.ema_specs = group.ema_specs()
        self.lag_specs = group.lag_specs()
        self.extrema_specs = group.extrema_specs()
        # The plain EMA sources to read from the prepared row each minute (chained EMAs build from emitted EMAs).
        self.ema_root_sources = sorted({spec.source for spec in self.ema_specs if spec.source is not None})
        self.lag_sources = sorted({spec.source for spec in self.lag_specs})
        self.extrema_sources = sorted({spec.source for spec in self.extrema_specs})
        self.symbols: list[str] | None = None
        self.ema_state: EMAState | None = None
        self.lag_state: LastKState | None = None
        self.extrema_state: ExtremaState | None = None

    def _prepared_latest(self, frame: pl.DataFrame, minute: object) -> pl.DataFrame:
        """The group's prepared at-T columns for ``minute``, one row per symbol, symbol-sorted."""
        prepared = self.group.prepare(frame.select(self.group._input_columns()).sort(["symbol", "minute"]))
        return prepared.filter(pl.col("minute") == minute).sort("symbol")

    def _source_columns(self, row: pl.DataFrame) -> dict[str, np.ndarray]:
        """Symbol-aligned (n_symbols,) numpy columns for every EMA root / lag / extrema source, NaN where null."""
        needed = sorted(set(self.ema_root_sources) | set(self.lag_sources) | set(self.extrema_sources))
        out: dict[str, np.ndarray] = {}
        for source in needed:
            out[source] = row.select(pl.col(source).cast(pl.Float64)).to_numpy().reshape(-1)
        return out

    def _fold_minute(self, frame: pl.DataFrame, minute: object) -> None:
        row = self._prepared_latest(frame, minute)
        assert self.symbols is not None and row.height == len(self.symbols), "stateful: symbol set changed; re-seed"
        sources = self._source_columns(row)
        if self.ema_state is not None:
            self.ema_state.fold(sources)
        # The extrema/lag kinds are emitted fresh from the buffer via Rust each minute (no per-minute fold);
        # the Python deque/ring folds only run as the parity reference (FP_STATEFUL_PYTHON).
        if self.lag_state is not None:
            self.lag_state.fold(int(minute.timestamp()), sources)  # type: ignore[attr-defined]
        if self.extrema_state is not None:
            self.extrema_state.fold(int(minute.timestamp()), sources)  # type: ignore[attr-defined]

    def seed(self, buffer_frame: pl.DataFrame) -> None:
        """Establish the symbol set and fold every buffered minute into fresh state (== batch over the buffer;
        also the daily-resync / crash-recovery entry point)."""
        self.symbols = sorted(buffer_frame["symbol"].unique().to_list())
        self.ema_state = EMAState(self.symbols, self.ema_specs) if self.ema_specs else None
        # On the Rust path the extrema/lag values are gathered from the buffer at emit-time, so their Python
        # state is never allocated or folded; only the EMA recursion is folded minute-to-minute.
        self.lag_state = (
            LastKState(self.symbols, self.lag_specs) if self.lag_specs and not self.use_rust else None
        )
        self.extrema_state = (
            ExtremaState(self.symbols, self.extrema_specs) if self.extrema_specs and not self.use_rust else None
        )
        folds_minutes = self.ema_state is not None or self.lag_state is not None or self.extrema_state is not None
        if folds_minutes:
            for minute in sorted(buffer_frame["minute"].unique()):
                self._fold_minute(buffer_frame, minute)

    def _state_row(
        self, frame: pl.DataFrame, latest: object, coded: _CodedBuffer | None = None
    ) -> pl.DataFrame:
        """The per-symbol state frame at ``latest``: prepared at-T columns + each EMA/lag value as a column —
        the SAME shape ``assemble`` consumes in the rolling backfill path.

        On the Rust extrema/lag path the buffer is PREPARED + symbol-coded + sorted ONCE (``_CodedBuffer`` —
        the per-minute whole-buffer sort that dominates the stateful emit) and shared by the latest-row read
        and every gather, instead of re-sorting per group call. ``coded`` lets the caller share ONE coded
        buffer across ALL stateful groups in a minute (the buffer carries every bar column the groups' identity
        ``prepare`` needs), cutting the redundant per-group sort — the real stateful-emit lever."""
        rust_needs = self.use_rust and bool(self.lag_specs or self.extrema_specs)
        if rust_needs:
            if coded is None:
                coded = _CodedBuffer(self.group.prepare(frame.select(self.group._input_columns())), latest)
            at_t_cols = [col for col in self.group._input_columns() if col not in ("symbol", "minute")]
            row = coded.frame.filter(pl.col("_mi") == coded.t).select(["symbol", "minute", *at_t_cols])
        else:
            coded = None  # type: ignore[assignment]
            row = self._prepared_latest(frame, latest)
        columns: list[pl.Series] = []
        if self.ema_state is not None:
            for spec in self.ema_specs:
                columns.append(pl.Series(spec.alias, self.ema_state.ema(spec.alias), dtype=pl.Float64))
        row = row.with_columns(columns) if columns else row
        if self.lag_specs:
            if self.use_rust:
                row = row.join(rust_lags_from(coded, self.lag_specs), on="symbol", how="left")
            else:
                assert self.lag_state is not None
                row = row.with_columns(
                    [pl.Series(spec.alias, self.lag_state.lag(spec.alias), dtype=pl.Float64) for spec in self.lag_specs]
                )
        if self.extrema_specs:
            if self.use_rust:
                row = row.join(rust_extrema_from(coded, self.extrema_specs), on="symbol", how="left")
            else:
                assert self.extrema_state is not None
                row = row.with_columns(
                    [pl.Series(spec.alias, self.extrema_state.extremum(spec.alias), dtype=pl.Float64) for spec in self.extrema_specs]
                )
        return row

    def step(
        self, frame: pl.DataFrame, ctx: BatchContext | None = None, coded: _CodedBuffer | None = None
    ) -> pl.DataFrame:
        """Fold the new latest minute and emit the group's features from state. ``frame`` is the trailing
        buffer incl. the new minute; seeds lazily on first call. ``ctx`` is required iff the group is HYBRID
        (declares ``reduction_columns``) — the engine joins those windowed-reduction columns onto the state row
        (the SAME columns the group's own ``compute_latest`` joins), so the fast and certified live forms agree.
        ``coded`` is an optional shared ``_CodedBuffer`` (``coded_buffer(frame)``) reused across all stateful
        groups in a minute so the whole-buffer sort happens once, not once per group."""
        latest = frame["minute"].max()
        if self.symbols is None:
            self.seed(frame)
        elif self.ema_state is not None or self.lag_state is not None or self.extrema_state is not None:
            # Only fold per-minute state that is maintained incrementally (the EMA recursion, plus the Python
            # deque/ring reference). On the Rust path with no EMAs, extrema/lags are gathered from the buffer
            # at emit-time, so there is nothing to fold here.
            self._fold_minute(frame, latest)
        state = self._state_row(frame, latest, coded)
        reduction = self.group.reduction_columns(ctx) if ctx is not None else None
        if reduction is not None:
            state = state.join(reduction, on="symbol", how="left")
        feats = self.group.assemble()
        return (
            state.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *self.group.feature_names])
        )
