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
power sums, presence/square) only need the last few bars, so they're derived over a ~6-minute slice; the few
columns that depend on long history — a frame-relative OLS time axis and a cumulative regressor (OBV) — are
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
    build_plan,
)

_OLS_KEYS = ("b", "x", "y", "xy", "xx", "yy")


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

    def update(self, minute_epoch: int, values: np.ndarray) -> None:
        """Fold one new minute into every window's running sum, then expire minutes now outside each window."""
        index = len(self._buf_epoch)
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

    def sums(self, window: int) -> np.ndarray:
        """The current ``(n_symbols, n_cols)`` running sum for ``window`` (minutes)."""
        return self.running[self.windows.index(window)]


class IncrementalEngine:
    """The live incremental execution path for the declarative reduction groups. Holds a per-shard
    ``WindowedSumState`` over the union of all groups' value columns (built by ``build_plan`` — the SAME
    columns the batch sums), and at each minute derives ONLY the new minute's values, folds them in, and
    assembles features from the running sums via ``assemble_from_long`` (the SAME core as the batch). So the
    feature logic is identical to the batch/backfill paths; only the source of the sums differs.

    V2 slice-derive: the new minute's short-lag value columns are derived over a ~6-minute slice (not the
    whole buffer); the long-history regressor columns (a frame-relative OLS time axis, a cumulative OBV) are
    declared via ``stateful_regressors()`` and maintained as running per-symbol engine state. The produced
    value matrix is identical to the whole-buffer derive, so parity holds (guarded by
    tests/test_fp_incremental_features.py).

    Usage: ``seed(buffer_frame)`` once (replays the buffer to establish state + symbols), then ``step(frame)``
    each minute (``frame`` = the trailing buffer including the new latest minute) -> {group: feature_frame}.
    """

    DERIVE_SLICE = 6  # minutes of history needed to slice-derive a minute's short-lag value columns (max lag + slack)

    def __init__(self, groups: list[ReductionGroup]) -> None:
        self.groups = [g for g in groups if isinstance(g, ReductionGroup)]
        self.derived, self.extra, self.value_cols, self.plan, self.reg_plan, self.windows = build_plan(self.groups)
        self.col_index = {col: i for i, col in enumerate(self.value_cols)}
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
        self.regx_col: dict[str, str] = {}  # ns -> non-stateful x base col name
        self.regy_col: dict[str, str] = {}
        for ns, slots in self.stateful_specs.items():
            for slot, slot_spec in slots.items():
                if slot_spec.kind == "cumulative":
                    assert slot_spec.increment is not None
                    name = f"__inc_{ns}"
                    self.stateful_aux.append(slot_spec.increment.alias(name))
                    self.inc_col[ns] = name
            if "x" not in slots:
                name = f"__regx_{ns}"
                self.stateful_aux.append(self.reg_x_expr[ns].alias(name))
                self.regx_col[ns] = name
            if "y" not in slots:
                name = f"__regy_{ns}"
                self.stateful_aux.append(self.reg_y_expr[ns].alias(name))
                self.regy_col[ns] = name
        self.aux_cols = [expr.meta.output_name() for expr in self.stateful_aux]

        self.symbols: list[str] | None = None
        self.state: WindowedSumState | None = None
        self.ref_epoch: int | None = None  # fixed origin for "time" regressors (OLS is origin-invariant)
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

    def _stateful_matrix(self, row: pl.DataFrame, minute: object) -> dict[int, np.ndarray]:
        """Rebuild the 6 OLS paired columns (b, x, y, xy, xx, yy) for every stateful regression at ``minute``
        from running x/y state (sourced from the already-derived ``row``) — pairing under nulls exactly as
        ``_ols_derived`` does. Returns {value_col_index: (n_symbols,) column}. Advances the running cumulatives."""
        out: dict[int, np.ndarray] = {}
        n_sym = len(self.symbols or [])
        assert self.ref_epoch is not None
        minute_epoch = int(minute.timestamp())  # type: ignore[attr-defined]
        time_x = np.full(n_sym, (minute_epoch - self.ref_epoch) / 60.0, dtype=np.float64)
        for ns, slots in self.stateful_specs.items():
            x = time_x if slots.get("x") and slots["x"].kind == "time" else self._aux_value(row, self.regx_col[ns])
            if "y" in slots and slots["y"].kind == "cumulative":
                inc = row.select(self.inc_col[ns]).fill_null(0.0).to_numpy().reshape(-1)
                running = self.obv_running.setdefault(ns, np.zeros(n_sym, dtype=np.float64))
                running += inc
                y = running.copy()
            elif "y" in slots and slots["y"].kind == "time":
                y = time_x
            else:
                y = self._aux_value(row, self.regy_col[ns])
            both = np.isfinite(x) & np.isfinite(y)
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

    def _matrix_at(self, frame: pl.DataFrame, minute: object, *, slice_derive: bool) -> np.ndarray:
        """The (n_symbols, n_value_cols) value matrix for ``minute``, symbol-aligned to ``self.symbols`` (nulls
        -> 0, matching the kernel). ``slice_derive`` derives short-lag columns over a small trailing slice and
        rebuilds stateful regression columns from running state; otherwise (seed) it derives over ``frame`` as
        given. Asserts a fixed symbol set (V1)."""
        source = frame
        if slice_derive:
            cutoff = minute - pl.duration(minutes=self.DERIVE_SLICE)  # type: ignore[operator]
            source = frame.filter(pl.col("minute") > cutoff)
        row = self._derived_row(source, minute)
        n_sym = len(self.symbols or [])
        if row.height != n_sym:
            raise ValueError(f"incremental: symbol set changed ({row.height} != {n_sym}); re-seed")
        matrix = np.zeros((n_sym, len(self.value_cols)), dtype=np.float64)
        safe = row.select(self.safe_value_cols).fill_null(0.0).to_numpy()
        for safe_i, col in enumerate(self.safe_value_cols):
            matrix[:, self.col_index[col]] = safe[:, safe_i]
        for col_index, column in self._stateful_matrix(row, minute).items():
            matrix[:, col_index] = column
        return matrix

    def _seed_stateful(self, buffer_frame: pl.DataFrame) -> None:
        """Initialise the running per-symbol state the stateful regressors need before folding the buffer:
        the FIXED time origin (the buffer's earliest minute) and the OBV running totals reset to zero
        (re-accumulated as the seed folds each minute)."""
        self.ref_epoch = int(buffer_frame.select(pl.col("minute").dt.epoch("s").min()).item())
        self.obv_running = {}

    def seed(self, buffer_frame: pl.DataFrame) -> None:
        """Establish the symbol set + fixed origins and fold every buffered minute into fresh state (== the
        batch recompute over the buffer; also the daily-resync / crash-recovery entry point). Folds minute by
        minute through the SAME slice-derive + stateful path used live, so the running OBV/time state is built
        exactly as it will be advanced — no separate seeding code to drift from the live path."""
        self.symbols = sorted(buffer_frame["symbol"].unique().to_list())
        self.state = WindowedSumState(self.symbols, self.windows, len(self.value_cols))
        self._seed_stateful(buffer_frame)
        for minute in sorted(buffer_frame["minute"].unique()):
            self.state.update(int(minute.timestamp()), self._matrix_at(buffer_frame, minute, slice_derive=True))
            self.state.trim()

    def step(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """Fold the new latest minute and assemble features from the running sums. ``frame`` is the trailing
        buffer including the new minute. Seeds lazily on first call."""
        latest = frame["minute"].max()
        if self.state is None:
            self.seed(frame)
        else:
            self.state.update(int(latest.timestamp()), self._matrix_at(frame, latest, slice_derive=True))
            self.state.trim()
        long = self._running_long()
        latest_frame = frame.filter(pl.col("minute") == latest)
        return assemble_from_long(self.groups, long, latest_frame, latest, self.plan, self.reg_plan)

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
