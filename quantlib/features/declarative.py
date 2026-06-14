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

from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.latest import pivot_stat, rust_reductions

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


class ReductionGroup(FeatureGroup):
    """Base for a windowed-reduction feature group. Set ``windows`` + ``reduce_input`` and implement
    ``reduced()`` / ``points()`` / ``assemble()``; ``compute()`` and ``compute_latest()`` are generated."""

    windows: tuple[int, ...]
    reduce_input: str = "minute_agg"

    @abstractmethod
    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...]]]:
        """{column_name: (expr_over_input, stats)} — the values to reduce and which stats each needs."""

    def points(self) -> dict[str, pl.Expr]:
        """{name: expr_over_input} — at-T scalar columns referenced via pt_() in assemble(). Default none."""
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
        reduced = self.reduced()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _) in reduced.items()])
        mats: list[pl.Expr] = []
        for name, (_, stats) in reduced.items():
            source = pl.col(f"__d_{name}")
            for w in self.windows:
                size = f"{w}m"
                if "mean" in stats:
                    mats.append(source.rolling_mean_by("minute", window_size=size).over("symbol").alias(f"__mean_{name}_{w}"))
                if "std" in stats:
                    mats.append(source.rolling_std_by("minute", window_size=size).over("symbol").alias(f"__std_{name}_{w}"))
                if "sum" in stats:
                    mats.append(source.rolling_sum_by("minute", window_size=size).over("symbol").alias(f"__sum_{name}_{w}"))
        frame = frame.with_columns([expr.alias(f"__pt_{name}") for name, expr in self.points().items()])
        frame = frame.with_columns(mats)
        feats = self.assemble()
        return frame.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()]).select(
            ["symbol", "minute", *self._feature_names()]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated LIVE form: aggregate-at-T via the Rust reduction kernel, one row per symbol."""
        frame = ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"])
        reduced = self.reduced()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _) in reduced.items()])
        latest = frame["minute"].max()
        wide = frame.filter(pl.col("minute") == latest).select(
            ["symbol", *[expr.alias(f"__pt_{name}") for name, expr in self.points().items()]]
        )
        for name, (_, stats) in reduced.items():
            long = rust_reductions(frame, f"__d_{name}", self.windows)
            for stat in stats:
                wide = wide.join(pivot_stat(long, stat, f"__{stat}_{name}_{{w}}", self.windows), on="symbol", how="left")
        feats = self.assemble()
        return (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *self._feature_names()])
        )
