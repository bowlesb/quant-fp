"""Storage-dtype rule: the declared FeatureSpec contract maps to a sensible narrow on-disk dtype.

Pins the standardized declaration → storage mapping: real-valued features store Float32, true 0/1 flags
store nullable UInt8, the integer calendar features store small ints, an explicit ``storage=`` declaration
wins, and NOTHING needs Float64 on disk. This is the guard that keeps the ~54% space win correct as new
groups register.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, storage_dtype
from quantlib.features.registry import REGISTRY


def test_explicit_storage_declaration_wins() -> None:
    spec = FeatureSpec(name="x", description="d" * 40, dtype="Float64", storage="Int8")
    assert storage_dtype(spec) == pl.Int8


def test_flag_features_store_uint8() -> None:
    for name in ("is_bullish", "sector_is_technology", "above_pivot", "outperforming_spy_5m"):
        spec = FeatureSpec(name=name, description="d" * 40, dtype="Float64", valid_range=(0.0, 1.0))
        assert storage_dtype(spec) == pl.UInt8, name


def test_continuous_in_unit_range_is_not_a_flag() -> None:
    # a [0,1] feature WITHOUT a flag-style name stays Float32 (e.g. ratios/percentiles)
    spec = FeatureSpec(name="efficiency_ratio_5m", description="d" * 40, dtype="Float64", valid_range=(-0.01, 1.01))
    assert storage_dtype(spec) == pl.Float32


def test_calendar_integers_store_small_ints() -> None:
    expected = {"day_of_week": pl.UInt8, "week_of_month": pl.UInt8,
                "minute_of_day_et": pl.UInt16, "minutes_since_open": pl.Int16}
    for name, dtype in expected.items():
        spec = FeatureSpec(name=name, description="d" * 40, dtype="Float64", valid_range=(0.0, 1440.0))
        assert storage_dtype(spec) == dtype, name


def test_no_registered_feature_needs_float64() -> None:
    # every real declared feature narrows below Float64 — the whole point of the survey
    for _group, spec in REGISTRY.feature_specs():
        assert storage_dtype(spec) in (pl.Float32, pl.UInt8, pl.UInt16, pl.Int16, pl.Int8), spec.name
