"""Incremental windowed-sum state — the pre-prepped-between-minutes path to a minimal-compute minute mark.

Instead of re-scanning the whole trailing buffer every minute (the current 86%-shuffling cost), keep a
running per-(symbol, window) sum of each value column and, when a new minute arrives, ADD the new minute and
EXPIRE the minutes that just fell out of each window — O(symbols × windows × cols) ≈ a few ms, vs O(buffer ×
…). This is the same windowed sum the Rust kernel computes, so the declarative groups assemble from these
sums unchanged.

Window semantics match ``quant_tick.windowed_sums`` exactly: window ``w`` covers minutes with epoch in
``(T − w·60, T]`` (a minute at exactly ``T − w`` is excluded).

PARITY: these running sums must equal the batch recompute within tolerance (guarded by
tests/test_fp_incremental.py). Incremental float sums drift slowly; bound it by re-seeding from the buffer
each session (``seed``), which also gives crash recovery. Backfill stays the polars rolling form (truth);
live uses this; the parity test proves they agree.

V1 derived each new minute's value columns over the WHOLE trailing buffer (correct, but O(buffer) — the last
big cost at the minute mark). V2 (this module) SLICE-DERIVES: the cheap short-lag columns (returns, products,
power sums, presence/square) only need the last few bars, so they're derived over each symbol's last
``max_lag+1`` rows (a per-symbol row tail — positionally exact for sparse symbols, so parity holds even when a
symbol skips minutes); the columns that depend on long history — a frame-relative OLS time axis and a
cumulative regressor (OBV) — are
maintained as running per-symbol engine state (``stateful_regressors()``). Both produce the IDENTICAL value
matrix the batch would, so the running sums (and therefore every feature) stay parity-true by construction.
"""

from __future__ import annotations

import os

import numpy as np
import polars as pl

from quantlib.features.declarative import _TIME_ORIGIN_LAG as declarative_TIME_ORIGIN_LAG
from quantlib.features.declarative import (
    ReductionGroup,
    StatefulRegressor,
    assemble_from_long,
    build_assemble_plan,
    build_plan,
    emit_numpy,
    emit_rust,
    emit_rust_unified,
    resolve_points,
)
from quantlib.features.metrics import record_point_ring_parity
from quantlib.features.point_ring import PointRing, point_frame_from_ring, point_specs
from quantlib.features.slice_derive import lag_specs, rewrite_global, rust_slice_derive
from quantlib.features.state_spine import obv_increment, price_volume_safe_cols, spine_active

_OLS_KEYS = ("b", "x", "y", "xy", "xx", "yy")

_POINT_RING_ABS_TOL = 1e-12  # __pt_ columns are EXACT carries, not aggregates -> absolute tol, not relative


def _record_point_ring_parity(ring_frame: pl.DataFrame, truth_frame: pl.DataFrame) -> None:
    """The FP_POINT_RING_PARITY shadow comparison (monitoring-only): compare the ring's ``__pt_`` columns to
    the whole-buffer ``resolve_points`` truth and record the worst absolute divergence + a breach flag.

    The ring is a SUPERSET of symbols (the fixed session index); ``resolve_points`` emits only the present
    symbols. Compare on the symbol INTERSECTION: a symbol in the ring but not the truth is expected coverage
    (``only_ring``), but a symbol in the truth but NOT the ring (``only_truth>0``) is a real breach (the ring
    dropped a symbol it should carry). Per common-symbol ``__pt_`` cell: (both NaN) OR ``abs(a-b) <= 1e-12``
    absolute — these are exact carries, not aggregates, so the tolerance is absolute, not relative. Any failing
    cell, or ``only_truth>0``, is a breach. Never raises (a shadow must never crash capture); on any structural
    mismatch it records a breach with +inf so it surfaces rather than silently passing."""
    point_cols = [c for c in truth_frame.columns if c.startswith("__pt_")]
    if not point_cols:
        record_point_ring_parity(0.0, breached=False)
        return
    truth = truth_frame.sort("symbol")
    ring = ring_frame.sort("symbol")
    truth_syms = set(truth["symbol"].to_list())
    ring_syms = set(ring["symbol"].to_list())
    only_truth = truth_syms - ring_syms  # symbols the truth has that the ring dropped -> a real breach
    common = sorted(truth_syms & ring_syms)
    if not common:
        record_point_ring_parity(float("inf"), breached=True)
        return
    truth_c = truth.filter(pl.col("symbol").is_in(common)).sort("symbol")
    ring_c = ring.filter(pl.col("symbol").is_in(common)).sort("symbol")
    max_abs_diff = 0.0
    breached = bool(only_truth)
    for col in point_cols:
        if col not in ring_c.columns:
            breached = True
            max_abs_diff = float("inf")
            continue
        a = truth_c[col].to_numpy().astype(np.float64)
        b = ring_c[col].to_numpy().astype(np.float64)
        both_nan = np.isnan(a) & np.isnan(b)
        diff = np.abs(a - b)
        # a NaN-vs-finite cell is a divergence: diff is NaN there -> treat as a breach with +inf.
        nan_mismatch = np.isnan(a) ^ np.isnan(b)
        if nan_mismatch.any():
            breached = True
            max_abs_diff = float("inf")
        finite = diff[~both_nan & ~nan_mismatch]
        if finite.size:
            col_max = float(finite.max())
            max_abs_diff = max(max_abs_diff, col_max)
            if col_max > _POINT_RING_ABS_TOL:
                breached = True
    record_point_ring_parity(max_abs_diff, breached=breached)


class IncrementUnderfilled(Exception):
    """Raised when a window that the supplied buffer HAD enough history to fill did NOT end up populated —
    the data was PRESENT in the buffer handed to the state but the state failed to absorb it (the 7/13-col
    schema ShapeError, a sort/index mismatch, a silently-dropped slot/minute). This is the "FAILED" arm of
    the universal three-way distinction (FULL / legitimately-not-yet-full / FAILED); it is source-agnostic —
    the SAME failure whether the buffer came from a warm-start seed or from live minutes accumulating. It is
    NOT raised for a window that is legitimately not-yet-full because the buffer itself was shorter than the
    window (a newly-listed ticker / first day / genuine gap). Fail-fast per CLAUDE.md ("let errors raise / no
    lazy graceful degradation") so a partial fill is caught loudly instead of silently under-warming."""


class IncrementInvariantError(Exception):
    """Raised when a ``WindowedSumState`` fails an INTERNAL self-consistency invariant — its own bookkeeping
    contradicts its own buffer (the span tracker disagrees with the buffered minutes; a per-window expiry
    pointer retained a minute that left the window or expired one still inside it; or, under the deep check,
    a running sum no longer equals the sum over its window's buffered minutes). This is corruption of the
    fold/expire/trim cycle itself, independent of how the window was filled — checkable at ANY time, not just
    after a seed. Distinct from ``IncrementUnderfilled`` (which is a present-but-not-absorbed INPUT problem,
    not internal corruption)."""


# How far (minutes) behind the incoming minute to pin the rolling time-OLS origin each fold. Small and fixed
# so the time regressor's x stays O(1) for every window — keeping ``b·Σxx − (Σx)²`` well conditioned instead
# of a difference of large near-equal sums (the source of the near-perfect-fit time-OLS incremental breach).
# Defined in declarative.py (the shared origin constant) so the batch latest-pin cannot drift from this fold pin.
_TIME_ORIGIN_LAG = declarative_TIME_ORIGIN_LAG


class SymbolSetExpanded(Exception):
    """Raised when a minute carries a symbol OUTSIDE the engine's fixed session index (a genuinely new
    ticker). The caller re-seeds the engine from the current buffer — the same parity-safe daily-resync
    path — which rebuilds the index to include it. Absent symbols (a SUBSET of the index) are handled
    inline (zero contribution); only an EXPANSION of the set needs a re-seed."""


