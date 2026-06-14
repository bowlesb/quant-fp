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

from abc import abstractmethod

import polars as pl

from quantlib.features import _phase
from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.latest import pivot_stat, rust_reductions, rust_windowed_sums

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

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated BACKFILL form: rolling_*_by over every minute (source of truth)."""
        frame = ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"])
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
        frame = ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"])
        reduced, regressions = self.reduced(), self.regressions()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _, _) in reduced.items()])
        if regressions:
            frame = frame.with_columns(
                [col for name, (x, y, _, _) in regressions.items() for col in _ols_derived(name, x, y)]
            )
        latest = frame["minute"].max()
        wide = frame.filter(pl.col("minute") == latest).select(
            ["symbol", *[expr.alias(f"__pt_{name}") for name, expr in self.points().items()]]
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
    latest = frame["minute"].max()

    derived, extra, value_cols, plan, reg_plan, windows = build_plan(groups)
    with _phase.phase("batch.derive(value cols)"):
        frame = frame.with_columns(derived)
    with _phase.phase("batch.derive(sq+presence)"):
        frame = frame.with_columns(extra)
    long = rust_windowed_sums(frame, value_cols, windows)

    with _phase.phase("batch.assemble(pivot+join)"):
        return assemble_from_long(groups, long, frame.filter(pl.col("minute") == latest), latest, plan, reg_plan)


def assemble_from_long(
    groups: list[ReductionGroup],
    long: pl.DataFrame,
    latest_frame: pl.DataFrame,
    latest: object,
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
) -> dict[str, pl.DataFrame]:
    """Build each group's feature frame from a LONG (symbol, window, <value-col sums>) frame + the latest
    minute's rows (for points). SHARED by the live-batch path (``long`` from the Rust kernel) and the
    incremental path (``long`` from the running-sum state) — so the canonical algebra and ``assemble()`` are
    the SAME code in both; only the source of the sums differs. ``latest`` is the minute stamped on output."""
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
        glong = long.select(["symbol", "window", *canon])
        wide = latest_frame.select(
            ["symbol", *[expr.alias(f"__pt_{name}") for name, expr in group.points().items()]]
        )
        for name, (_, stats, windows) in group.reduced().items():
            for stat in stats:
                wide = wide.join(
                    pivot_stat(glong, f"__c_{stat}_{name}", f"__{stat}_{name}_{{w}}", windows), on="symbol", how="left"
                )
        for name, (_, _, stats, windows) in group.regressions().items():
            for stat in stats:
                wide = wide.join(
                    pivot_stat(glong, f"__c_{stat}_{name}", f"__{stat}_{name}_{{w}}", windows), on="symbol", how="left"
                )
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results
