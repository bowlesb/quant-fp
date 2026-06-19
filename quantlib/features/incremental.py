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

import numpy as np
import polars as pl

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
from quantlib.features.slice_derive import lag_specs, rewrite_global, rust_slice_derive

_OLS_KEYS = ("b", "x", "y", "xy", "xx", "yy")


class WarmStartUnderfilled(Exception):
    """Raised after a warm-start seed when a window that the seed buffer HAD enough history to fill did NOT
    end up populated — i.e. the seed data was present but the state failed to absorb it (the 7/13-col schema
    ShapeError, a sort/index mismatch, a silently-dropped slot). This is the "warm-start FAILED" arm of the
    three-way distinction: it is NOT raised for a window that is legitimately not-yet-full because the
    AVAILABLE seed history was itself shorter than the window (a newly-listed ticker / first day / genuine
    gap). Fail-fast per CLAUDE.md ("let errors raise / no lazy graceful degradation") so a partial warm-start
    is caught loudly at init instead of silently under-warming live emissions."""

# How far (minutes) behind the incoming minute to pin the rolling time-OLS origin each fold. Small and fixed
# so the time regressor's x stays O(1) for every window — keeping ``b·Σxx − (Σx)²`` well conditioned instead
# of a difference of large near-equal sums (the source of the near-perfect-fit time-OLS incremental breach).
_TIME_ORIGIN_LAG = 2


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
            self.running[wi] += values  # the new minute is in every window (epoch == T > T - w)
            cutoff = minute_epoch - w * 60
            oldest = self._oldest[wi]
            while oldest <= index and self._buf_epoch[oldest] <= cutoff:  # minute at/under T-w left the window
                self.running[wi] -= self._buf_vals[oldest]
                oldest += 1
            self._oldest[wi] = oldest

    def trim(self) -> None:
        """Drop buffered minutes older than the longest window (bound memory). Call after each update."""
        if not self._buf_epoch:
            return
        keep_from = min(self._oldest)
        if keep_from:
            self._buf_epoch = self._buf_epoch[keep_from:]
            self._buf_vals = self._buf_vals[keep_from:]
            self._oldest = [o - keep_from for o in self._oldest]

    def rebase_time_axis(self, delta_minutes: float, time_ols_cols: list[tuple[int, int, int, int, int]]) -> None:
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
        for matrix in (self.running.reshape(-1, self.running.shape[-1]), *self._buf_vals):
            for b_i, x_i, y_i, xy_i, xx_i in time_ols_cols:
                b_col = matrix[..., b_i]
                x_col = matrix[..., x_i]
                y_col = matrix[..., y_i]
                matrix[..., xx_i] += -2.0 * delta * x_col + delta * delta * b_col
                matrix[..., xy_i] += -delta * y_col
                matrix[..., x_i] = x_col - delta * b_col

    def sums(self, window: int) -> np.ndarray:
        """The current ``(n_symbols, n_cols)`` running sum for ``window`` (minutes)."""
        return self.running[self.windows.index(window)]

    def observed_span_minutes(self) -> float:
        """How many minutes of history the state has ABSORBED: latest folded minute − earliest folded minute,
        in minutes. ``0`` before any fold. Survives ``trim`` (tracked from ``_first/_last_epoch``, not the
        evicted buffer), so it reflects the full warm-started depth even after memory eviction."""
        if self._first_epoch is None or self._last_epoch is None:
            return 0.0
        return (self._last_epoch - self._first_epoch) / 60.0

    def populated(self, window: int) -> bool:
        """True when the absorbed history reaches a FULL ``window`` minutes behind the latest minute — i.e.
        the window holds its full required depth rather than being truncated by the seed's left edge. A
        window equal to or shorter than the observed span is full; a longer one is still warming."""
        return self.observed_span_minutes() >= float(window)

    def assert_populated(self, available_span_minutes: float) -> None:
        """Post-seed self-check (the warm-start ``populated`` assert). ``available_span_minutes`` is the span
        the SEED BUFFER actually carried (latest − earliest seed minute). For every window the engine
        declares, apply the three-way distinction:

          * FULL — ``observed_span >= window`` → the state absorbed its full depth → OK.
          * LEGITIMATELY not-yet-full — the seed buffer itself was shorter than the window
            (``available_span < window``: newly-listed ticker, first day, genuine short history) → the
            window is correctly not populated → OK (no raise; it emits partial/NaN as today).
          * warm-start FAILED — the seed buffer HAD ``>= window`` minutes but the state's observed span is
            short (data present, not absorbed: the ShapeError, a schema/index mismatch) → RAISE
            ``WarmStartUnderfilled``.

        The assert fires ONLY on the third arm: ``available_span >= window`` (the buffer could fill it) but
        ``observed_span < window`` (the state didn't)."""
        observed = self.observed_span_minutes()
        for window in self.windows:
            if available_span_minutes >= float(window) and observed < float(window):
                raise WarmStartUnderfilled(
                    f"warm-start failed to populate the {window}m window: seed buffer carried "
                    f"{available_span_minutes:.1f}m of history (>= {window}m, so it COULD fill the window) "
                    f"but the state only absorbed {observed:.1f}m — the seed data was present but not "
                    f"absorbed (schema/shape mismatch or dropped minutes), not a legitimately-short history."
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

    def __init__(self, groups: list[ReductionGroup], *, rust_slice: bool = True, warm_start_assert: bool = False) -> None:
        self.rust_slice = rust_slice
        # When True, the FIRST seed (the lazy warm-start seed, folding the rehydrated ring) asserts every
        # window is ``populated`` GIVEN that buffer's available history (``assert_populated``) — catching a
        # FAILED warm-start (data present but not absorbed) loudly. Cleared after that first seed, so later
        # re-seeds (a genuinely-new ticker / daily resync mid-session) are normal operation, not re-asserted.
        self.warm_start_assert = warm_start_assert
        self.groups = [g for g in groups if isinstance(g, ReductionGroup)]
        self.derived, self.extra, self.value_cols, self.plan, self.reg_plan, self.windows = build_plan(self.groups)
        self.col_index = {col: i for i, col in enumerate(self.value_cols)}
        # Flattened metadata for the Rust assemble kernel (FP_RUST_ASSEMBLE) — built ONCE here, reused each minute.
        self.asm_plan = build_assemble_plan(self.groups, self.windows, self.col_index, self.plan, self.reg_plan)
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
        self.safe_derived = self._slice_safe_derived()
        # value columns that slice-derive produces directly (everything except stateful regressions' paired cols)
        stateful_cols = {f"__rd_{ns}_{key}" for ns in self.stateful_ns for key in _OLS_KEYS}
        self.safe_value_cols = [col for col in self.value_cols if col not in stateful_cols]

        # The extra per-minute columns the stateful regressions need, derived in the SAME slice pass as the
        # safe value cols (one derive, one row-T read — no per-regressor re-sort): cumulative increments
        # (__inc_<ns>) and the base values of a stateful regression's NON-stateful partner slot (__reg{x,y}_<ns>).
        self.stateful_aux: list[pl.Expr] = []
        self.inc_col: dict[str, str] = {}  # ns -> increment col name
        self.bcast_col: dict[str, str] = {}  # ns -> broadcast-value col name (the index ticker's per-minute value)
        self.bcast_symbol: dict[str, str] = {}  # ns -> the index ticker whose row carries the broadcast value
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
                self.stateful_aux.append(self.reg_y_expr[ns].alias(name))
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
        self.obv_running: dict[str, np.ndarray] = {}  # ns -> (n_symbols,) running cumulative for "cumulative" slots

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

    def _slice_safe_derived(self) -> list[pl.Expr]:
        """The union derive exprs MINUS the OLS paired columns of stateful regressions (those are rebuilt from
        running state). A stateful regression's paired columns are named ``__rd_<ns>_<key>``; everything else
        (reduced bases, non-stateful regressions' paired columns) is short-lag and slice-safe."""
        skip = {f"__rd_{ns}_{key}" for ns in self.stateful_ns for key in _OLS_KEYS}
        return [expr for expr in self.derived if expr.meta.output_name() not in skip]

    def _derived_row(self, frame: pl.DataFrame, minute: object) -> pl.DataFrame:
        """ONE lazy slice pass: derive the safe value cols + presence/square + the stateful regressions' aux
        cols (cumulative increments, non-stateful partner-slot bases), then return the single row per symbol at
        ``minute`` (sorted by symbol). Lazy so polars fuses the 40+ windowed exprs into one optimized plan."""
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
        return lag_row.with_columns([*self.rust_safe_derived, *self.rust_stateful_aux]).with_columns(self.rust_extra)

    def _stateful_matrix(self, row: pl.DataFrame, minute: object, present: np.ndarray) -> dict[int, np.ndarray]:
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
        source = frame
        if slice_derive:
            # Positional lags (``shift(k).over("symbol")``) need each present symbol's last ``max_lag+1`` ROWS,
            # not a fixed minute window. A sparse symbol's prior bar can be arbitrarily far back in time, and
            # backfill's shift is POSITIONAL (the k-th prior ROW, not the bar k minutes ago); a minute-window
            # slice would miss it and slice-derive a wrong null lag where backfill returns a real value. Tailing
            # by ROW per symbol reaches each symbol's actual prior bars regardless of gaps, so the slice derive
            # is cell-for-cell identical to the whole-buffer derive at the latest row for dense AND sparse
            # symbols (this resolves the OPEN PARITY CONSTRAINT). ``minute``-sort so each symbol's ``tail`` is
            # its latest rows; the derive then runs on ~``max_lag+1`` rows/symbol, not the whole buffer.
            source = frame.sort("minute").group_by("symbol", maintain_order=True).tail(self.max_lag + 1)
        row = self._derived_row_rust(source, minute) if self.rust_slice else self._derived_row(source, minute)
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

    def _reindex_to_index(self, row: pl.DataFrame) -> tuple[pl.DataFrame, np.ndarray]:
        """Align a present-symbols-only derived ``row`` to the fixed session index (``self.symbols``, sorted).
        Returns (full-height row in index order with nulls for absent symbols, present-mask). Fast-paths the
        fully-present case (every index symbol delivered) to avoid a join. Raises ``SymbolSetExpanded`` if the
        minute carries a symbol outside the index — the caller re-seeds (the parity-safe resync path)."""
        symbols = self.symbols or []
        if row.height == len(symbols):  # fully-present (the fixed-set case): row is already the index, sorted
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

    def seed(self, buffer_frame: pl.DataFrame, symbols: list[str] | None = None, *, slice_derive: bool = True) -> None:
        """Establish the symbol index + fixed origins and fold every buffered minute into fresh state (== the
        batch recompute over the buffer; also the daily-resync / crash-recovery entry point). Folds minute by
        minute through the SAME slice-derive + stateful path used live, so the running OBV/time state is built
        exactly as it will be advanced — no separate seeding code to drift from the live path.

        ``symbols`` pins the index to a FIXED session set (e.g. the shard's full universe) — a stable superset
        of any single minute's active symbols, so intraday membership churn folds in as absent (zero) rows
        instead of forcing a re-seed. When None, the index is the symbols seen in ``buffer_frame`` (the prior
        behaviour). ``slice_derive`` controls whether per-minute value columns are derived over a per-symbol
        last-``max_lag+1``-rows tail (fast, and parity-safe for sparse symbols — positional lags reach each
        symbol's actual prior bars) or the whole buffer (gap-safe; identical result, just more rows derived)."""
        index = symbols if symbols is not None else buffer_frame["symbol"].unique().to_list()
        self.symbols = sorted(index)
        self.state = WindowedSumState(self.symbols, self.windows, len(self.value_cols))
        self._seed_stateful(buffer_frame)
        for minute in sorted(buffer_frame["minute"].unique()):
            minute_epoch = int(minute.timestamp())
            self._roll_time_origin(minute_epoch)
            self.state.update(minute_epoch, self._matrix_at(buffer_frame, minute, slice_derive=slice_derive))
            self.state.trim()
        if self.warm_start_assert:
            self.assert_populated(buffer_frame)
            self.warm_start_assert = False  # assert only on the warm-start seed; later resyncs are normal ops

    def seed_buffer_span_minutes(self, buffer_frame: pl.DataFrame) -> float:
        """The span of distinct minutes the seed buffer carries (latest − earliest), in minutes — how much
        history was AVAILABLE to populate the windows. Compared against ``observed_span_minutes`` to tell a
        legitimately-short buffer (no raise) from a failed absorb of a deep buffer (raise)."""
        if buffer_frame.is_empty():
            return 0.0
        minutes = buffer_frame.select(
            pl.col("minute").dt.epoch("s").min().alias("lo"),
            pl.col("minute").dt.epoch("s").max().alias("hi"),
        )
        return float((minutes["hi"][0] - minutes["lo"][0]) / 60.0)

    def assert_populated(self, buffer_frame: pl.DataFrame) -> None:
        """Assert every window the engine declares is ``populated`` GIVEN the history the seed buffer
        carried. Call AFTER a warm-start ``seed(buffer_frame)`` to catch a FAILED warm-start (data present
        but not absorbed) loudly, while letting a legitimately-short seed buffer pass. No-op (the assert is
        vacuous) when the engine declares no windows or has not been seeded."""
        if self.state is None or not self.windows:
            return
        self.state.assert_populated(self.seed_buffer_span_minutes(buffer_frame))

    def step(self, frame: pl.DataFrame, *, slice_derive: bool = True) -> dict[str, pl.DataFrame]:
        """Fold the new latest minute and assemble features from the running sums. ``frame`` is the trailing
        buffer including the new minute. Seeds lazily on first call. A ``SymbolSetExpanded`` (a genuinely new
        ticker appeared) triggers a re-seed from ``frame`` — the parity-safe resync — and a retry, so live
        membership growth never breaks the run. ``slice_derive=False`` derives over the whole buffer (gap-safe,
        O(buffer)); the default fast slice tails each symbol's last ``max_lag+1`` ROWS — positionally exact for
        sparse symbols, so it is cell-for-cell identical to the whole-buffer derive (the OPEN PARITY CONSTRAINT,
        resolved)."""
        latest = frame["minute"].max()
        self._fold_latest(frame, latest, slice_derive=slice_derive)
        long = self._running_long()
        latest_frame = resolve_points(self.groups, frame, latest)  # points resolved over the whole buffer (lag-safe)
        return assemble_from_long(self.groups, long, latest_frame, latest, self.plan, self.reg_plan)

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
        except SymbolSetExpanded:
            self.seed(frame, slice_derive=slice_derive)  # rebuild the index to include the new ticker(s)

    def step_numpy(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """NUMPY-EMIT alternative to ``step``: fold the new minute, then assemble features DIRECTLY from the
        running-sum numpy array via ``emit_numpy`` (no ``_running_long`` long-frame build, no polars pivot in
        assemble). Parity-true by construction — ``emit_numpy`` uses the IDENTICAL canonical/OLS algebra as the
        polars ``assemble_from_long``. Guarded against it cell-for-cell by tests/test_fp_incremental_emit.py."""
        latest = frame["minute"].max()
        self._fold_latest(frame, latest, slice_derive=True)
        assert self.state is not None
        latest_frame = resolve_points(self.groups, frame, latest)  # points resolved over the whole buffer (lag-safe)
        return emit_numpy(
            self.groups,
            self.state.running,
            self.symbols or [],
            self.windows,
            self.col_index,
            latest_frame,
            latest,
            self.plan,
            self.reg_plan,
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
        latest_frame = resolve_points(self.groups, frame, latest)  # points resolved over the whole buffer (lag-safe)
        return emit_rust(self.groups, self.state.running, self.symbols or [], self.asm_plan, latest_frame, latest)

    def step_rust_unified(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """UNIFIED single-pass twin of ``step_rust``: fold the new minute, then assemble EVERY reduction
        group's features in ONE shared wide-frame pass (``emit_rust_unified``) instead of one per-group
        polars frame-build + ``assemble`` each. Parity-true by construction — same kernel, same point exprs,
        same ``assemble`` expressions; only the polars pass count changes. Guarded == ``step_rust`` /
        ``step_numpy`` / ``step`` / batch by tests/test_fp_unified_emit.py."""
        latest = frame["minute"].max()
        self._fold_latest(frame, latest, slice_derive=True)
        assert self.state is not None
        latest_frame = resolve_points(self.groups, frame, latest)  # points resolved over the whole buffer (lag-safe)
        return emit_rust_unified(
            self.groups, self.state.running, self.symbols or [], self.asm_plan, latest_frame, latest
        )

    def _running_long(self) -> pl.DataFrame:
        """The running sums as a LONG (symbol, window, <value-col sum>) frame — the exact shape
        ``assemble_from_long`` expects, so the SAME assemble code runs as in the batch (NO pivot to build it)."""
        assert self.state is not None
        running = self.state.running  # (n_windows, n_symbols, n_cols)
        n_win, n_sym, _ = running.shape
        data: dict[str, object] = {
            "symbol": (self.symbols or []) * n_win,
            "window": [w for w in self.windows for _ in range(n_sym)],
        }
        for col_index, col in enumerate(self.value_cols):
            data[col] = running[:, :, col_index].reshape(-1)
        return pl.DataFrame(data)
