"""Declarative windowed-reduction features — declare the reduction ONCE, get BOTH forms.

A ``ReductionGroup`` subclass declares three things instead of hand-writing two parallel implementations:
  - ``reduced()``  -> {name: (expr, stats)}  the value columns to reduce over each window, and which
                     statistics it needs of each ("mean" | "std" | "sum"),
  - ``points()``   -> {name: expr}           at-T scalar columns (the latest minute's values),
  - ``assemble()`` -> {feature: expr}        the features, written with the agg accessors
                     ``mean_(col, w)`` / ``std_(col, w)`` / ``sum_(col, w)`` / ``pt_(name)``.

From that ONE declaration the engine GENERATES:
  - ``compute()``         — the rolling form over every minute (backfill / source of truth), materialised
                            with polars ``rolling_*_by`` (so it stays bit-identical to a hand-written group),
  - ``compute_latest()``  — the aggregate-at-T form, one row per symbol, materialised with the single-pass
                            Rust kernels (``rust_reductions``).
Both forms materialise the SAME canonical aggregate columns (``__mean_<col>_<w>`` etc.) and then evaluate
the SAME ``assemble()`` expressions — so they cannot diverge by more than the kernel-vs-rolling float noise
the parity test already tolerates. The modeller writes it ONCE; production (live) and modeling (backfill)
ride the same declaration. Genuinely-weird features still subclass ``FeatureGroup`` directly and write
arbitrary polars — this is the fast lane for the common windowed-reduction shape, not a cage.
"""
from __future__ import annotations

import os
from abc import abstractmethod
from dataclasses import dataclass

import numpy as np
import polars as pl
import quant_tick

from quantlib.features import _phase
from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.latest import pivot_stat, rust_reductions, rust_windowed_sums

# The reduction EMIT (building each group's canonical __<stat>_<col>_<w> columns from the running sums) is
# the fast-path floor. FP_RUST_ASSEMBLE moves that canonical column algebra into the ``assemble_canonical``
# Rust kernel (one pass over the whole running-sum array, NaN==null by construction); the numpy/polars
# ``emit_numpy`` stays the parity reference (FP_RUST_ASSEMBLE unset, or FP_RUST_ASSEMBLE=0).
_USE_RUST_ASSEMBLE = bool(os.environ.get("FP_RUST_ASSEMBLE")) and os.environ.get("FP_RUST_ASSEMBLE") != "0"

# Statistic codes shared with the Rust ``assemble_canonical`` kernel (kind byte). The OLS codes' order
# (slope, corr, r2, mean_y) matches the kernel's 3..=6 arm.
_STAT_CODE = {"sum": 0, "mean": 1, "std": 2, "slope": 3, "corr": 4, "r2": 5, "mean_y": 6}

# Agg accessors — used inside assemble() to reference the canonical aggregate columns the engine builds.
STATS = ("mean", "std", "sum")


def mean_(col: str, w: int) -> pl.Expr:
    return pl.col(f"__mean_{col}_{w}")


def std_(col: str, w: int) -> pl.Expr:
    return pl.col(f"__std_{col}_{w}")


def sum_(col: str, w: int) -> pl.Expr:
    return pl.col(f"__sum_{col}_{w}")


def pt_(name: str) -> pl.Expr:
    return pl.col(f"__pt_{name}")


# OLS (regression) accessors — used inside assemble() to reference a regression's canonical stat columns.
OLS_STATS = ("slope", "corr", "r2", "mean_y")


def slope_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__slope_{name}_{w}")


def corr_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__corr_{name}_{w}")


def r2_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__r2_{name}_{w}")


def mean_y_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__mean_y_{name}_{w}")


def _ols_stat_exprs(sums: dict[str, pl.Expr], stats: tuple[str, ...]) -> dict[str, pl.Expr]:
    """OLS slope/corr/r2/mean_y of y-on-x from the six paired windowed sums (b=paired count, x, y, xy, xx,
    yy). Identical algebra to ols.py — pairing handled by the caller (partner-null rows zeroed, excluded
    from b). Undefined cells (n<2 or zero x-variance) are null."""
    b, sx, sy, sxy, sxx, syy = (sums[key] for key in ("b", "x", "y", "xy", "xx", "yy"))
    denom_x = b * sxx - sx * sx
    denom_y = b * syy - sy * sy
    cov_n = b * sxy - sx * sy
    defined = (b >= 2.0) & (denom_x > 0.0)
    defined_corr = defined & (denom_y > 0.0)
    out: dict[str, pl.Expr] = {}
    if "slope" in stats:
        out["slope"] = pl.when(defined).then(cov_n / denom_x).otherwise(None)
    if "corr" in stats:
        out["corr"] = pl.when(defined_corr).then(cov_n / (denom_x * denom_y).sqrt()).otherwise(None)
    if "r2" in stats:
        out["r2"] = pl.when(defined_corr).then((cov_n * cov_n) / (denom_x * denom_y)).otherwise(None)
    if "mean_y" in stats:
        out["mean_y"] = pl.when(b > 0).then(sy / b).otherwise(None)
    return out