class WindowedSumState:
    """Running per-(window, symbol, col) sums, updated one minute at a time. ``values`` passed to ``update``
    is an ``(n_symbols, n_cols)`` float matrix (nulls already filled to 0), symbol-aligned to ``symbols``."""

    def __init__(self, symbols: list[str], windows: tuple[int, ...], n_cols: int) -> None:
        self.symbols = list(symbols)
        self.n = len(self.symbols)
        self.windows = tuple(int(w) for w in windows)
        self.n_cols = n_cols
        self.running = np.zeros((len(self.windows), self.n, n_cols), dtype=np.float64)
        # Neumaier (improved-Kahan) compensation for the running sum. The running ``+=`` / ``-=`` chain on
        # LARGE-MAGNITUDE columns (raw share volume, its square) accumulates rounding the batch fresh-sum does
        # not, so the plain running sum drifts past the 1e-9 ABSOLUTE parity tolerance (worst_rel stays ~1e-15
        # — the float floor — but worst_abs grows with magnitude). ``_comp`` carries the lost low-order bits so
        # the EFFECTIVE sum ``running + _comp`` tracks the exact window sum to ~machine precision and matches
        # the batch fresh-sum within tolerance. This is the stable-summation rewrite the ``incremental_safe``
        # gate referenced; it changes NO algebra (the effective sum is the same mathematical quantity), only
        # the float-accumulation order, so it is value-improving and parity-true, not a value change.
        self._comp = np.zeros_like(self.running)
        self._buf_epoch: list[int] = []
        self._buf_vals: list[np.ndarray] = []
        self._oldest = [0] * len(self.windows)  # per-window index of the oldest minute still in the sum
        # Span of folded history (in epoch seconds), tracked across trims so ``populated`` survives the
        # memory eviction that drops buffered minutes once they leave the longest window. ``_first_epoch``
        # is the EARLIEST minute ever folded since seed (the left edge of the absorbed history);
        # ``_last_epoch`` is the latest. A window ``w`` is POPULATED when the absorbed history reaches at
        # least ``w`` minutes behind the latest minute — i.e. its lower edge ``last − w·60`` has slid past
        # real data rather than being truncated by the seed's left edge.
        self._first_epoch: int | None = None
        self._last_epoch: int | None = None

    def update(self, minute_epoch: int, values: np.ndarray) -> None:
        """Fold one new minute into every window's running sum, then expire minutes now outside each window."""
        index = len(self._buf_epoch)
        if self._first_epoch is None:
            self._first_epoch = int(minute_epoch)
        self._last_epoch = int(minute_epoch)
        self._buf_epoch.append(int(minute_epoch))
        self._buf_vals.append(values)
        for wi, w in enumerate(self.windows):
            self._neumaier_add(wi, values)  # the new minute is in every window (epoch == T > T - w)
            cutoff = minute_epoch - w * 60
            oldest = self._oldest[wi]
            while (
                oldest <= index and self._buf_epoch[oldest] <= cutoff
            ):  # minute at/under T-w left the window
                self._neumaier_add(wi, -self._buf_vals[oldest])
                oldest += 1
            self._oldest[wi] = oldest

    def _neumaier_add(self, wi: int, addend: np.ndarray) -> None:
        """Neumaier-compensated ``self.running[wi] += addend``: accumulate into ``running[wi]`` while routing
        the per-element rounding loss into ``_comp[wi]``, so the EFFECTIVE sum ``running + _comp`` stays
        exact to ~machine precision through the long add/expire chain (vs a plain ``+=`` that drifts on
        large-magnitude columns). Pure float-order change; the mathematical sum is unchanged."""
        current = self.running[wi]
        total = current + addend
        # Neumaier: when |current| >= |addend| the low bits lost are in addend, else in current.
        big_current = np.abs(current) >= np.abs(addend)
        loss = np.where(big_current, (current - total) + addend, (addend - total) + current)
        self._comp[wi] += loss
        self.running[wi] = total

    def trim(self) -> None:
        """Drop buffered minutes older than the longest window (bound memory). Call after each update."""
        if not self._buf_epoch:
            return
        keep_from = min(self._oldest)
        if keep_from:
            self._buf_epoch = self._buf_epoch[keep_from:]
            self._buf_vals = self._buf_vals[keep_from:]
            self._oldest = [o - keep_from for o in self._oldest]

    def rebase_time_axis(
        self, delta_minutes: float, time_ols_cols: list[tuple[int, int, int, int, int]]
    ) -> None:
        """Shift every OLS time regressor's x-axis by ``-delta_minutes`` in place (origin moves forward by
        ``delta_minutes``), so the regression's x stays small and the ``b·Σxx − (Σx)²`` variance term is well
        conditioned instead of a difference of large near-equal sums (the source of the time-OLS incremental-
        vs-batch breach on near-perfect fits — price_r2, clean_momentum). Applied to BOTH the per-window running
        sums and every buffered minute matrix (so expiry subtracts the shifted value), preserving the sums'
        invariant. OLS is origin-invariant, so slope/r2/corr are unchanged in exact arithmetic; the only effect
        is to keep the float cancellation small. ``time_ols_cols`` is ``(b, x, y, xy, xx)`` column indices per
        time regression. Under ``x → x − Δ``: ``xx → xx − 2Δ·x + Δ²·b``, ``xy → xy − Δ·y``, ``x → x − Δ·b`` (xx
        and xy read the OLD x/y, so update them before x)."""
        delta = float(delta_minutes)
        if delta == 0.0:
            return
        # Realize the Neumaier compensation into ``running`` for ONLY the time-OLS columns the shift mutates,
        # so the shift acts on their full effective sum and ``_comp`` does not go stale for them (rebase mutates
        # ``running`` directly). Every OTHER column's ``running`` / ``_comp`` is left EXACTLY untouched: a
        # co-resident non-time group (e.g. an autocorrelation corr group sharing the engine with price_volume's
        # obv time regression) must fold bit-identically to its standalone engine — realizing the WHOLE array's
        # compensation here would collapse such a group's compensated-to-exactly-zero cell (a flat-name ``Σxx``)
        # into a ~1e-22 residue, straddling the OLS defined-guard and emitting a spurious corr where the batch
        # NULLs (a shared-engine-only parity breach). Restricting realization to the shifted columns keeps the
        # rebase a no-op for everyone else.
        touched = sorted({idx for cols in time_ols_cols for idx in cols})
        running_flat = self.running.reshape(-1, self.running.shape[-1])
        comp_flat = self._comp.reshape(-1, self._comp.shape[-1])
        running_flat[:, touched] += comp_flat[:, touched]
        comp_flat[:, touched] = 0.0
        for matrix in (running_flat, *self._buf_vals):
            for b_i, x_i, y_i, xy_i, xx_i in time_ols_cols:
                b_col = matrix[..., b_i]
                x_col = matrix[..., x_i]
                y_col = matrix[..., y_i]
                matrix[..., xx_i] += -2.0 * delta * x_col + delta * delta * b_col
                matrix[..., xy_i] += -delta * y_col
                matrix[..., x_i] = x_col - delta * b_col

    def sums(self, window: int) -> np.ndarray:
        """The current ``(n_symbols, n_cols)`` running sum for ``window`` (minutes) — the Neumaier-corrected
        EFFECTIVE sum ``running + _comp`` (the compensation carries the low-order bits the long add/expire
        chain would otherwise lose on large-magnitude columns), so it matches the batch fresh-sum to
        ~machine precision."""
        wi = self.windows.index(window)
        return self.running[wi] + self._comp[wi]

    def corrected(self) -> np.ndarray:
        """The full ``(n_windows, n_symbols, n_cols)`` Neumaier-corrected running-sum array ``running + _comp``
        — the value the emit path must read so every assembled feature uses the compensated (parity-true)
        sum, not the drifting raw ``running``."""
        return self.running + self._comp

    def observed_span_minutes(self) -> float:
        """How many minutes of history the state has ABSORBED: latest folded minute − earliest folded minute,
        in minutes. ``0`` before any fold. Maintained by the shared ``update`` fold (so it is identical
        whether the depth came from a warm-start seed or from live minutes accumulating), and survives
        ``trim`` (tracked from ``_first/_last_epoch``, not the evicted buffer) so it reflects the full
        absorbed depth even after memory eviction."""
        if self._first_epoch is None or self._last_epoch is None:
            return 0.0
        return (self._last_epoch - self._first_epoch) / 60.0

    def buffer_span_minutes(self) -> float:
        """The span the CURRENTLY-RETAINED buffer covers (newest − oldest buffered minute), in minutes. After
        ``trim`` this is ≤ ``observed_span_minutes`` (older minutes are evicted once past the longest window);
        before any trim it equals it. Read from the live buffer, so it tracks the actual folded minutes."""
        if not self._buf_epoch:
            return 0.0
        return (self._buf_epoch[-1] - self._buf_epoch[0]) / 60.0

    def populated(self, window: int) -> bool:
        """The continuous, SOURCE-AGNOSTIC populated property: True when the absorbed history reaches a FULL
        ``window`` minutes behind the latest minute (so the window holds its full required depth rather than
        being truncated by a short left edge). Derived the SAME way at all times — at init after a seed, or in
        steady state after live expiry/repopulation — because ``observed_span_minutes`` is maintained by the
        shared fold. A window ≤ the observed span is full; a longer one is still warming."""
        return self.observed_span_minutes() >= float(window)

    def check_invariants(self, *, deep: bool = False) -> None:
        """UNIVERSAL internal self-consistency check — the state's own bookkeeping must agree with its own
        buffer. Checkable at ANY time (init or steady state), independent of how the windows filled. Cheap
        checks always run; the O(buffer) running-sum reconstruction runs only when ``deep`` (kept off the hot
        path). Raises ``IncrementInvariantError`` on any contradiction.

          1. Span ↔ buffer: the tracked newest epoch equals the buffer's newest, and the tracked earliest is
             no newer than the buffer's oldest (the earliest may predate the trimmed buffer, never postdate).
          2. Expiry pointers: each window's ``_oldest`` partitions the buffer correctly — every retained
             minute (index ≥ ``_oldest``) is strictly INSIDE the window (epoch > last − w·60) and every
             expired minute (index < ``_oldest``) is at/under that cutoff. A mis-fold/mis-expire trips here.
          3. (deep) Running sum ↔ buffer: each window's running sum equals the sum over exactly its retained
             in-window minutes — catches a dropped/duplicated fold or numeric corruption at source."""
        if not self._buf_epoch:
            if self._first_epoch is not None or self._last_epoch is not None:
                raise IncrementInvariantError("span tracker set but buffer empty")
            return
        if self._last_epoch != self._buf_epoch[-1]:
            raise IncrementInvariantError(
                f"last-epoch tracker {self._last_epoch} != buffer newest {self._buf_epoch[-1]}"
            )
        if self._first_epoch is None or self._first_epoch > self._buf_epoch[0]:
            raise IncrementInvariantError(
                f"first-epoch tracker {self._first_epoch} is newer than buffer oldest {self._buf_epoch[0]}"
            )
        last = self._buf_epoch[-1]
        for wi, w in enumerate(self.windows):
            oldest = self._oldest[wi]
            cutoff = last - w * 60
            if not (0 <= oldest <= len(self._buf_epoch)):
                raise IncrementInvariantError(f"window {w}: _oldest {oldest} out of range")
            if oldest > 0 and self._buf_epoch[oldest - 1] > cutoff:
                raise IncrementInvariantError(
                    f"window {w}: retained an expired minute at index {oldest - 1} (inside _oldest)"
                )
            if oldest < len(self._buf_epoch) and self._buf_epoch[oldest] <= cutoff:
                raise IncrementInvariantError(
                    f"window {w}: failed to expire a minute at index {oldest} (epoch ≤ cutoff)"
                )
            if deep:
                expected = (
                    np.sum(self._buf_vals[oldest:], axis=0)
                    if oldest < len(self._buf_vals)
                    else np.zeros((self.n, self.n_cols), dtype=np.float64)
                )
                effective = (
                    self.running[wi] + self._comp[wi]
                )  # the Neumaier-corrected sum (what consumers read)
                if not np.allclose(effective, expected, rtol=1e-9, atol=1e-9, equal_nan=True):
                    raise IncrementInvariantError(
                        f"window {w}: running sum diverged from its buffered in-window minutes"
                    )

    def assert_ready(self, buffer_span_minutes: float) -> None:
        """The three-way FILL check, source-agnostic. ``buffer_span_minutes`` is the span the buffer handed to
        the state actually carried — the span of the seed frame at init, or of the trailing frame in steady
        state; it is NOT warm-start-specific. For every declared window:

          * FULL — ``observed_span >= window`` → the state absorbed its full depth → OK.
          * LEGITIMATELY not-yet-full — the buffer itself was shorter than the window
            (``buffer_span < window``: newly-listed ticker, first day, genuine short history) → correctly not
            populated → OK (no raise; emits partial/NaN as today).
          * FAILED — the buffer HAD ``>= window`` minutes but the state's observed span is short (data present
            in the supplied buffer, not absorbed: the ShapeError, a schema/index mismatch, a dropped minute)
            → RAISE ``IncrementUnderfilled``.

        Fires only on the third arm (``buffer_span >= window`` but ``observed_span < window``). Also runs the
        internal ``check_invariants`` so a corruption surfaces here too. Identical at init and in steady
        state — the post-seed assert is just one call site of this universal property."""
        self.check_invariants()
        observed = self.observed_span_minutes()
        for window in self.windows:
            if buffer_span_minutes >= float(window) and observed < float(window):
                raise IncrementUnderfilled(
                    f"the {window}m window is underfilled: the supplied buffer carried "
                    f"{buffer_span_minutes:.1f}m of history (>= {window}m, so it COULD fill the window) but "
                    f"the state only absorbed {observed:.1f}m — data present in the buffer but not absorbed "
                    f"(schema/shape mismatch or dropped minutes), not a legitimately-short history."
                )


