"""Tests for the feature-vector bus codec, schema and accessor — the contract strategy containers
trust. Covers round-trip fidelity, NaN/absent handling, the µs-exact minute, zero-copy decode, schema
fingerprint mismatch detection, nested group.feature access, and the registry-built full schema.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from quantlib.bus.codec import HEADER_SIZE, decode, encode
from quantlib.bus.schema import BusField, BusSchema, default_schema

MINUTE = dt.datetime(2026, 6, 15, 14, 31, tzinfo=dt.timezone.utc)


def _toy_schema() -> BusSchema:
    """A small hand-built schema so the codec is tested independently of the live registry."""
    fields = [
        BusField(group="momentum", name="momentum_fast_1", offset=0, version="1.0.0"),
        BusField(group="momentum", name="momentum_slow_1", offset=1, version="1.0.0"),
        BusField(group="volatility", name="realized_vol_5m", offset=2, version="2.1.0"),
    ]
    return BusSchema(fields)


def test_round_trip_mapping() -> None:
    schema = _toy_schema()
    frame = encode("AAPL", MINUTE, {"momentum_fast_1": 1.5, "realized_vol_5m": 0.02}, schema)
    vec = decode(frame, schema)
    assert vec.symbol == "AAPL"
    assert vec.minute == MINUTE
    assert vec.value("momentum_fast_1") == 1.5
    assert vec.value("realized_vol_5m") == 0.02
    # absent feature -> NaN (not silently 0)
    assert np.isnan(vec.value("momentum_slow_1"))


def test_round_trip_array_path() -> None:
    schema = _toy_schema()
    array = np.array([3.0, 4.0, 5.0], dtype="<f8")
    vec = decode(encode("SPY", MINUTE, array, schema), schema)
    assert vec.array.tolist() == [3.0, 4.0, 5.0]


def test_minute_microsecond_exact() -> None:
    schema = _toy_schema()
    minute = dt.datetime(2026, 6, 15, 9, 30, 0, 123456, tzinfo=dt.timezone.utc)
    vec = decode(encode("AAPL", minute, {}, schema), schema)
    assert vec.minute == minute


def test_unknown_names_ignored_and_naive_minute_is_utc() -> None:
    schema = _toy_schema()
    naive = dt.datetime(2026, 6, 15, 14, 31)
    frame = encode("AAPL", naive, {"not_a_feature": 9.9, "momentum_fast_1": 1.0}, schema)
    vec = decode(frame, schema)
    assert vec.minute == MINUTE  # naive interpreted as UTC
    assert vec.value("momentum_fast_1") == 1.0


def test_decode_is_zero_copy_view() -> None:
    schema = _toy_schema()
    frame = bytearray(encode("AAPL", MINUTE, np.array([1.0, 2.0, 3.0], dtype="<f8"), schema))
    vec = decode(bytes(frame), schema)
    # the payload array is a view over the buffer, not a parsed copy
    assert vec.array.base is not None


def test_fingerprint_mismatch_raises() -> None:
    schema = _toy_schema()
    frame = encode("AAPL", MINUTE, {"momentum_fast_1": 1.0}, schema)
    other = BusSchema([BusField(group="g", name="x", offset=0, version="1.0.0")])
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        decode(frame, other)


def test_bad_magic_raises() -> None:
    schema = _toy_schema()
    with pytest.raises(ValueError, match="bad magic"):
        decode(b"XXXX" + bytes(HEADER_SIZE + 24), schema)


def test_nested_group_attribute_access() -> None:
    schema = _toy_schema()
    vec = decode(encode("AAPL", MINUTE, {"momentum_fast_1": 7.0, "realized_vol_5m": 0.03}, schema), schema)
    assert vec.momentum.momentum_fast_1 == 7.0
    assert vec.volatility.realized_vol_5m == 0.03
    assert vec["momentum_fast_1"] == 7.0


def test_nested_access_validates_group_membership() -> None:
    schema = _toy_schema()
    vec = decode(encode("AAPL", MINUTE, {}, schema), schema)
    with pytest.raises(AttributeError, match="has no feature"):
        _ = vec.momentum.realized_vol_5m  # belongs to volatility, not momentum
    with pytest.raises(AttributeError, match="no feature group"):
        _ = vec.not_a_group.x


def test_ref_and_to_dict() -> None:
    schema = _toy_schema()
    vec = decode(encode("AAPL", MINUTE, {"realized_vol_5m": 0.05}, schema), schema)
    ref = vec.ref("realized_vol_5m")
    assert ref.group == "volatility" and ref.name == "realized_vol_5m" and ref.value == 0.05
    assert set(vec.to_dict().keys()) == {"momentum_fast_1", "momentum_slow_1", "realized_vol_5m"}


def test_schema_from_registry_is_deterministic_and_complete() -> None:
    schema_a = BusSchema.from_registry()
    schema_b = BusSchema.from_registry()
    assert schema_a.n_features == schema_b.n_features
    assert schema_a.n_features > 100  # the real platform has hundreds of features
    assert schema_a.fingerprint == schema_b.fingerprint  # stable across builds
    assert default_schema().fingerprint == schema_a.fingerprint


def test_fingerprint_is_clean_engine_flag_conditional(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clean-engine fingerprint break is FP_CLEAN_ENGINE-conditional (the deploy invariant): flag OFF
    reproduces the OLD fingerprint EXACTLY (so a rollback to the old engine reuses the same store/bus contract),
    flag ON folds in the engine-version tag → a new, stable, distinct fingerprint (a clean v= partition + recal
    signal). Pinned to the exact pre-arm value so a future change to the OLD schema fails loudly here."""
    _OLD_FINGERPRINT = 0x204F9EE42521B36F  # the pre-clean-engine bus/store fingerprint (frozen contract)

    monkeypatch.delenv("FP_CLEAN_ENGINE", raising=False)
    flag_off = BusSchema.from_registry().fingerprint
    assert (
        flag_off == _OLD_FINGERPRINT
    ), f"flag-OFF fingerprint drifted: {flag_off:#018x} != {_OLD_FINGERPRINT:#018x}"

    monkeypatch.setenv("FP_CLEAN_ENGINE", "1")
    flag_on = BusSchema.from_registry().fingerprint
    assert flag_on != _OLD_FINGERPRINT, "flag-ON fingerprint did not break from the OLD engine's"
    assert flag_on == BusSchema.from_registry().fingerprint, "flag-ON fingerprint not stable across builds"

    # and OFF again restores the old fingerprint (the rollback path is reversible, not one-way).
    monkeypatch.delenv("FP_CLEAN_ENGINE", raising=False)
    assert BusSchema.from_registry().fingerprint == _OLD_FINGERPRINT


def test_full_schema_round_trip() -> None:
    schema = default_schema()
    names = schema.names()
    payload = {names[0]: 1.0, names[10]: 2.0, names[-1]: 3.0}
    vec = decode(encode("NVDA", MINUTE, payload, schema), schema)
    assert vec.symbol == "NVDA"
    assert vec.value(names[0]) == 1.0
    assert vec.value(names[-1]) == 3.0
    assert vec.array.shape == (schema.n_features,)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