@dataclass(frozen=True)
class StatefulRegressor:
    """Declares that a regression's ``slot`` (``"x"`` or ``"y"``) is NOT a short-lag PER-SYMBOL column the
    incremental engine can slice-derive, but a value the engine must source specially each minute. Three kinds:

    - ``kind="time"``: a frame-relative time axis ``(epoch_minutes - origin)``. Slice-derive can't reproduce
      a frame-relative origin, so the engine substitutes a FIXED origin (its seed minute). OLS is
      origin-invariant, so slope/r2/corr are identical to the batch's per-frame-centered axis within tol.
    - ``kind="cumulative"``: a running total ``v[T] = v[T-1] + increment[T]`` (e.g. OBV = cum_sum(signed)).
      The group provides ``increment`` (a short-lag expr the engine evaluates per minute) and the engine keeps
      the running per-symbol total. The centered-time *partner* slot may also be ``"time"``.
    - ``kind="broadcast"``: a CROSS-SYMBOL value that is the SAME for every symbol at a given minute — a market
      index's per-minute value broadcast to the whole universe (e.g. SPY's one-minute return as the market-beta
      regressor). The group provides ``broadcast_symbol`` (the index ticker whose row carries the value) and
      ``increment`` (the short-lag expr that yields that ticker's per-minute value, e.g. ``close/close.shift(1)
      - 1``). Each minute the engine reads that expr at the index symbol's row and broadcasts it to all symbols'
      slot — the cross-symbol minute-join the batch path does, without a per-symbol rolling.

    Backfill / live-batch ignore this entirely — they evaluate the group's own ``regressions()`` exprs
    directly (over a frame the group's ``prepare`` has already broadcast onto). It only tells the incremental
    engine HOW to source that regressor instead of re-deriving over the buffer."""

    slot: str  # "x" or "y"
    kind: str  # "time" | "cumulative" | "broadcast"
    increment: pl.Expr | None = None  # required iff kind in {"cumulative", "broadcast"}
    broadcast_symbol: str | None = None  # required iff kind == "broadcast" (the index ticker carrying the value)


def _ols_derived(name: str, x_expr: pl.Expr, y_expr: pl.Expr) -> list[pl.Expr]:
    """The six paired columns the engine sums for one regression: only rows where BOTH x and y are present
    contribute (partner-null zeroed and dropped from the count), so a warmup/missing value never biases the
    fit. Column names ``__rd_<name>_{b,x,y,xy,xx,yy}``."""
    both = x_expr.is_not_null() & y_expr.is_not_null()
    x_paired = pl.when(both).then(x_expr).otherwise(0.0)
    y_paired = pl.when(both).then(y_expr).otherwise(0.0)
    return [
        both.cast(pl.Float64).alias(f"__rd_{name}_b"),
        x_paired.alias(f"__rd_{name}_x"),
        y_paired.alias(f"__rd_{name}_y"),
        (x_paired * y_paired).alias(f"__rd_{name}_xy"),
        (x_paired * x_paired).alias(f"__rd_{name}_xx"),
        (y_paired * y_paired).alias(f"__rd_{name}_yy"),
    ]