class IncrementalEngine:
    """The live incremental execution path for the declarative reduction groups. Holds a per-shard
    ``WindowedSumState`` over the union of all groups' value columns (built by ``build_plan`` — the SAME
    columns the batch sums), and at each minute derives ONLY the new minute's values, folds them in, and
    assembles features from the running sums via ``assemble_from_long`` (the SAME core as the batch). So the
    feature logic is identical to the batch/backfill paths; only the source of the sums differs.

    V2 slice-derive: the new minute's short-lag value columns are derived over each symbol's last ``max_lag+1``
    rows (a per-symbol row tail — positionally exact for sparse symbols, not a fixed minute window); the
    long-history regressor columns (a frame-relative OLS time axis, a cumulative OBV) are
    declared via ``stateful_regressors()`` and maintained as running per-symbol engine state. The produced
    value matrix is identical to the whole-buffer derive, so parity holds (guarded by
    tests/test_fp_incremental_features.py).

    Usage: ``seed(buffer_frame)`` once (replays the buffer to establish state + symbols), then ``step(frame)``
    each minute (``frame`` = the trailing buffer including the new latest minute) -> {group: feature_frame}.
    """

    DERIVE_SLICE = 6  # legacy minute-window depth (>= max_lag); the live slice tails by ROW (see _matrix_at). Used by the rust-vs-polars unit tests and the dense-feed sim slot count.

    def __init__(
        self, groups: list[ReductionGroup], *, rust_slice: bool = True, assert_ready_on_seed: bool = False
    ) -> None:
        self.rust_slice = rust_slice
        # When True, the FIRST seed runs the UNIVERSAL ``assert_ready`` (three-way FULL / legit-not-yet-full /
        # FAILED) + internal invariants against the seed buffer — catching a present-but-not-absorbed fill
        # (e.g. the warm-start ShapeError) loudly at init. It is the SAME populated property maintained by the
        # shared fold, evaluated at one convenient call site; cleared after that first seed so later re-seeds
        # (a genuinely-new ticker / daily resync) are normal operation. The invariant itself holds at all
        # times regardless of this flag.
        self.assert_ready_on_seed = assert_ready_on_seed
        self.groups = [g for g in groups if isinstance(g, ReductionGroup)]
        (
            self.derived,
            self.extra,
            self.value_cols,
            self.plan,
            self.reg_plan,
            self.windows,
            self.centered,
        ) = build_plan(self.groups)
        self.col_index = {col: i for i, col in enumerate(self.value_cols)}
        # Flattened metadata for the Rust assemble kernel (FP_RUST_ASSEMBLE) — built ONCE here, reused each minute.
        self.asm_plan = build_assemble_plan(
            self.groups, self.windows, self.col_index, self.plan, self.reg_plan, self.centered
        )
        self.reduce_input = self.groups[0].reduce_input if self.groups else "minute_agg"
        input_cols: list[str] = []
        for group in self.groups:
            for col in group._input_columns():
                if col not in input_cols:
                    input_cols.append(col)
        self.input_cols = input_cols

        # Split the union derive into SLICE-SAFE exprs (the default — derived over a small slice) and the
        # STATEFUL regressions (their 6 OLS paired columns are rebuilt from running per-symbol x/y state).
        self.stateful_specs = self._collect_stateful_specs()  # ns -> {slot: StatefulRegressor}
        self.stateful_ns = set(self.stateful_specs)
        self.reg_x_expr, self.reg_y_expr = self._collect_regression_exprs()  # ns -> base x/y expr
        # ns -> the per-symbol-constant y-centering anchor expr ACTIVE under FP_RUST_REDUCE (empty when off).
        # The y (close) of an anchored regression is centered on this constant BEFORE the OLS paired columns
        # are formed, so the running-sum y-side denom is conditioned IDENTICALLY to the batch path (the batch
        # applies the same centering in ``_ols_derived`` via ``build_plan``). Value-identical (OLS is
        # translation-invariant in y); the centering is the parity-critical contract — the anchor is the SAME
        # per-symbol constant both paths read off the input frame.
        self.reg_y_anchor = self._collect_y_anchor_exprs()  # ns -> anchor pl.Expr
        self.safe_derived = self._slice_safe_derived()
        # value columns that slice-derive produces directly (everything except stateful regressions' paired cols)
        stateful_cols = {f"__rd_{ns}_{key}" for ns in self.stateful_ns for key in _OLS_KEYS}
        self.safe_value_cols = [col for col in self.value_cols if col not in stateful_cols]

        # The extra per-minute columns the stateful regressions need, derived in the SAME slice pass as the
        # safe value cols (one derive, one row-T read — no per-regressor re-sort): cumulative increments
        # (__inc_<ns>) and the base values of a stateful regression's NON-stateful partner slot (__reg{x,y}_<ns>).
        self.stateful_aux: list[pl.Expr] = []
        self.inc_col: dict[str, str] = {}  # ns -> increment col name
        self.bcast_col: dict[str, str] = (
            {}
        )  # ns -> broadcast-value col name (the index ticker's per-minute value)
        self.bcast_symbol: dict[str, str] = (
            {}
        )  # ns -> the index ticker whose row carries the broadcast value
        self.regx_col: dict[str, str] = {}  # ns -> non-stateful x base col name
        self.regy_col: dict[str, str] = {}
        for ns, slots in self.stateful_specs.items():
            for slot, slot_spec in slots.items():
                if slot_spec.kind == "cumulative":
                    assert slot_spec.increment is not None
                    name = f"__inc_{ns}"
                    self.stateful_aux.append(slot_spec.increment.alias(name))
                    self.inc_col[ns] = name
                elif slot_spec.kind == "broadcast":
                    assert slot_spec.increment is not None and slot_spec.broadcast_symbol is not None
                    name = f"__bcast_{ns}"
                    self.stateful_aux.append(slot_spec.increment.alias(name))
                    self.bcast_col[ns] = name
                    self.bcast_symbol[ns] = slot_spec.broadcast_symbol
            if "x" not in slots:
                name = f"__regx_{ns}"
                self.stateful_aux.append(self.reg_x_expr[ns].alias(name))
                self.regx_col[ns] = name
            if "y" not in slots:
                name = f"__regy_{ns}"
                anchor = self.reg_y_anchor.get(ns)
                y_expr = self.reg_y_expr[ns] if anchor is None else (self.reg_y_expr[ns] - anchor)
                self.stateful_aux.append(y_expr.alias(name))
                self.regy_col[ns] = name
        self.aux_cols = [expr.meta.output_name() for expr in self.stateful_aux]

        # Rust slice-derive path: the only per-symbol op in the safe/aux/extra derive is
        # ``Column(c).shift(k).over("symbol")``, so resolve those lags in Rust (one ordered pass) and rewrite
        # the exprs to read plain ``__lag{k}_{c}`` columns — the derive then runs GLOBALLY (no Polars partition,
        # the ~53ms cost). lag_columns: col -> [lags needed]; rewritten exprs evaluate on the kernel's lag frame.
        all_derive_exprs = [*self.safe_derived, *self.stateful_aux, *self.extra]
        lags, self.max_lag = lag_specs(all_derive_exprs)
        self.lag_columns: dict[str, list[int]] = {}
        for column, lag in sorted(lags):
            self.lag_columns.setdefault(column, []).append(lag)
        self.rust_safe_derived = [rewrite_global(expr) for expr in self.safe_derived]
        self.rust_stateful_aux = [rewrite_global(expr) for expr in self.stateful_aux]
        self.rust_extra = [rewrite_global(expr) for expr in self.extra]

        # The (b, x, y, xy, xx) running-sum column indices of every OLS regression whose x slot is a "time"
        # axis. The time origin (``ref_epoch``) is advanced each minute to keep these x small, and the running
        # sums are rebased in lockstep (``WindowedSumState.rebase_time_axis``) so the variance term stays well
        # conditioned — closing the near-perfect-fit time-OLS breach (price_r2, clean_momentum) at source.
        self.time_ols_cols: list[tuple[int, int, int, int, int]] = []
        for ns, slots in self.stateful_specs.items():
            if slots.get("x") and slots["x"].kind == "time":
                self.time_ols_cols.append(tuple(self.col_index[f"__rd_{ns}_{key}"] for key in ("b", "x", "y", "xy", "xx")))  # type: ignore[arg-type]

        self.symbols: list[str] | None = None
        self.state: WindowedSumState | None = None
        self.ref_epoch: int | None = None  # rolling origin for "time" regressors (OLS is origin-invariant)
        self.obv_running: dict[str, np.ndarray] = (
            {}
        )  # ns -> (n_symbols,) running cumulative for "cumulative" slots
        # FP_POINT_RING: carry the groups' point/lag columns in an O(1) per-symbol positional ring instead of
        # the per-minute whole-buffer ``resolve_points`` pass (phase_profile: ~6ms framework overhead). The
        # specs (the (source, lag) columns to carry) are pure plan metadata, built once. ``point_ring`` is the
        # held state, seeded alongside ``state`` and folded each minute; OFF by default (resolve_points path).
        self.point_specs = point_specs(self.groups) if os.environ.get("FP_POINT_RING") == "1" else []
        # Arm the ring only when the groups actually declare points to carry — an empty-points set has nothing
        # to ring, and ``_latest_frame`` keeps the resolve_points path (which returns an empty __pt_ frame).
        self._use_point_ring = bool(self.point_specs)
        self.point_ring: PointRing | None = None
        # FP_POINT_RING_PARITY: MONITORING-ONLY self-check (NOT a gate), default OFF, mirrors
        # FP_INCREMENTAL_PARITY. When the ring is armed AND this is set, also run the whole-buffer
        # ``resolve_points`` each minute and record the ring-vs-truth __pt_ divergence to Prometheus
        # (``feature_point_ring_breach_total`` + a max-abs-diff gauge). The SERVED output is still the ring's —
        # this never alters values; the FP_POINT_RING=0 revert on a sustained breach is an ops action off the
        # metric, not something this does to the emit path. Only meaningful when the ring is actually armed.
        self._point_ring_parity = self._use_point_ring and os.environ.get("FP_POINT_RING_PARITY") == "1"

    def _collect_stateful_specs(self) -> dict[str, dict[str, StatefulRegressor]]:
        specs: dict[str, dict[str, StatefulRegressor]] = {}
        for gi, group in enumerate(self.groups):
            declared = group.stateful_regressors()
            for reg_name, slots in declared.items():
                ns = f"{gi}_{reg_name}"
                specs[ns] = {spec.slot: spec for spec in slots}
        return specs

    def _collect_regression_exprs(self) -> tuple[dict[str, pl.Expr], dict[str, pl.Expr]]:
        x_exprs: dict[str, pl.Expr] = {}
        y_exprs: dict[str, pl.Expr] = {}
        for gi, group in enumerate(self.groups):
            for reg_name, (x_expr, y_expr, _, _) in group.regressions().items():
                ns = f"{gi}_{reg_name}"
                x_exprs[ns], y_exprs[ns] = x_expr, y_expr
        return x_exprs, y_exprs

    def _collect_y_anchor_exprs(self) -> dict[str, pl.Expr]:
        """The per-namespace y-centering anchor exprs ACTIVE under ``FP_RUST_REDUCE`` (empty when off — each
        group's ``_y_anchor_exprs`` already returns {} unless the flag is on). Namespaced by ``<gi>_<reg>`` to
        match the OLS paired-column namespace, so the incremental fold centers ``y`` on the SAME per-symbol
        constant the batch ``build_plan`` does."""
        anchors: dict[str, pl.Expr] = {}
        for gi, group in enumerate(self.groups):
            for reg_name, anchor_expr in group._y_anchor_exprs().items():
                anchors[f"{gi}_{reg_name}"] = anchor_expr
        return anchors

    def _slice_safe_derived(self) -> list[pl.Expr]:
        """The union derive exprs MINUS the OLS paired columns of stateful regressions (those are rebuilt from
        running state). A stateful regression's paired columns are named ``__rd_<ns>_<key>``; everything else
        (reduced bases, non-stateful regressions' paired columns) is short-lag and slice-safe."""
        skip = {f"__rd_{ns}_{key}" for ns in self.stateful_ns for key in _OLS_KEYS}
        return [expr for expr in self.derived if expr.meta.output_name() not in skip]

    def _derived_row(self, frame: pl.DataFrame, minute: object) -> pl.DataFrame:
        """ONE lazy slice pass: derive the safe value cols + presence/square + the stateful regressions' aux
        cols (cumulative increments, non-stateful partner-slot bases), then return the single row per symbol at
        ``minute`` (sorted by symbol). Lazy so polars fuses the 40+ windowed exprs into one optimized plan.
        """
        return (
            frame.lazy()
            .select(self.input_cols)
            .sort(["symbol", "minute"])
            .with_columns([*self.safe_derived, *self.stateful_aux])
            .with_columns(self.extra)
            .filter(pl.col("minute") == minute)
            .sort("symbol")
            .collect()
        )

    def _derived_row_rust(self, frame: pl.DataFrame, minute: object) -> pl.DataFrame:
        """Rust slice-derive: resolve the per-symbol ``shift(k).over("symbol")`` lags in one Rust pass, then
        derive the SAME safe value cols + presence/square + stateful aux cols GLOBALLY (no Polars per-symbol
        partition — the ~53ms cost). ``rust_slice_derive`` returns one row per symbol (the latest minute) plus
        ``__lag{k}_{c}`` columns (missing prior bar -> ``null``); the rewritten exprs read those lag columns
        instead of ``shift().over()``, so the result is the SAME single-row-per-symbol derive as
        ``_derived_row`` (guarded cell-for-cell by tests/test_fp_slice_derive_rust.py)."""
        lag_row = rust_slice_derive(frame, self.input_cols, self.lag_columns, minute)
        return lag_row.with_columns([*self.rust_safe_derived, *self.rust_stateful_aux]).with_columns(
            self.rust_extra
        )

    def _stateful_matrix(
        self, row: pl.DataFrame, minute: object, present: np.ndarray
    ) -> dict[int, np.ndarray]:
        """Rebuild the 6 OLS paired columns (b, x, y, xy, xx, yy) for every stateful regression at ``minute``
        from running x/y state (sourced from the already-derived ``row``) — pairing under nulls exactly as
        ``_ols_derived`` does. Returns {value_col_index: (n_symbols,) column}. Advances the running cumulatives.

        ``present`` is the (n_symbols,) bool mask of which index symbols delivered a bar this minute. A symbol
        absent this minute has NO row in the batch (so it contributes nothing to the OLS sums at ``minute``);
        we enforce that here by forcing ``b=0`` for absent symbols (``both &= present``). This matters for the
        cumulative (OBV) slot whose ``y`` is a running float that stays finite even when the symbol is absent —
        without the mask its pair would be wrongly counted. The cumulative still does NOT advance for absent
        symbols (their increment is ``fill_null(0)``), matching the batch cumsum over present rows."""
        out: dict[int, np.ndarray] = {}
        n_sym = len(self.symbols or [])
        assert self.ref_epoch is not None
        minute_epoch = int(minute.timestamp())  # type: ignore[attr-defined]
        time_x = np.full(n_sym, (minute_epoch - self.ref_epoch) / 60.0, dtype=np.float64)
        for ns, slots in self.stateful_specs.items():
            if slots.get("x") and slots["x"].kind == "broadcast":
                x = self._broadcast_value(row, ns, n_sym)
            elif slots.get("x") and slots["x"].kind == "time":
                x = time_x
            else:
                x = self._aux_value(row, self.regx_col[ns])
            if "y" in slots and slots["y"].kind == "cumulative":
                inc = row.select(self.inc_col[ns]).fill_null(0.0).to_numpy().reshape(-1)
                running = self.obv_running.setdefault(ns, np.zeros(n_sym, dtype=np.float64))
                running += inc
                y = running.copy()
            elif "y" in slots and slots["y"].kind == "time":
                y = time_x
            elif "y" in slots and slots["y"].kind == "broadcast":
                y = self._broadcast_value(row, ns, n_sym)
            else:
                y = self._aux_value(row, self.regy_col[ns])
            both = np.isfinite(x) & np.isfinite(y) & present
            x_paired = np.where(both, x, 0.0)
            y_paired = np.where(both, y, 0.0)
            paired = {
                "b": both.astype(np.float64),
                "x": x_paired,
                "y": y_paired,
                "xy": x_paired * y_paired,
                "xx": x_paired * x_paired,
                "yy": y_paired * y_paired,
            }
            for key, column in paired.items():
                out[self.col_index[f"__rd_{ns}_{key}"]] = column
        return out

    def _aux_value(self, row: pl.DataFrame, col: str) -> np.ndarray:
        """A non-stateful regressor base value at the row, kept as NaN where null so pairing drops it."""
        return row.select(col).to_numpy().reshape(-1).astype(np.float64)

    def _broadcast_value(self, row: pl.DataFrame, ns: str, n_sym: int) -> np.ndarray:
        """The cross-symbol broadcast regressor for ``ns``: read the index ticker's per-minute value (the
        ``__bcast_<ns>`` aux column at the index symbol's row) and broadcast it to every symbol — the SAME
        minute-broadcast the batch path does by a minute-join on the index series. NaN-everywhere when the
        index ticker is absent this minute or its value is null (so the regression pairs nothing, exactly as
        the batch left-join would leave the broadcast column null and ``_ols_derived`` drop the pair)."""
        index_symbol = self.bcast_symbol[ns]
        symbols = self.symbols or []
        if index_symbol not in symbols:
            return np.full(n_sym, np.nan, dtype=np.float64)
        position = symbols.index(index_symbol)  # row is symbol-sorted == self.symbols order
        value = float(row.select(self.bcast_col[ns]).to_numpy().reshape(-1)[position])
        return np.full(n_sym, value, dtype=np.float64)

    def _matrix_at(self, frame: pl.DataFrame, minute: object, *, slice_derive: bool) -> np.ndarray:
        """The (n_symbols, n_value_cols) value matrix for ``minute``, symbol-aligned to ``self.symbols`` (nulls
        -> 0, matching the kernel). ``slice_derive`` derives short-lag columns over a small trailing slice and
        rebuilds stateful regression columns from running state; otherwise (seed) it derives over ``frame`` as
        given. Asserts a fixed symbol set (V1)."""
        # FP_STATE_SPINE (step 1, price_volume only): the polars-free numpy derive. Only on the LIVE per-minute
        # step path (``minute`` IS the buffer's latest) — the seed/resync path (folding a HISTORICAL minute over
        # a multi-minute buffer) keeps the proven polars slice-derive, since that is a once-per-session warm-up,
        # not the per-minute hot path this demonstration optimizes. ``spine_active`` is the conservative gate
        # (flag on AND the engine's groups are exactly {price_volume}); off → today's exact path, byte-identical.
        if (
            slice_derive
            and spine_active({g.name for g in self.groups})
            and self.symbols is not None
            and minute == frame["minute"].max()
        ):
            return self._matrix_at_spine(frame, minute)
        source = frame
        if slice_derive:
            # Positional lags (``shift(k).over("symbol")``) need each present symbol's last ``max_lag+1`` ROWS
            # AT OR BEFORE ``minute``, not a fixed minute window. A sparse symbol's prior bar can be arbitrarily
            # far back in time, and backfill's shift is POSITIONAL (the k-th prior ROW, not the bar k minutes
            # ago); a minute-window slice would miss it and slice-derive a wrong null lag where backfill returns
            # a real value. Tailing by ROW per symbol (over rows ``<= minute``) reaches each symbol's actual
            # prior bars regardless of gaps AND ends each symbol's tail at its ``minute`` row, so the slice
            # derive is cell-for-cell identical to the whole-buffer derive AT ``minute`` for dense AND sparse
            # symbols (this resolves the OPEN PARITY CONSTRAINT). The ``<= minute`` cut is a no-op when ``minute``
            # is the buffer's latest (the live ``step``), and the correctness fix when the SEED folds a HISTORICAL
            # minute over a multi-minute buffer: without it the tail would END at a symbol's FUTURE bar, so the
            # rust lag kernel would join that future row's lag onto the earlier ``minute`` row — wrongly making a
            # first-appearance return's prior-close lag non-null and double-counting the OLS pairing (b) on the
            # sparse first-bar window (the FP_INCREMENTAL null/non-null A/B breach on pv_correlation). ``minute``-
            # sort so each symbol's ``tail`` is its latest in-window rows.
            source = (
                frame.filter(pl.col("minute") <= minute)
                .sort("minute")
                .group_by("symbol", maintain_order=True)
                .tail(self.max_lag + 1)
            )
        row = (
            self._derived_row_rust(source, minute) if self.rust_slice else self._derived_row(source, minute)
        )
        n_sym = len(self.symbols or [])
        # Live capture delivers only the minute's ACTIVE symbols — a fluctuating SUBSET of the fixed session
        # index. Align the present rows to the full index: absent symbols contribute 0 to every windowed sum
        # (exactly as a missing bar does in the batch — no row, no contribution) and are masked out of the OLS
        # pairing (present=False -> b=0). A symbol present but OUTSIDE the index is genuinely new -> re-seed.
        row, present = self._reindex_to_index(row)
        matrix = np.zeros((n_sym, len(self.value_cols)), dtype=np.float64)
        safe = row.select(self.safe_value_cols).fill_null(0.0).to_numpy()
        for safe_i, col in enumerate(self.safe_value_cols):
            matrix[:, self.col_index[col]] = safe[:, safe_i]
        for col_index, column in self._stateful_matrix(row, minute, present).items():
            matrix[:, col_index] = column
        return matrix

    def _matrix_at_spine(self, frame: pl.DataFrame, minute: object) -> np.ndarray:
        """FP_STATE_SPINE: the (n_symbols, n_value_cols) value matrix for ``price_volume`` at the LIVE latest
        ``minute``, built entirely in numpy — no per-minute polars derive, no ``_reindex`` join.

        The frame arrives pre-sorted by ``[symbol, minute]`` (the caller sorts once, off the hot path), so each
        symbol's rows are contiguous and minute-ordered. We read the per-symbol LATEST row (its block's last) and
        PRIOR close (the block's second-to-last — ``close.shift(1).over("symbol")`` positionally) as numpy arrays,
        aligned to the fixed session index, then:
          * ``price_volume_safe_cols`` derives the safe value columns (vol/cv/mfv/up/dn + presence + the pv-corr
            OLS pairs with the y=vol−anchor centering) — cell-for-cell the safe half of ``_derived_row``;
          * the OBV-slope OLS pairs are rebuilt from the engine's EXISTING carried ``obv_running`` + rolled
            ``ref_epoch`` (the same already-numpy, already-#451-proven path ``_stateful_matrix`` runs), advanced by
            ``obv_increment`` — so only the increment's SOURCE moves to numpy; the carried OBV math is unchanged.
        """
        symbols = self.symbols or []
        n_sym = len(symbols)
        sym_pos = {sym: i for i, sym in enumerate(symbols)}

        # Pull the needed columns once (no derive, no sort — the frame is already [symbol, minute]-sorted).
        sub = frame.select(["symbol", "minute", "close", "high", "low", "volume", "__anchor_volume"])
        sym_arr = sub["symbol"].to_numpy()
        close_arr = sub["close"].to_numpy().astype(np.float64)
        high_arr = sub["high"].to_numpy().astype(np.float64)
        low_arr = sub["low"].to_numpy().astype(np.float64)
        vol_arr = sub["volume"].to_numpy().astype(np.float64)
        anchor_arr = sub["__anchor_volume"].to_numpy().astype(np.float64)

        # Per-symbol contiguous block boundaries in the pre-sorted frame: starts[i] is the first row of symbol i's
        # block, so its LATEST row is the row before the next block's start (the block's last), and its PRIOR row
        # is one before that (only if the block has >= 2 rows — else no prior bar, lag is null).
        uniq, starts = np.unique(sym_arr, return_index=True)
        order = np.argsort(starts)
        uniq = uniq[order]
        starts = starts[order]
        ends = np.append(starts[1:], len(sym_arr))  # one past each block's last row

        close = np.full(n_sym, np.nan)
        high = np.full(n_sym, np.nan)
        low = np.full(n_sym, np.nan)
        volume = np.full(n_sym, np.nan)
        anchor_volume = np.full(n_sym, np.nan)
        prior_close = np.full(n_sym, np.nan)
        present = np.zeros(n_sym, dtype=bool)
        for block_i, sym in enumerate(uniq):
            pos = sym_pos.get(sym)
            if pos is None:
                raise SymbolSetExpanded([str(sym)])  # a genuinely new ticker — caller re-seeds
            last = ends[block_i] - 1
            close[pos] = close_arr[last]
            high[pos] = high_arr[last]
            low[pos] = low_arr[last]
            volume[pos] = vol_arr[last]
            anchor_volume[pos] = anchor_arr[last]
            present[pos] = True
            if last - 1 >= starts[block_i]:  # the block has a prior row -> positional shift(1)
                prior_close[pos] = close_arr[last - 1]

        y_anchored = bool(self.reg_y_anchor)  # FP_RUST_REDUCE on -> pv y is centered on the volume anchor
        safe = price_volume_safe_cols(
            close, high, low, volume, anchor_volume, prior_close, present, y_anchored=y_anchored
        )
        matrix = np.zeros((n_sym, len(self.value_cols)), dtype=np.float64)
        for col, column in safe.items():
            matrix[:, self.col_index[col]] = np.nan_to_num(column, nan=0.0)

        # OBV-slope OLS pairs from the carried state (== _stateful_matrix's cumulative+time block, numpy already).
        assert self.ref_epoch is not None
        minute_epoch = int(minute.timestamp())  # type: ignore[attr-defined]
        time_x = np.full(n_sym, (minute_epoch - self.ref_epoch) / 60.0, dtype=np.float64)
        inc = obv_increment(close, prior_close, volume, present)
        for ns in self.stateful_ns:
            running = self.obv_running.setdefault(ns, np.zeros(n_sym, dtype=np.float64))
            running += inc
            y = running.copy()
            both = np.isfinite(time_x) & np.isfinite(y) & present
            x_paired = np.where(both, time_x, 0.0)
            y_paired = np.where(both, y, 0.0)
            pairs = {
                "b": both.astype(np.float64),
                "x": x_paired,
                "y": y_paired,
                "xy": x_paired * y_paired,
                "xx": x_paired * x_paired,
                "yy": y_paired * y_paired,
            }
            for key, column in pairs.items():
                matrix[:, self.col_index[f"__rd_{ns}_{key}"]] = column
        return matrix

    def _reindex_to_index(self, row: pl.DataFrame) -> tuple[pl.DataFrame, np.ndarray]:
        """Align a present-symbols-only derived ``row`` to the fixed session index (``self.symbols``, sorted).
        Returns (full-height row in index order with nulls for absent symbols, present-mask). Fast-paths the
        fully-present case (every index symbol delivered) to avoid a join. Raises ``SymbolSetExpanded`` if the
        minute carries a symbol outside the index — the caller re-seeds (the parity-safe resync path)."""
        symbols = self.symbols or []
        if row.height == len(
            symbols
        ):  # fully-present (the fixed-set case): row is already the index, sorted
            ordered = row.sort("symbol")
            if ordered["symbol"].to_list() == symbols:
                return ordered, np.ones(len(symbols), dtype=bool)
        extra = set(row["symbol"].to_list()) - set(symbols)
        if extra:
            raise SymbolSetExpanded(sorted(extra)[:5])
        index_df = pl.DataFrame({"symbol": symbols}, schema={"symbol": row.schema["symbol"]})
        aligned = index_df.join(row.with_columns(pl.lit(True).alias("__present")), on="symbol", how="left")
        present = aligned["__present"].fill_null(False).to_numpy().astype(bool)
        return aligned.drop("__present"), present

    def _seed_stateful(self, buffer_frame: pl.DataFrame) -> None:
        """Initialise the running per-symbol state the stateful regressors need before folding the buffer:
        the time origin (the buffer's earliest minute) and the OBV running totals reset to zero
        (re-accumulated as the seed folds each minute). The origin then ROLLS forward each minute
        (``_roll_time_origin``) to keep the time-OLS x bounded and well conditioned."""
        self.ref_epoch = int(buffer_frame.select(pl.col("minute").dt.epoch("s").min()).item())
        self.obv_running = {}

    def _roll_time_origin(self, minute_epoch: int) -> None:
        """Advance the time-regression origin so the minute about to fold maps to a SMALL x, and rebase the
        running sums to the new origin in lockstep. Without this the ``time`` x grows unbounded over a session
        (origin fixed at seed), so ``b·Σxx − (Σx)²`` becomes a difference of large near-equal sums and a
        near-perfect fit's r2/slope round differently from the batch fresh sums (the price_r2 / clean_momentum
        incremental breach). Pinning the latest x to ``_TIME_ORIGIN_LAG`` keeps every in-window x O(1) (small,
        like the batch's per-frame centering), so the cancellation stays small; OLS is origin-invariant, so the
        features are unchanged. No-op when the engine has no time regressors or before any state exists."""
        if not self.time_ols_cols or self.state is None or self.ref_epoch is None:
            return
        # Pin the origin a fixed small offset behind the incoming minute so its x is O(1) and every in-window
        # x stays in ``[_TIME_ORIGIN_LAG - w, _TIME_ORIGIN_LAG]`` — small for every window, so the OLS variance
        # term never cancels large sums. (Anchoring to ``max(windows)`` would still leave x ~ the longest
        # window, which is large enough to breach near a perfect fit; a small fixed lag keeps it bounded.)
        new_ref = minute_epoch - _TIME_ORIGIN_LAG * 60
        delta_minutes = (new_ref - self.ref_epoch) / 60.0
        if delta_minutes <= 0.0:  # only ever advance the origin forward (never re-grow x)
            return
        self.state.rebase_time_axis(delta_minutes, self.time_ols_cols)
        self.ref_epoch = new_ref

    def seed(
        self, buffer_frame: pl.DataFrame, symbols: list[str] | None = None, *, slice_derive: bool = True
    ) -> None:
        """Establish the symbol index + fixed origins and fold every buffered minute into fresh state (== the
        batch recompute over the buffer; also the daily-resync / crash-recovery entry point). Folds minute by
        minute through the SAME slice-derive + stateful path used live, so the running OBV/time state is built
        exactly as it will be advanced — no separate seeding code to drift from the live path.

        ``symbols`` pins the index to a FIXED session set (e.g. the shard's full universe) — a stable superset
        of any single minute's active symbols, so intraday membership churn folds in as absent (zero) rows
        instead of forcing a re-seed. When None, the index is the symbols seen in ``buffer_frame`` (the prior
        behaviour). ``slice_derive`` controls whether per-minute value columns are derived over a per-symbol
        last-``max_lag+1``-rows tail (fast, and parity-safe for sparse symbols — positional lags reach each
        symbol's actual prior bars) or the whole buffer (gap-safe; identical result, just more rows derived).
        """
        index = symbols if symbols is not None else buffer_frame["symbol"].unique().to_list()
        self.symbols = sorted(index)
        self.state = WindowedSumState(self.symbols, self.windows, len(self.value_cols))
        self._seed_stateful(buffer_frame)
        if self._use_point_ring:
            # Seed the point ring by folding every buffered minute (== resolve_points over the buffer at each
            # minute, carried), so the live ring state == the backfill points the instant seed returns.
            self.point_ring = PointRing(self.symbols, self.point_specs)
        for minute in sorted(buffer_frame["minute"].unique()):
            minute_epoch = int(minute.timestamp())
            self._roll_time_origin(minute_epoch)
            self.state.update(minute_epoch, self._matrix_at(buffer_frame, minute, slice_derive=slice_derive))
            self.state.trim()
            if self.point_ring is not None:
                self.point_ring.fold(buffer_frame.filter(pl.col("minute") == minute))
        # The internal invariants are cheap (O(windows)) and run by DEFAULT at init — a corrupted fold/expire
        # surfaces immediately, on every seed, not only on the warm-start path. The deep O(buffer) sum-vs-
        # buffer reconstruction is opt-in (FP_INCREMENT_DEEP_CHECK=1) so steady-state throughput is unaffected.
        self.state.check_invariants(deep=os.environ.get("FP_INCREMENT_DEEP_CHECK") == "1")
        if self.assert_ready_on_seed:
            self.assert_ready(buffer_frame)
            self.assert_ready_on_seed = False  # one call site; the populated property holds at all times

    def frame_span_minutes(self, frame: pl.DataFrame) -> float:
        """The span of distinct minutes ``frame`` carries (latest − earliest), in minutes — how much history
        the supplied buffer made available to fill the windows. Source-agnostic: the seed frame at init, or
        the trailing frame in steady state. Compared against ``observed_span_minutes`` to tell a
        legitimately-short buffer (no raise) from a present-but-not-absorbed fill (raise)."""
        if frame.is_empty():
            return 0.0
        minutes = frame.select(
            pl.col("minute").dt.epoch("s").min().alias("lo"),
            pl.col("minute").dt.epoch("s").max().alias("hi"),
        )
        return float((minutes["hi"][0] - minutes["lo"][0]) / 60.0)

    def assert_ready(self, frame: pl.DataFrame) -> None:
        """Run the UNIVERSAL readiness check against ``frame``: every window the engine declares is either
        ``populated`` or legitimately not-yet-full given the history ``frame`` carried; a present-but-not-
        absorbed window raises ``IncrementUnderfilled``, and an internal-bookkeeping contradiction raises
        ``IncrementInvariantError`` (``WindowedSumState.assert_ready`` → ``check_invariants`` + the three-way
        fill check). Source-agnostic and valid at ANY time — at init after a seed, or in steady state against
        the live trailing frame. No-op (vacuous) when the engine declares no windows or has not been seeded.
        """
        if self.state is None or not self.windows:
            return
        self.state.assert_ready(self.frame_span_minutes(frame))

    def step(self, frame: pl.DataFrame, *, slice_derive: bool = True) -> dict[str, pl.DataFrame]:
        """Fold the new latest minute and assemble features from the running sums. ``frame`` is the trailing
        buffer including the new minute. Seeds lazily on first call. A ``SymbolSetExpanded`` (a genuinely new
        ticker appeared) triggers a re-seed from ``frame`` — the parity-safe resync — and a retry, so live
        membership growth never breaks the run. ``slice_derive=False`` derives over the whole buffer (gap-safe,
        O(buffer)); the default fast slice tails each symbol's last ``max_lag+1`` ROWS — positionally exact for
        sparse symbols, so it is cell-for-cell identical to the whole-buffer derive (the OPEN PARITY CONSTRAINT,
        resolved)."""
        latest = frame["minute"].max()
        # FP_STATE_SPINE (price_volume): assemble through the polars-free numpy emit too, not just the numpy
        # _matrix_at derive. Without this the step still runs assemble_from_long's per-stat pivot+join (the
        # ASSEMBLE half of the tax) — so the whole-step claim is "matrix_at deleted" only. Routing the spine
        # step through emit_numpy (the proven byte-identical read surface, test_fp_incremental_emit / #44) makes
        # the per-minute COMPUTE fully polars-free (collect→0, the keystone's ~2ms), which is the demonstration
        # Ben asked for. step_numpy is self-contained (its own lazy seed + _fold_latest, so the numpy
        # _matrix_at_spine derive runs); emit_numpy uses the IDENTICAL canonical/OLS algebra as
        # assemble_from_long — value-true.
        if spine_active({g.name for g in self.groups}):
            return self.step_numpy(frame)
        self._fold_latest(frame, latest, slice_derive=slice_derive)
        long = self._running_long()
        latest_frame = self._latest_frame(frame, latest)
        return assemble_from_long(
            self.groups, long, latest_frame, latest, self.plan, self.reg_plan, self.centered
        )

    def _fold_latest(self, frame: pl.DataFrame, latest: object, *, slice_derive: bool) -> None:
        """Roll the time origin, fold the new latest minute into the running sums, and expire what left every
        window. Seeds lazily on the first call; a ``SymbolSetExpanded`` (a genuinely new ticker) triggers a
        re-seed from ``frame`` (the parity-safe resync). Shared by every ``step*`` emit variant."""
        if self.state is None:
            self.seed(frame, slice_derive=slice_derive)
            return
        try:
            minute_epoch = int(latest.timestamp())  # type: ignore[attr-defined]
            self._roll_time_origin(minute_epoch)
            self.state.update(minute_epoch, self._matrix_at(frame, latest, slice_derive=slice_derive))
            self.state.trim()
            if self.point_ring is not None:
                self.point_ring.fold(frame.filter(pl.col("minute") == latest))
        except SymbolSetExpanded:
            self.seed(frame, slice_derive=slice_derive)  # rebuild the index to include the new ticker(s)

    def _latest_frame(self, frame: pl.DataFrame, latest: object) -> pl.DataFrame:
        """The latest-minute ``__pt_<name>`` point frame ``assemble`` consumes — from the O(1) point ring when
        ``FP_POINT_RING`` is armed (the carried state was folded in lockstep with the running sums), else the
        per-minute whole-buffer ``resolve_points`` pass. Byte-identical (tests/test_fp_point_ring.py)."""
        if self.point_ring is not None:
            assert self.symbols is not None
            ring_frame = point_frame_from_ring(self.groups, self.point_ring, self.symbols, latest)
            if self._point_ring_parity:
                # MONITORING-ONLY shadow: compare the ring's __pt_ columns to the whole-buffer truth and record
                # the divergence. The served value is STILL ``ring_frame`` — this never alters it.
                _record_point_ring_parity(ring_frame, resolve_points(self.groups, frame, latest))
            return ring_frame
        return resolve_points(self.groups, frame, latest)  # points over the whole buffer (lag-safe)

    def step_numpy(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """NUMPY-EMIT alternative to ``step``: fold the new minute, then assemble features DIRECTLY from the
        running-sum numpy array via ``emit_numpy`` (no ``_running_long`` long-frame build, no polars pivot in
        assemble). Parity-true by construction — ``emit_numpy`` uses the IDENTICAL canonical/OLS algebra as the
        polars ``assemble_from_long``. Guarded against it cell-for-cell by tests/test_fp_incremental_emit.py.
        """
        latest = frame["minute"].max()
        self._fold_latest(frame, latest, slice_derive=True)
        assert self.state is not None
        latest_frame = self._latest_frame(frame, latest)
        return emit_numpy(
            self.groups,
            self.state.corrected(),
            self.symbols or [],
            self.windows,
            self.col_index,
            latest_frame,
            latest,
            self.plan,
            self.reg_plan,
            self.centered,
        )

    def step_rust(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """RUST-ASSEMBLE alternative to ``step_numpy``: fold the new minute, then assemble features from the
        running-sum array via ``emit_rust`` (the canonical/OLS columns built in ONE ``assemble_canonical`` Rust
        pass instead of per-column numpy). Parity-true by construction — the kernel mirrors ``_canonical_numpy``
        / ``_ols_stat_numpy`` cell-for-cell (NaN==null). Guarded against ``step_numpy``/``step`` and the batch by
        tests/test_fp_incremental_emit.py."""
        latest = frame["minute"].max()
        self._fold_latest(frame, latest, slice_derive=True)
        assert self.state is not None
        latest_frame = self._latest_frame(frame, latest)
        return emit_rust(
            self.groups, self.state.corrected(), self.symbols or [], self.asm_plan, latest_frame, latest
        )

    def step_rust_unified(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """UNIFIED single-pass twin of ``step_rust``: fold the new minute, then assemble EVERY reduction
        group's features in ONE shared wide-frame pass (``emit_rust_unified``) instead of one per-group
        polars frame-build + ``assemble`` each. Parity-true by construction — same kernel, same point exprs,
        same ``assemble`` expressions; only the polars pass count changes. Guarded == ``step_rust`` /
        ``step_numpy`` / ``step`` / batch by tests/test_fp_unified_emit.py."""
        latest = frame["minute"].max()
        self._fold_latest(frame, latest, slice_derive=True)
        assert self.state is not None
        latest_frame = self._latest_frame(frame, latest)
        return emit_rust_unified(
            self.groups, self.state.corrected(), self.symbols or [], self.asm_plan, latest_frame, latest
        )

    def _running_long(self) -> pl.DataFrame:
        """The running sums as a LONG (symbol, window, <value-col sum>) frame — the exact shape
        ``assemble_from_long`` expects, so the SAME assemble code runs as in the batch (NO pivot to build it).
        """
        assert self.state is not None
        running = self.state.corrected()  # (n_windows, n_symbols, n_cols), Neumaier-corrected
        n_win, n_sym, _ = running.shape
        data: dict[str, object] = {
            "symbol": (self.symbols or []) * n_win,
            "window": [w for w in self.windows for _ in range(n_sym)],
        }
        for col_index, col in enumerate(self.value_cols):
            data[col] = running[:, :, col_index].reshape(-1)
        return pl.DataFrame(data)
