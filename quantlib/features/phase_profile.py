"""Phase-decomposition micro-profiler for the LIVE incremental per-minute path.

The per-group latency table (``latency_expectations.py`` / ``profile.py``) gives ONE number per group:
the wall time of ``IncrementalEngine.step`` for an ``incremental_safe`` reduction group. That number is a
black box — it does NOT say whether the cost is the arithmetic (the running-sum fold) or the framework
(per-minute polars expression evaluation over the trailing buffer). This module opens the box.

For each armed ``incremental_safe`` ReductionGroup it times ``step`` and breaks it into its four phases,
all on the SAME seeded engine the live capture runs:

  * ``matrix_at``      — ``_matrix_at``: the per-minute polars derive (slice/sort/group_by/tail over the
                         WHOLE buffer + the short-lag expressions) marshalled to the numpy value row.
  * ``fold``           — ``state.update``: the Neumaier-compensated running-sum update. This is the ACTUAL
                         O(1) arithmetic (+ ``_roll_time_origin`` + ``trim``, both negligible).
  * ``resolve_points`` — ``resolve_points``: the point/lag exprs evaluated over the WHOLE buffer (a full
                         sort + select + filter) to carry ``__pt_<name>`` onto the latest row.
  * ``assemble``       — ``assemble_from_long``: the numpy running-sums -> long polars frame + the per-group
                         pivot / rename / join / ``assemble()`` expression evaluation.

The sum of the four ~equals the ``step`` total (any gap is allocation/dispatch noise). It also times the
gated-off emit twins (``step_numpy`` / ``step_rust_unified``) for the same group so the headroom of the
already-written faster assemble paths is visible next to the live default.

Run (load-gated, never -n auto):

    docker run --rm --cpus 6 -e POLARS_MAX_THREADS=4 -e ALPACA_KEY_ID=mock -e ALPACA_SECRET_KEY=mock \
        -v "$PWD":/app -w /app fp-dev \
        python -m quantlib.features.phase_profile [n_tickers] [window_min] [reps]

Defaults match the latency-expectations reference shard (312 tickers x 245m), so the per-group totals here
line up with the dashboard's incremental rows.
"""
from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable

import polars as pl

from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, assemble_from_long, resolve_points
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.profile import build_frames, runs_incremental

REF_N_TICKERS = 312
REF_WINDOW_MIN = 245
REF_DAILY_DAYS = 250
DEFAULT_REPS = 60


def _pct(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of ``values`` (not pre-sorted)."""
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def _time(call: Callable[[], object], reps: int) -> list[float]:
    """The ms distribution of ``call`` over ``reps`` runs after one warmup (JIT/cache priming excluded)."""
    call()
    samples: list[float] = []
    for _ in range(reps):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def phase_rows(n_tickers: int, window_min: int, reps: int) -> list[dict[str, object]]:
    """For every runnable armed incremental_safe reduction group, the ``step`` p50 broken into its four
    phases (matrix_at / fold / resolve_points / assemble) plus the gated-off emit-twin p50s, all at the
    reference shard scale on a freshly-seeded single-group engine (the live isolation the dashboard uses)."""
    frames = build_frames(n_tickers, window_min, REF_DAILY_DAYS, include_trades=True)
    rows: list[dict[str, object]] = []
    for group in runnable(frames):
        if not (isinstance(group, ReductionGroup) and runs_incremental(group)):
            continue
        buffer_frame = frames[group.inputs[0].name]
        latest = buffer_frame["minute"].max()
        minute_epoch = int(latest.timestamp())  # type: ignore[attr-defined]

        engine = IncrementalEngine([group])
        engine.seed(buffer_frame)
        step_p50 = _pct(_time(lambda: engine.step(buffer_frame), reps), 50)

        # matrix_at: the per-minute polars derive marshalled to the value row.
        matrix_p50 = _pct(
            _time(lambda: engine._matrix_at(buffer_frame, latest, slice_derive=True), reps), 50
        )
        value_matrix = engine._matrix_at(buffer_frame, latest, slice_derive=True)
        # fold: the Neumaier running-sum update (the genuine O(1) arithmetic) + roll + trim.
        assert engine.state is not None
        fold_p50 = (
            _pct(_time(lambda: engine._roll_time_origin(minute_epoch), reps), 50)
            + _pct(_time(lambda: engine.state.update(minute_epoch, value_matrix), reps), 50)
            + _pct(_time(lambda: engine.state.trim(), reps), 50)
        )
        # resolve_points: the lag/point exprs over the whole buffer.
        points_p50 = _pct(_time(lambda: resolve_points([group], buffer_frame, latest), reps), 50)
        # assemble: long-frame build + per-group pivot/join/expr.
        long_frame = engine._running_long()
        latest_frame = resolve_points([group], buffer_frame, latest)
        assemble_p50 = _pct(
            _time(
                lambda: assemble_from_long(
                    [group], long_frame, latest_frame, latest, engine.plan, engine.reg_plan, engine.centered
                ),
                reps,
            ),
            50,
        )

        twins: dict[str, float] = {}
        for twin in ("step_numpy", "step_rust_unified"):
            twin_engine = IncrementalEngine([group])
            twin_engine.seed(buffer_frame)
            call = getattr(twin_engine, twin)
            twins[twin] = _pct(_time(lambda: call(buffer_frame), reps), 50)

        rows.append(
            {
                "group": group.name,
                "n_windows": len(engine.windows),
                "n_cols": len(engine.value_cols),
                "step_ms": round(step_p50, 3),
                "matrix_at_ms": round(matrix_p50, 3),
                "fold_ms": round(fold_p50, 3),
                "resolve_points_ms": round(points_p50, 3),
                "assemble_ms": round(assemble_p50, 3),
                "polars_overhead_ms": round(matrix_p50 + points_p50 + assemble_p50, 3),
                "step_numpy_ms": round(twins["step_numpy"], 3),
                "step_rust_unified_ms": round(twins["step_rust_unified"], 3),
            }
        )
    rows.sort(key=lambda row: -float(row["step_ms"]))  # type: ignore[arg-type]
    return rows


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n_tickers = int(args[0]) if len(args) > 0 else REF_N_TICKERS
    window_min = int(args[1]) if len(args) > 1 else REF_WINDOW_MIN
    reps = int(args[2]) if len(args) > 2 else DEFAULT_REPS

    print(
        f"=== INCREMENTAL step() phase decomposition @ {n_tickers} tickers x {window_min}m "
        f"({reps} reps, p50) ===",
        flush=True,
    )
    rows = phase_rows(n_tickers, window_min, reps)
    table = pl.DataFrame(rows)
    pl.Config.set_tbl_rows(100)
    pl.Config.set_tbl_cols(20)
    print(table)

    fold_total = sum(float(row["fold_ms"]) for row in rows)
    polars_total = sum(float(row["polars_overhead_ms"]) for row in rows)
    step_total = sum(float(row["step_ms"]) for row in rows)
    print(
        f"\nAcross {len(rows)} incremental groups (sum of standalone p50):\n"
        f"  step total           = {step_total:7.1f} ms\n"
        f"  arithmetic (fold)    = {fold_total:7.1f} ms  ({100.0 * fold_total / step_total:4.1f}%)\n"
        f"  polars overhead      = {polars_total:7.1f} ms  ({100.0 * polars_total / step_total:4.1f}%)\n"
        "    (matrix_at + resolve_points + assemble — per-minute expression evaluation over the buffer)"
    )


if __name__ == "__main__":
    main()
