"""FP0 tests: registry contract, engine validation, conformance gate, introspection.

These cover the FP0 exit criteria (FEATURE_PLATFORM.md §7): metadata complete, columns equal the
registry, the group-conformance gate FAILS on each break type and PASSES clean, introspection
catches degenerate/out-of-range features.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

BASE_MINUTE = datetime(2026, 6, 12, 8, 0)

from quantlib.features import (
    REGISTRY,
    BatchContext,
    ContractError,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    IntrospectionError,
    RegistrationError,
    Registry,
    assert_deterministic,
    assert_sane,
    introspect,
    run_all,
    run_group,
)

EXPECTED_FEATURES = {
    "ret_1m",
    "ret_5m",
    "ret_30m",
    "signed_volume_1m",
    "trade_freq_1m",
    "trade_rate_accel_1m",
}


def make_minute_agg(symbols: tuple[str, ...] = ("AAA", "BBB"), n: int = 200) -> pl.DataFrame:
    """A synthetic complete minute grid: rising close, varying trade counts and signed volume."""
    rows = [
        {
            "symbol": symbol,
            "minute": BASE_MINUTE + timedelta(minutes=i),
            "close": 100.0 + i * 0.1 + (0.0 if symbol == "AAA" else 1.0),
            "n_trades": 10 + (i % 7),
            "signed_volume": float((i % 5) - 2) * 100.0,
        }
        for symbol in symbols
        for i in range(n)
    ]
    return pl.DataFrame(rows)


def make_ctx(n: int = 200) -> BatchContext:
    return BatchContext(frames={"minute_agg": make_minute_agg(n=n)})


# --- module-level groups used by the conformance gate (each breaks one contract) ---


class GoodGroup(FeatureGroup):
    name = "good_probe"
    version = "1.0.0"
    owner = "qa"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="close_level",
                description="The raw close price as of the minute open (a trivial probe feature).",
                dtype="Float64",
            )
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return ctx.frame("minute_agg").select(
            ["symbol", "minute", pl.col("close").cast(pl.Float64).alias("close_level")]
        )


class UndeclaredColumnGroup(GoodGroup):
    name = "undeclared_probe"

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return super().compute(ctx).with_columns(pl.lit(1.0).alias("sneaky_extra"))


class OutOfRangeGroup(FeatureGroup):
    name = "range_probe"
    version = "1.0.0"
    owner = "qa"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="bounded_probe",
                description="A probe feature declared to live in [0, 1] but emitted out of range.",
                dtype="Float64",
                valid_range=(0.0, 1.0),
            )
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return ctx.frame("minute_agg").select(
            ["symbol", "minute", pl.lit(2.0).alias("bounded_probe")]
        )


class NonDeterministicGroup(FeatureGroup):
    name = "nondet_probe"
    version = "1.0.0"
    owner = "qa"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    _counter = 0

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="nondet_probe_value",
                description="A probe feature whose value changes on each call (non-deterministic).",
                dtype="Float64",
            )
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        NonDeterministicGroup._counter += 1
        return ctx.frame("minute_agg").select(
            ["symbol", "minute", pl.lit(float(NonDeterministicGroup._counter)).alias("nondet_probe_value")]
        )


class ShortDescGroup(GoodGroup):
    name = "shortdesc_probe"

    def declare(self) -> list[FeatureSpec]:
        return [FeatureSpec(name="close_level", description="too short", dtype="Float64")]


class DupFeatureA(GoodGroup):
    name = "dup_a"


class DupFeatureB(GoodGroup):
    name = "dup_b"  # same feature name "close_level" -> collision


# --- registry / metadata ---


def test_real_groups_registered_with_unique_complete_metadata() -> None:
    names = REGISTRY.feature_names()
    assert set(names) == EXPECTED_FEATURES
    assert len(names) == len(set(names))  # no duplicate feature names
    catalog = REGISTRY.catalog()
    assert catalog.height == len(EXPECTED_FEATURES)
    assert (catalog["description"].str.len_chars() >= 40).all()


def test_columns_equal_registry() -> None:
    vector = run_all(REGISTRY.groups(), make_ctx())
    feature_cols = set(vector.columns) - {"symbol", "minute"}
    assert feature_cols == EXPECTED_FEATURES


# --- engine correctness ---


def test_returns_are_correct() -> None:
    vector = run_all(REGISTRY.groups(), make_ctx())
    minute_100 = BASE_MINUTE + timedelta(minutes=100)
    row = vector.filter((pl.col("symbol") == "AAA") & (pl.col("minute") == minute_100)).row(0, named=True)
    # close rises by 0.1/min; ret_1m = (c_100 - c_99)/c_99
    expected = 0.1 / (100.0 + 99 * 0.1)
    assert row["ret_1m"] == pytest.approx(expected, rel=1e-9)
    # first minute has no prior bar -> warmup null for both symbols
    assert vector.filter(pl.col("minute") == BASE_MINUTE)["ret_1m"].null_count() == 2


# --- conformance gate: PASSES clean, FAILS on each break type ---


def test_conformance_passes_clean() -> None:
    group = GoodGroup()
    run_group(group, make_ctx())  # validates output contract
    assert_deterministic(group, make_ctx())


def test_conformance_fails_undeclared_column() -> None:
    with pytest.raises(ContractError):
        run_group(UndeclaredColumnGroup(), make_ctx())


def test_conformance_fails_out_of_range() -> None:
    with pytest.raises(ContractError):
        run_group(OutOfRangeGroup(), make_ctx())


def test_conformance_fails_nondeterministic() -> None:
    with pytest.raises(ContractError):
        assert_deterministic(NonDeterministicGroup(), make_ctx())


def test_conformance_fails_short_description() -> None:
    with pytest.raises(RegistrationError):
        Registry().register(ShortDescGroup)


def test_conformance_fails_duplicate_feature_name() -> None:
    fresh = Registry()
    fresh.register(DupFeatureA)
    with pytest.raises(RegistrationError):
        fresh.register(DupFeatureB)


def test_conformance_fails_duplicate_group_name() -> None:
    fresh = Registry()
    fresh.register(GoodGroup)
    with pytest.raises(RegistrationError):
        fresh.register(GoodGroup)


# --- introspection ---


def test_introspect_passes_on_good_data() -> None:
    groups = REGISTRY.groups()
    vector = run_all(groups, make_ctx())
    specs = [spec for group in groups for spec in group.declare()]
    report = assert_sane(vector, specs)
    assert report.height == len(EXPECTED_FEATURES)
    assert not report["degenerate"].any()


def test_introspect_catches_degenerate() -> None:
    spec = FeatureSpec(
        name="const_probe",
        description="A constant probe feature that should be flagged as degenerate by introspection.",
        dtype="Float64",
    )
    frame = pl.DataFrame({"symbol": ["A", "A"], "minute": [0, 1], "const_probe": [1.0, 1.0]})
    with pytest.raises(IntrospectionError):
        assert_sane(frame, [spec])


def test_introspect_catches_range_violation() -> None:
    spec = FeatureSpec(
        name="bounded_probe",
        description="A probe feature declared in [0, 1] but containing an out-of-range value.",
        dtype="Float64",
        valid_range=(0.0, 1.0),
    )
    frame = pl.DataFrame({"symbol": ["A", "A"], "minute": [0, 1], "bounded_probe": [0.5, 9.0]})
    with pytest.raises(IntrospectionError):
        assert_sane(frame, [spec])
