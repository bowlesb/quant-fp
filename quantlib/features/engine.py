"""Run feature groups and enforce their declared output contract.

The engine is what makes the registry trustworthy: a group's ``compute()`` output is validated
against its ``declare()`` contract — exactly the declared feature columns (plus the keys), correct
dtype, and values inside the declared range — and a violation RAISES. This is the structural guard
(FEATURE_PLATFORM.md §3.6) that one agent's group cannot corrupt the store.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import KEY_COLUMNS, BatchContext, FeatureGroup

_DTYPE_MAP: dict[str, pl.DataType] = {
    "Float64": pl.Float64,
    "Float32": pl.Float32,
    "Int64": pl.Int64,
    "Int32": pl.Int32,
    "Boolean": pl.Boolean,
}


class ContractError(Exception):
    """Raised when a group's compute output violates its declared FeatureSpec contract."""


def run_group(group: FeatureGroup, ctx: BatchContext, validate: bool = True) -> pl.DataFrame:
    """Compute one group. With ``validate`` (the conformance-gate default) the output is checked
    against the declared contract and a violation RAISES. Production compute over real data passes
    ``validate=False`` — a real data anomaly (e.g. a split) is a data issue surfaced by
    introspection/parity, not a reason to crash the run."""
    out = group.compute(ctx)
    if validate:
        _validate_output(group, out)
    return out


def run_all(groups: list[FeatureGroup], ctx: BatchContext, validate: bool = True) -> pl.DataFrame:
    """Compute every group and join the outputs into one wide vector frame."""
    vector: pl.DataFrame | None = None
    for group in groups:
        out = run_group(group, ctx, validate=validate)
        vector = out if vector is None else vector.join(out, on=list(KEY_COLUMNS), how="full", coalesce=True)
    return vector if vector is not None else pl.DataFrame()


def _validate_output(group: FeatureGroup, out: pl.DataFrame) -> None:
    specs = group.declare()
    expected = set(KEY_COLUMNS) | {spec.name for spec in specs}
    actual = set(out.columns)
    if actual != expected:
        undeclared = actual - expected
        missing = expected - actual
        raise ContractError(
            f"{group.name}: output columns {sorted(actual)} != declared {sorted(expected)} "
            f"(undeclared={sorted(undeclared)}, missing={sorted(missing)})"
        )
    for spec in specs:
        expected_dtype = _DTYPE_MAP.get(spec.dtype)
        if expected_dtype is not None and out.schema[spec.name] != expected_dtype:
            raise ContractError(
                f"{group.name}.{spec.name}: dtype {out.schema[spec.name]} != declared {spec.dtype}"
            )
        if spec.valid_range is not None:
            _check_range(group.name, out, spec.name, spec.valid_range)


def _check_range(
    group_name: str, out: pl.DataFrame, name: str, valid_range: tuple[float | None, float | None]
) -> None:
    low, high = valid_range
    column = pl.col(name)
    violation = column.is_not_null()
    if name in out.columns and out.schema[name].is_float():
        violation = violation & column.is_not_nan()
    bounds = pl.lit(False)
    if low is not None:
        bounds = bounds | (column < low)
    if high is not None:
        bounds = bounds | (column > high)
    n_bad = out.select((violation & bounds).sum()).item()
    if n_bad:
        raise ContractError(
            f"{group_name}.{name}: {n_bad} value(s) outside declared range {valid_range}"
        )


def assert_deterministic(group: FeatureGroup, ctx: BatchContext) -> None:
    """Run a group twice and assert bit-identical output (FEATURE_PLATFORM.md R12)."""
    first = run_group(group, ctx)
    second = run_group(group, ctx)
    if not first.sort(list(KEY_COLUMNS)).equals(second.sort(list(KEY_COLUMNS))):
        raise ContractError(f"{group.name}: compute is non-deterministic")