class ReductionGroup(FeatureGroup):
    """Base for a windowed-reduction feature group. Set ``reduce_input`` and implement ``reduced()`` /
    ``points()`` / ``assemble()``; ``compute()`` and ``compute_latest()`` are generated."""

    reduce_input: str = "minute_agg"

    # Whether this group may be served from the INCREMENTAL running sums (FP_INCREMENTAL) or must stay on the
    # batch fresh-sum recompute. Default True — for almost every group the incremental sums and the batch fresh
    # window sums agree to benign float drift. Set False on a group whose canonical algebra is a difference of
    # large near-equal sums on LARGE-MAGNITUDE values (variance/correlation of raw share volume), where the
    # running add/subtract rounds differently from the batch fresh sum and the cancellation amplifies it past
    # the parity-breach ratio at near-degenerate cells (a perfect-fit corr, an n=2 z-score). Such a group keeps
    # the batch fresh-sum path even under FP_INCREMENTAL until a stable-summation rewrite closes the corner;
    # the absolute divergence is ~1e-8 (float floor), so this is a parity-self-check guard, not a value bug.
    incremental_safe: bool = True

    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Optional per-minute preprocessing of the (symbol, minute)-sorted input frame BEFORE the reduced /
        regression exprs are evaluated — for a CROSS-SYMBOL column the per-symbol exprs need (e.g. broadcasting
        a market index's per-minute return onto every symbol's row). Applied identically in ``compute`` (rolling
        backfill), ``compute_latest`` (live), and the batched path, so both forms see the same column. The
        incremental engine sources such broadcast regressors from running state (``broadcast`` StatefulRegressor)
        and therefore does NOT call ``prepare``. Default: identity (no extra columns)."""
        return frame

    @abstractmethod
    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        """{column_name: (expr_over_input, stats, windows)} — the value to reduce, which stats each needs
        ("mean"|"std"|"sum"), and the windows (in minutes) for that column (each column may differ)."""

    def points(self) -> dict[str, pl.Expr]:
        """{name: expr_over_input} — at-T scalar columns referenced via pt_() in assemble(). Default none."""
        return {}

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        """{name: (x_expr, y_expr, stats, windows)} — windowed OLS of y on x; stats ⊆ slope/corr/r2/mean_y,
        referenced via slope_/corr_/r2_/mean_y_ in assemble(). Default none. (For a TIME regressor, pass a
        small frame-relative x like ``(minute.epoch - minute.epoch.min())`` — OLS is origin-invariant.)"""
        return {}

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        """{regression_name: [StatefulRegressor, ...]} — declares which regressor slots the INCREMENTAL engine
        must source from running per-symbol state (a frame-relative time axis, or a cumulative like OBV)
        instead of slice-deriving. ONLY the incremental path reads this; backfill/live-batch evaluate the
        ``regressions()`` exprs directly and are unaffected. Default none (all regressors are short-lag)."""
        return {}

    @abstractmethod
    def assemble(self) -> dict[str, pl.Expr]:
        """{feature_name: expr} written with mean_/std_/sum_/pt_ — evaluated identically in both forms."""

    def _feature_names(self) -> list[str]:
        return list(self.assemble().keys())

    def _input_columns(self) -> list[str]:
        for spec in self.inputs:
            if spec.name == self.reduce_input:
                return list(spec.columns)
        raise KeyError(f"{self.name}: reduce_input '{self.reduce_input}' not in inputs")

    def reduce_buffer_minutes(self) -> int | None:
        """Derived from this group's DECLARED reduced/regression windows — the longest window is the
        deepest trailing context its latest-minute reduction reads. ``None`` only if it declares no
        windows (then the caller keeps the full buffer)."""
        windows: list[int] = []
        for _, _, group_windows in self.reduced().values():
            windows.extend(group_windows)
        for _, _, _, group_windows in self.regressions().values():
            windows.extend(group_windows)
        return max(windows) if windows else None

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated BACKFILL form: rolling_*_by over every minute (source of truth)."""
        frame = self.prepare(ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"]))
        reduced, regressions = self.reduced(), self.regressions()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _, _) in reduced.items()])
        if regressions:
            frame = frame.with_columns(
                [col for name, (x, y, _, _) in regressions.items() for col in _ols_derived(name, x, y)]
            )
        mats: list[pl.Expr] = []
        for name, (_, stats, windows) in reduced.items():
            source = pl.col(f"__d_{name}")
            for w in windows:
                size = f"{w}m"
                if "mean" in stats:
                    mats.append(source.rolling_mean_by("minute", window_size=size).over("symbol").alias(f"__mean_{name}_{w}"))
                if "std" in stats:
                    mats.append(source.rolling_std_by("minute", window_size=size).over("symbol").alias(f"__std_{name}_{w}"))
                if "sum" in stats:
                    mats.append(source.rolling_sum_by("minute", window_size=size).over("symbol").alias(f"__sum_{name}_{w}"))
        for name, (_, _, stats, windows) in regressions.items():
            for w in windows:
                size = f"{w}m"
                sums = {
                    key: pl.col(f"__rd_{name}_{key}").rolling_sum_by("minute", window_size=size).over("symbol")
                    for key in ("b", "x", "y", "xy", "xx", "yy")
                }
                for stat, expr in _ols_stat_exprs(sums, stats).items():
                    mats.append(expr.alias(f"__{stat}_{name}_{w}"))
        frame = frame.with_columns([expr.alias(f"__pt_{name}") for name, expr in self.points().items()])
        frame = frame.with_columns(mats)
        feats = self.assemble()
        return frame.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()]).select(
            ["symbol", "minute", *self._feature_names()]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated LIVE form: aggregate-at-T via the Rust reduction kernel, one row per symbol."""
        frame = self.prepare(ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"]))
        reduced, regressions = self.reduced(), self.regressions()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _, _) in reduced.items()])
        if regressions:
            frame = frame.with_columns(
                [col for name, (x, y, _, _) in regressions.items() for col in _ols_derived(name, x, y)]
            )
        latest = frame["minute"].max()
        wide = resolve_points([self], frame, latest).select(
            ["symbol", *[f"__pt_{name}" for name in self.points()]]
        )
        for name, (_, stats, windows) in reduced.items():
            long = rust_reductions(frame, f"__d_{name}", windows)
            for stat in stats:
                wide = wide.join(pivot_stat(long, stat, f"__{stat}_{name}_{{w}}", windows), on="symbol", how="left")
        for name, (_, _, stats, windows) in regressions.items():
            value_cols = [f"__rd_{name}_{key}" for key in ("b", "x", "y", "xy", "xx", "yy")]
            long = rust_windowed_sums(frame, value_cols, windows)
            sums = {key: pl.col(f"__rd_{name}_{key}") for key in ("b", "x", "y", "xy", "xx", "yy")}
            glong = long.with_columns([expr.alias(f"__c_{stat}_{name}") for stat, expr in _ols_stat_exprs(sums, stats).items()])
            for stat in stats:
                wide = wide.join(
                    pivot_stat(glong, f"__c_{stat}_{name}", f"__{stat}_{name}_{{w}}", windows), on="symbol", how="left"
                )
        feats = self.assemble()
        return (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *self._feature_names()])
        )


def _canonical(name: str, stats: tuple[str, ...], base: str) -> list[pl.Expr]:
    """Per-window canonical stat columns for one reduced column, derived from its batched windowed sums in
    the long frame: mean = sum/count, std(ddof=1) = sqrt((sumsq - sum^2/count)/(count-1)). ``base`` is the
    sum-of-value column, ``base__p`` the sum-of-presence (non-null count), ``base__sq`` the sum-of-squares."""
    out = []
    if "sum" in stats:
        out.append(pl.col(base).alias(f"__c_sum_{name}"))
    if "mean" in stats:
        count = pl.col(f"{base}__p")
        # guard count==0 (an all-null window) -> null, matching rolling_mean / rust_reductions (not NaN)
        out.append(pl.when(count > 0).then(pl.col(base) / count).otherwise(None).alias(f"__c_mean_{name}"))
    if "std" in stats:
        count, total, sumsq = pl.col(f"{base}__p"), pl.col(base), pl.col(f"{base}__sq")
        out.append(
            pl.when(count > 1)
            .then(((sumsq - total * total / count) / (count - 1)).sqrt())
            .otherwise(None)
            .alias(f"__c_std_{name}")
        )
    return out


_PlanEntry = tuple[int, str, tuple[str, ...], tuple[int, ...], str]


def build_plan(
    groups: list[ReductionGroup],
) -> tuple[list[pl.Expr], list[pl.Expr], list[str], list[_PlanEntry], list[_PlanEntry], tuple[int, ...]]:
    """The union value-column plan for a set of declarative groups — SHARED by the batch and the incremental
    engine so both sum EXACTLY the same columns. Returns:
      derived  — exprs for the base reduced cols + the six OLS paired cols (namespaced per group),
      extra    — exprs for the presence/square cols that mean/std need,
      value_cols — the ordered names to sum (base, base__p, base__sq, OLS b/x/y/xy/xx/yy),
      plan/reg_plan — per-group (gi, name, stats, windows, base|ns) for assemble_from_long,
      windows  — the sorted union of all windows."""
    derived: list[pl.Expr] = []
    plan: list[_PlanEntry] = []
    reg_plan: list[_PlanEntry] = []
    all_windows: set[int] = set()
    for gi, group in enumerate(groups):
        for name, (expr, stats, windows) in group.reduced().items():
            base = f"__b{gi}_{name}"
            derived.append(expr.alias(base))
            plan.append((gi, name, stats, tuple(windows), base))
            all_windows |= set(windows)
        for name, (x_expr, y_expr, stats, windows) in group.regressions().items():
            ns = f"{gi}_{name}"  # namespace the regression's six paired columns per group
            derived += _ols_derived(ns, x_expr, y_expr)
            reg_plan.append((gi, name, stats, tuple(windows), ns))
            all_windows |= set(windows)
    extra: list[pl.Expr] = []
    value_cols: list[str] = []
    for _, _, stats, _, base in plan:
        value_cols.append(base)
        if "mean" in stats or "std" in stats:
            extra.append(pl.col(base).is_not_null().cast(pl.Float64).alias(f"{base}__p"))
            value_cols.append(f"{base}__p")
        if "std" in stats:
            extra.append((pl.col(base) * pl.col(base)).alias(f"{base}__sq"))
            value_cols.append(f"{base}__sq")
    for _, _, _, _, ns in reg_plan:
        value_cols += [f"__rd_{ns}_{key}" for key in ("b", "x", "y", "xy", "xx", "yy")]
    return derived, extra, value_cols, plan, reg_plan, tuple(sorted(all_windows))


def compute_reduction_batch(groups: list[ReductionGroup], ctx: BatchContext) -> dict[str, pl.DataFrame]:
    """Compute MANY declarative reduction groups in ONE shared marshal + kernel pass.

    Groups sharing a ``reduce_input`` have their derived columns concatenated into one frame; a SINGLE
    ``rust_windowed_sums`` (over every derived column + its square + a presence flag, across the union of
    all windows) replaces one kernel call per group — so the buffer is symbol-coded, sorted, and copied to
    numpy ONCE instead of N times (that per-group marshaling is the live-path floor). Each group then
    assembles from its own slice of the shared sums. Returns {group_name: feature_frame}; each is identical
    within tolerance to that group's own ``compute_latest`` (the generic parity test still guards each)."""
    groups = [g for g in groups if isinstance(g, ReductionGroup)]
    if not groups:
        return {}
    reduce_input = groups[0].reduce_input
    input_cols: list[str] = []
    for group in groups:
        for col in group._input_columns():
            if col not in input_cols:
                input_cols.append(col)
    with _phase.phase("batch.sort"):
        frame = ctx.frame(reduce_input).select(input_cols).sort(["symbol", "minute"])
    for group in groups:
        frame = group.prepare(frame)
    latest = frame["minute"].max()

    derived, extra, value_cols, plan, reg_plan, windows = build_plan(groups)
    with _phase.phase("batch.derive(value cols)"):
        frame = frame.with_columns(derived)
    with _phase.phase("batch.derive(sq+presence)"):
        frame = frame.with_columns(extra)
    long = rust_windowed_sums(frame, value_cols, windows)

    with _phase.phase("batch.assemble(pivot+join)"):
        return assemble_from_long(groups, long, resolve_points(groups, frame, latest), latest, plan, reg_plan)


def resolve_points(groups: list[ReductionGroup], frame: pl.DataFrame, latest: object) -> pl.DataFrame:
    """Evaluate every group's ``points()`` exprs over the FULL trailing buffer (so positive-lag exprs such as
    ``close.shift(w).over("symbol")`` resolve against history — exactly as backfill ``compute()`` does), then
    return the single latest-minute row per symbol carrying the materialised ``__pt_<name>`` columns.

    PARITY FIX (was a live-vs-backfill break): every assemble path previously re-evaluated the point exprs on
    a SINGLE-minute frame (``frame.filter(minute == latest)``), where ``shift(w>0).over("symbol")`` is null —
    so the lag-point feature families (efficiency, return_dynamics, momentum_consistency, ...) emitted 100%
    NaN live while backfill computed them fine. Resolving over the whole buffer is gap-safe (a sparse
    symbol's prior bar is found however far back it is) and matches the backfill truth. Point names that
    collide across groups carry the SAME expr on the SAME input column (dedup by output name is byte-correct
    — the invariant ``emit_rust_unified`` already relies on). Assemble paths now SELECT the precomputed
    ``__pt_<name>`` columns by name rather than re-evaluating the exprs on the latest minute."""
    point_exprs: dict[str, pl.Expr] = {}
    for group in groups:
        for name, expr in group.points().items():
            point_exprs.setdefault(f"__pt_{name}", expr.alias(f"__pt_{name}"))
    return (
        frame.sort(["symbol", "minute"])
        .select(["symbol", "minute", *point_exprs.values()])
        .filter(pl.col("minute") == latest)
    )


def assemble_from_long(
    groups: list[ReductionGroup],
    long: pl.DataFrame,
    latest_frame: pl.DataFrame,
    latest: object,
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
) -> dict[str, pl.DataFrame]:
    """Build each group's feature frame from a LONG (symbol, window, <value-col sums>) frame + the latest
    minute's rows carrying the precomputed ``__pt_<name>`` point columns (from ``resolve_points``, which
    resolves positive-lag points over the whole buffer — see that function). SHARED by the live-batch path
    (``long`` from the Rust kernel) and the incremental path (``long`` from the running-sum state) — so the
    canonical algebra and ``assemble()`` are the SAME code in both; only the source of the sums differs.
    ``latest`` is the minute stamped on output."""
    results: dict[str, pl.DataFrame] = {}
    for gi, group in enumerate(groups):
        canon: list[pl.Expr] = []
        for pgi, name, stats, _, base in plan:
            if pgi == gi:
                canon += _canonical(name, stats, base)
        for pgi, name, stats, _, ns in reg_plan:
            if pgi == gi:
                sums = {key: pl.col(f"__rd_{ns}_{key}") for key in ("b", "x", "y", "xy", "xx", "yy")}
                canon += [expr.alias(f"__c_{stat}_{name}") for stat, expr in _ols_stat_exprs(sums, stats).items()]
        # ONE pivot for ALL of this group's canonical columns (vs one pivot+join per stat) — the pivot
        # names columns `<value>_<window>`, so `__c_<stat>_<name>` over window w -> `__c_<stat>_<name>_<w>`,
        # which we rename to the `__<stat>_<name>_<w>` the accessors expect. Extra union-windows are dropped
        # by the final feature select.
        # `__c_z` keeps ≥2 value columns so polars always names pivoted columns `<value>_<window>` (with a
        # single value it would drop the value name to just `<window>`).
        glong = long.select(["symbol", "window", *canon, pl.lit(0.0).alias("__c_z")])
        piv = glong.pivot(on="window", index="symbol")
        piv = piv.rename({c: "__" + c[4:] for c in piv.columns if c.startswith("__c_") and not c.startswith("__c_z")})
        wide = latest_frame.select(
            ["symbol", *[f"__pt_{name}" for name in group.points()]]
        ).join(piv, on="symbol", how="left")
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results


@dataclass(frozen=True)
class _AssemblePlan:
    """The flattened, group-INDEPENDENT plan the Rust ``assemble_canonical`` kernel consumes, built ONCE per
    engine (it is pure metadata over plan/reg_plan/col_index/windows). One row per OUTPUT canonical column:
    the window index into ``running``, the statistic kind byte, and up to six value-col indices. ``col_names``
    is the per-column accessor name (``__<stat>_<name>_<w>``) and ``group_slices`` maps each group index to its
    contiguous half-open column range in the kernel output, so a group's wide columns are sliced with no copy."""

    win: list[int]
    kind: list[int]
    idx: tuple[list[int], list[int], list[int], list[int], list[int], list[int]]
    col_names: list[str]
    group_slices: dict[int, tuple[int, int]]


def build_assemble_plan(
    groups: list[ReductionGroup],
    windows: tuple[int, ...],
    col_index: dict[str, int],
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
) -> _AssemblePlan:
    """Flatten plan/reg_plan into the per-output-column spec the Rust kernel needs, in the SAME column order
    (per group: reduced columns then regressions, each window then stat) ``emit_numpy`` builds ``wide_cols``.
    Pure metadata; built once and reused every minute."""
    win_index = {int(w): wi for wi, w in enumerate(windows)}
    win: list[int] = []
    kind: list[int] = []
    idx_lists: tuple[list[int], ...] = ([], [], [], [], [], [])
    col_names: list[str] = []
    group_slices: dict[int, tuple[int, int]] = {}

    def push(window: int, stat: str, indices: list[int], col_name: str) -> None:
        win.append(win_index[int(window)])
        kind.append(_STAT_CODE[stat])
        padded = (indices + [0, 0, 0, 0, 0, 0])[:6]
        for axis in range(6):
            idx_lists[axis].append(padded[axis])
        col_names.append(col_name)

    for gi, _group in enumerate(groups):
        start = len(col_names)
        for pgi, name, stats, group_windows, base in plan:
            if pgi != gi:
                continue
            for window in group_windows:
                for stat in stats:
                    if stat == "sum":
                        indices = [col_index[base]]
                    elif stat == "mean":
                        indices = [col_index[base], col_index[f"{base}__p"]]
                    else:  # std
                        indices = [col_index[base], col_index[f"{base}__p"], col_index[f"{base}__sq"]]
                    push(window, stat, indices, f"__{stat}_{name}_{window}")
        for pgi, name, stats, group_windows, ns in reg_plan:
            if pgi != gi:
                continue
            ols_indices = [col_index[f"__rd_{ns}_{key}"] for key in ("b", "x", "y", "xy", "xx", "yy")]
            for window in group_windows:
                for stat in stats:
                    push(window, stat, ols_indices, f"__{stat}_{name}_{window}")
        group_slices[gi] = (start, len(col_names))
    return _AssemblePlan(win, kind, idx_lists, col_names, group_slices)


def emit_rust(
    groups: list[ReductionGroup],
    running: np.ndarray,
    symbols: list[str],
    asm_plan: _AssemblePlan,
    latest_frame: pl.DataFrame,
    latest: object,
) -> dict[str, pl.DataFrame]:
    """RUST-ASSEMBLE alternative to ``emit_numpy``: compute EVERY group's per-window canonical columns in ONE
    ``quant_tick.assemble_canonical`` pass over the whole ``(n_windows, n_symbols, n_value_cols)`` running-sum
    array (the same canonical/OLS algebra as ``_canonical_numpy``/``_ols_stat_numpy``, NaN==null by
    construction), then slice each group's columns out of the contiguous result. Replaces ONLY how the
    ``__<stat>_<name>_<w>`` columns are PRODUCED — each group's ``assemble()`` (the feature formulas) is
    unchanged. ``asm_plan`` is ``build_assemble_plan(...)`` (pure metadata, built once)."""
    canon = quant_tick.assemble_canonical(
        np.ascontiguousarray(running), asm_plan.win, asm_plan.kind, *asm_plan.idx
    )  # (n_symbols, n_out), NaN where null
    symbol_series = pl.Series("symbol", symbols)
    results: dict[str, pl.DataFrame] = {}
    for gi, group in enumerate(groups):
        start, stop = asm_plan.group_slices[gi]
        # Ingest the group's contiguous canonical block in ONE polars allocation (vs a per-column pl.Series
        # copy): pl.from_numpy over the C-contiguous (n_symbols, n_group_cols) slice. NaN stays NaN (not null),
        # exactly as the numpy emit's per-column Series — so assemble()'s null/NaN handling is unchanged.
        points_select = latest_frame.select(
            ["symbol", *[f"__pt_{name}" for name in group.points()]]
        )
        if stop > start:
            block = np.ascontiguousarray(canon[:, start:stop])
            piv = pl.from_numpy(block, schema=asm_plan.col_names[start:stop]).with_columns(symbol_series)
            wide = points_select.join(piv, on="symbol", how="left")
        else:
            wide = points_select  # points-only group: no canonical columns to ingest
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results


def emit_rust_unified(
    groups: list[ReductionGroup],
    running: np.ndarray,
    symbols: list[str],
    asm_plan: _AssemblePlan,
    latest_frame: pl.DataFrame,
    latest: object,
) -> dict[str, pl.DataFrame]:
    """UNIFIED single-pass twin of ``emit_rust`` (a SCHEDULING change, not a math change).

    ``emit_rust`` runs the ONE ``assemble_canonical`` Rust kernel, then for EACH of the ~13 reduction
    groups builds its own polars frame (ingest its canonical slice + a per-group points select + a join)
    and evaluates that group's ``assemble()`` exprs in its OWN ``with_columns`` — the per-group polars
    frame-build + expr-eval is the reduction-emit floor (the canonical algebra is ~1-3ms). This builds ONE
    wide frame keyed (symbol) carrying EVERY group's canonical columns (the kernel's full contiguous
    ``(n_symbols, n_out)`` block ingested in ONE ``pl.from_numpy`` — every canonical name is unique across
    groups by construction of ``build_assemble_plan``) plus the UNION of every group's ``__pt_<name>`` point
    columns (deduped by output name; colliding point names across groups carry the SAME expr on the SAME
    input column, so one shared column is byte-correct), then evaluates ALL groups' ``assemble()`` exprs in
    ONE ``with_columns`` pass, and slices each group's feature columns back out.

    Byte-identical to per-group ``emit_rust`` by construction: the SAME kernel output, the SAME point exprs,
    feeding the SAME ``assemble()`` expressions — only the polars pass/join count changes (1 ingest + 1
    points-select + 1 join + 1 with_columns, vs N of each). Feature names are unique across groups, so the
    per-group slice is exact. Returns the SAME ``{group_name: feature_frame}`` shape ``emit_rust`` returns."""
    canon = quant_tick.assemble_canonical(
        np.ascontiguousarray(running), asm_plan.win, asm_plan.kind, *asm_plan.idx
    )  # (n_symbols, n_out), NaN where null
    symbol_series = pl.Series("symbol", symbols)

    # Ingest EVERY group's canonical columns in ONE pl.from_numpy over the full contiguous kernel block.
    # All canonical names (asm_plan.col_names) are unique across groups, so there is no column collision.
    if canon.shape[1] > 0:
        wide = pl.from_numpy(np.ascontiguousarray(canon), schema=asm_plan.col_names).with_columns(symbol_series)
    else:
        wide = pl.DataFrame({"symbol": symbols})

    # The UNION of every group's at-T point columns, deduped by output name (colliding names are identical
    # exprs), evaluated on the latest minute's frame ONCE and joined onto the wide canonical frame.
    point_cols: list[str] = []
    for group in groups:
        for name in group.points():
            col = f"__pt_{name}"
            if col not in point_cols:
                point_cols.append(col)
    if point_cols:
        points = latest_frame.select(["symbol", *point_cols])
        wide = wide.join(points, on="symbol", how="left")

    # Evaluate ALL groups' assemble() exprs in ONE with_columns pass (feature names unique across groups).
    all_feature_exprs: list[pl.Expr] = []
    for group in groups:
        for name, expr in group.assemble().items():
            all_feature_exprs.append(expr.cast(pl.Float64).alias(name))
    wide = wide.with_columns(all_feature_exprs).with_columns(pl.lit(latest).alias("minute"))

    results: dict[str, pl.DataFrame] = {}
    for group in groups:
        results[group.name] = wide.select(["symbol", "minute", *group._feature_names()])
    return results


def _canonical_numpy(
    sums: np.ndarray, stats: tuple[str, ...], col_index: dict[str, int], base: str
) -> dict[str, np.ndarray]:
    """Numpy twin of ``_canonical`` for ONE reduced column over ONE window. ``sums`` is the ``(n_symbols,
    n_value_cols)`` running-sum row for the window. Reproduces the IDENTICAL algebra cell-for-cell, with
    ``np.nan`` standing in for polars ``null`` (the same guard conditions): mean = sum/count guarded count>0,
    std(ddof=1) = sqrt((sumsq - sum^2/count)/(count-1)) guarded count>1. Returns {canonical_col_name: column}
    keyed ``__c_<stat>_<name>`` to mirror the polars path's intermediate names."""
    total = sums[:, col_index[base]]
    out: dict[str, np.ndarray] = {}
    name = base  # key the returned dict by ``base`` (the caller looks up ``__c_<stat>_<base>`` directly)
    if "sum" in stats:
        out[f"__c_sum_{name}"] = total
    if "mean" in stats:
        count = sums[:, col_index[f"{base}__p"]]
        mean = np.where(count > 0, np.divide(total, count, out=np.zeros_like(total), where=count > 0), np.nan)
        out[f"__c_mean_{name}"] = mean
    if "std" in stats:
        count = sums[:, col_index[f"{base}__p"]]
        sumsq = sums[:, col_index[f"{base}__sq"]]
        safe = count > 1
        # ((sumsq - total^2/count) / (count - 1)).sqrt() — guarded count>1 (else null), matching _canonical.
        # Sentinel count=2 on unsafe rows (count<=1) so the intermediate never divides by zero; those rows are
        # then masked to NaN. On SAFE rows the algebra is bit-identical to the polars _canonical expression.
        cnt_safe = np.where(safe, count, 2.0)
        var_calc = (sumsq - total * total / cnt_safe) / (cnt_safe - 1.0)
        out[f"__c_std_{name}"] = np.where(safe, np.sqrt(var_calc), np.nan)
    return out


def _ols_stat_numpy(
    sums: np.ndarray, stats: tuple[str, ...], col_index: dict[str, int], ns: str
) -> dict[str, np.ndarray]:
    """Numpy twin of ``_ols_stat_exprs`` for ONE regression over ONE window — IDENTICAL algebra to
    ``_ols_derived``/``ols.py``, with ``np.nan`` for polars ``null`` and the SAME defined guards (b>=2 &
    denom_x>0 for slope; additionally denom_y>0 for corr/r2). The six paired sums are columns
    ``__rd_<ns>_{b,x,y,xy,xx,yy}`` in the running-sum row."""
    base_sums = {key: sums[:, col_index[f"__rd_{ns}_{key}"]] for key in ("b", "x", "y", "xy", "xx", "yy")}
    b, sx, sy, sxy, sxx, syy = (base_sums[key] for key in ("b", "x", "y", "xy", "xx", "yy"))
    denom_x = b * sxx - sx * sx
    denom_y = b * syy - sy * sy
    cov_n = b * sxy - sx * sy
    defined = (b >= 2.0) & (denom_x > 0.0)
    defined_corr = defined & (denom_y > 0.0)
    out: dict[str, np.ndarray] = {}
    if "slope" in stats:
        slope = np.where(defined, np.divide(cov_n, denom_x, out=np.zeros_like(cov_n), where=defined), np.nan)
        out["slope"] = slope
    if "corr" in stats:
        denom = np.sqrt(denom_x * denom_y)
        corr = np.where(defined_corr, np.divide(cov_n, denom, out=np.zeros_like(cov_n), where=defined_corr), np.nan)
        out["corr"] = corr
    if "r2" in stats:
        prod = denom_x * denom_y
        r2 = np.where(defined_corr, np.divide(cov_n * cov_n, prod, out=np.zeros_like(cov_n), where=defined_corr), np.nan)
        out["r2"] = r2
    if "mean_y" in stats:
        out["mean_y"] = np.where(b > 0, np.divide(sy, b, out=np.zeros_like(sy), where=b > 0), np.nan)
    return out


def emit_numpy(
    groups: list[ReductionGroup],
    running: np.ndarray,
    symbols: list[str],
    windows: tuple[int, ...],
    col_index: dict[str, int],
    latest_frame: pl.DataFrame,
    latest: object,
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
) -> dict[str, pl.DataFrame]:
    """NUMPY-NATIVE alternative to ``assemble_from_long`` — builds each group's per-window canonical columns
    (``__<stat>_<name>_<w>``) DIRECTLY from the ``(n_windows, n_symbols, n_value_cols)`` running-sum array,
    BYPASSING the polars pivot. The canonical/OLS algebra is the numpy twin of ``_canonical``/``_ols_stat_exprs``
    (parity-true by construction; null↔NaN), and a column is only emitted for the windows a group actually
    declares (so the wide frame already has the accessor-expected columns, no pivot/rename). ``running`` is
    ``WindowedSumState.running``; ``col_index`` maps value-col name -> column index. Returns the SAME
    {group_name: feature_frame} shape as ``assemble_from_long``."""
    win_index = {int(w): wi for wi, w in enumerate(windows)}
    results: dict[str, pl.DataFrame] = {}
    for gi, group in enumerate(groups):
        wide_cols: dict[str, pl.Series] = {}
        for pgi, name, stats, group_windows, base in plan:
            if pgi != gi:
                continue
            for w in group_windows:
                row_sums = running[win_index[int(w)]]
                canon = _canonical_numpy(row_sums, stats, col_index, base)
                for stat in stats:
                    column = canon[f"__c_{stat}_{base}"]
                    wide_cols[f"__{stat}_{name}_{w}"] = pl.Series(column, dtype=pl.Float64)
        for pgi, name, stats, group_windows, ns in reg_plan:
            if pgi != gi:
                continue
            for w in group_windows:
                row_sums = running[win_index[int(w)]]
                ols = _ols_stat_numpy(row_sums, stats, col_index, ns)
                for stat in stats:
                    wide_cols[f"__{stat}_{name}_{w}"] = pl.Series(ols[stat], dtype=pl.Float64)
        piv = pl.DataFrame({"symbol": symbols, **wide_cols})
        wide = latest_frame.select(
            ["symbol", *[f"__pt_{name}" for name in group.points()]]
        ).join(piv, on="symbol", how="left")
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results
