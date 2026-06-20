"""Proof tests for the name-addressed, fingerprint-decoupled bus consume (docs/BUS_FEATURE_ACCESS.md §4).

All pure / in-process: a ``DictSchemaBackend`` stands in for Redis, so no live cluster is needed. The 12
tests are the decoupling proof — most importantly the regression guards: a version-bumped consumed feature
must be caught RED by the compat gate (B2), the consumer must retry-not-stop on an unresolved fingerprint
(B1), and the gate must fail closed on a missing contract (B3).
"""

from __future__ import annotations

import datetime as dt
import struct

import numpy as np
import pytest

from quantlib.bus.codec import HEADER_SIZE, decode_view, encode
from quantlib.bus.compat import (
    FeatureReq,
    IncompatibleSchema,
    MissingContract,
    assert_compatible,
    run_gate,
)
from quantlib.bus.publisher import BusPublisher
from quantlib.bus.registry import (
    DictSchemaBackend,
    RedisSchemaBackend,
    SchemaRegistry,
    UnknownSchema,
    schema_key,
)
from quantlib.bus.schema import BusField, BusSchema
from quantlib.bus.view import MissingFeature
from quantlib.strategy_core.feature_row import FeatureRow
from quantlib.strategy_core.models.single_name import MockMLModel
from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel
from strategies.smoke.strategy import MODEL_FOLD_FEATURES, SAMPLE_FEATURES

MINUTE = dt.datetime(2026, 6, 19, 14, 31, tzinfo=dt.timezone.utc)


def _schema(fields: list[tuple[str, str, str]]) -> BusSchema:
    """Build a schema from (group, name, version) triples; offsets are positional."""
    return BusSchema([BusField(group=g, name=n, offset=i, version=v) for i, (g, n, v) in enumerate(fields)])


def _registry_with(*schemas: BusSchema, compiled: BusSchema | None = None) -> SchemaRegistry:
    backend = DictSchemaBackend()
    registry = SchemaRegistry(
        backend, compiled_schema=compiled or schemas[0], max_retries=3, backoff_base_s=0.0
    )
    for schema in schemas:
        registry.publish(schema)
    return registry


def _frame(symbol: str, schema: BusSchema, values: dict[str, float]) -> bytes:
    return encode(symbol, MINUTE, values, schema)


class _OrderRecordingRedis:
    """A minimal fake Redis that records whether bus:schema:<fp> was SET before the first XADD (B1)."""

    def __init__(self, fingerprint: int) -> None:
        self._schema_key = schema_key(fingerprint)
        self.kv: dict[str, bytes] = {}
        self.xadds: list[str] = []
        self.set_before_first_xadd: bool | None = None

    def get(self, key: str) -> bytes | None:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> None:
        self.kv[key] = value.encode()

    def xadd(self, key: str, *args: object, **kwargs: object) -> None:
        if self.set_before_first_xadd is None:  # snapshot state at the FIRST xadd
            self.set_before_first_xadd = self._schema_key in self.kv
        self.xadds.append(key)


# Schema X: {A, B}.  Schema Y: a SUPERSET adding C0..C2 with A, B at DIFFERENT offsets.
SCHEMA_X = _schema([("mom", "A", "v1"), ("vol", "B", "v1")])
SCHEMA_Y = _schema(
    [("x", "C0", "v1"), ("mom", "A", "v1"), ("x", "C1", "v1"), ("vol", "B", "v1"), ("x", "C2", "v1")]
)


def test_additions_are_non_breaking() -> None:
    """Test 1 — the core decoupling proof: a consumer reading {A, B} gets identical values from X and Y."""
    registry = _registry_with(SCHEMA_X, SCHEMA_Y, compiled=SCHEMA_X)
    view_x = decode_view(_frame("AAA", SCHEMA_X, {"A": 1.0, "B": 2.0}), registry)
    view_y = decode_view(_frame("AAA", SCHEMA_Y, {"A": 1.0, "B": 2.0, "C0": 9.0, "C1": 9.0}), registry)
    assert SCHEMA_X.fingerprint != SCHEMA_Y.fingerprint
    assert view_y.fingerprint == SCHEMA_Y.fingerprint
    assert view_x.to_model_vector(["A", "B"]).tolist() == [1.0, 2.0]
    assert view_y.to_model_vector(["A", "B"]).tolist() == [1.0, 2.0]


def test_value_identical_restructure_is_a_nonevent() -> None:
    """Test 2 — same names, reordered offsets, new fingerprint: to_model_vector returns identical values."""
    restructured = _schema([("vol", "B", "v1"), ("mom", "A", "v1")])  # A, B swapped
    registry = _registry_with(SCHEMA_X, restructured, compiled=SCHEMA_X)
    view = decode_view(_frame("AAA", restructured, {"A": 7.0, "B": 8.0}), registry)
    assert restructured.fingerprint != SCHEMA_X.fingerprint
    assert view.to_model_vector(["A", "B"]).tolist() == [7.0, 8.0]


def test_missing_feature_errors_clearly() -> None:
    """Test 3 — a needed name absent from the frame raises MissingFeature (never a silent NaN)."""
    registry = _registry_with(SCHEMA_X)
    view = decode_view(_frame("AAA", SCHEMA_X, {"A": 1.0, "B": 2.0}), registry)
    with pytest.raises(MissingFeature, match="B_missing"):
        view.to_model_vector(["A", "B_missing"])
    with pytest.raises(MissingFeature):
        view.value("B_missing")


def test_length_misalignment_still_loud() -> None:
    """Test 4 — a header n_features that disagrees with the resolved schema raises (never index past)."""
    registry = _registry_with(SCHEMA_X)
    frame = bytearray(_frame("AAA", SCHEMA_X, {"A": 1.0, "B": 2.0}))
    n_off = struct.calcsize("<4sQq")  # bytes before the uint32 n_features field in the header
    struct.pack_into("<I", frame, n_off, 99)  # corrupt n_features to disagree with the schema
    with pytest.raises(ValueError, match="n_features mismatch"):
        decode_view(bytes(frame), registry)


def test_unknown_schema_retries_then_resolves() -> None:
    """Test 5 (B1) — an unresolved fingerprint retries-with-backoff and succeeds once published; a
    same-fingerprint-as-compiled missing key falls back to the compiled schema."""
    backend = DictSchemaBackend()
    registry = SchemaRegistry(backend, compiled_schema=SCHEMA_X, max_retries=4, backoff_base_s=0.0)
    # Y not yet published -> a single resolve raises UnknownSchema (recoverable signal).
    with pytest.raises(UnknownSchema):
        registry.resolve(SCHEMA_Y.fingerprint)
    # Publish it, then resolve_blocking succeeds (models the publish landing mid-poll).
    registry.publish(SCHEMA_Y)
    assert registry.resolve_blocking(SCHEMA_Y.fingerprint).fingerprint == SCHEMA_Y.fingerprint
    # A frame at the compiled fingerprint with NO published key still resolves (compiled fallback).
    bare = SchemaRegistry(DictSchemaBackend(), compiled_schema=SCHEMA_X)
    assert bare.resolve(SCHEMA_X.fingerprint).fingerprint == SCHEMA_X.fingerprint


def test_featureview_satisfies_featurerow_and_cores_match() -> None:
    """Test 6 — FeatureView is a FeatureRow; the single-name cores read identical values off it."""
    schema = _schema([("price", "vwap_deviation_30m", "v1"), ("mom", "ret_1m", "v1")])
    registry = _registry_with(schema)
    view = decode_view(_frame("AAA", schema, {"vwap_deviation_30m": -0.005, "ret_1m": 0.01}), registry)
    assert isinstance(view, FeatureRow)
    rev = VwapReversionModel(window_m=30)
    assert rev.predict(view).probability > 0.5  # below VWAP -> long
    mock = MockMLModel(["ret_1m"])
    # deterministic in (symbol, minute, folded feature) — stable, in [0, 1]
    assert 0.0 <= mock.predict(view).probability <= 1.0


def test_compat_gate_green_on_additions() -> None:
    """Test 7 — declared {A@v1, B@v1} is a subset of Y (which adds features) -> no raise."""
    declared = [FeatureReq("A", "v1"), FeatureReq("B", "v1")]
    assert_compatible(SCHEMA_Y, declared, strategy="smoke")  # no raise


def test_compat_gate_red_names_missing_feature() -> None:
    """Test 8 — a removed/renamed consumed feature -> IncompatibleSchema naming it."""
    declared = [FeatureReq("A", "v1"), FeatureReq("B", "v1")]
    candidate = _schema([("mom", "A", "v1")])  # B removed
    with pytest.raises(IncompatibleSchema) as exc:
        assert_compatible(candidate, declared, strategy="smoke")
    assert exc.value.missing == ["B"]


def test_contract_equals_model_input_list() -> None:
    """Test 9 (B3) — a strategy's declared names == the expected_names it feeds to_model_vector."""
    # the smoke contract is its model-fold constant; the sample read uses the same names.
    contract_names = [req.name for req in (FeatureReq(n, "v1") for n in MODEL_FOLD_FEATURES)]
    assert contract_names == list(MODEL_FOLD_FEATURES) == list(SAMPLE_FEATURES)


def test_version_bumped_consumed_feature_is_red() -> None:
    """Test 10 (B2, the regression test) — a consumed feature at a NEW version is RED; a value-identical
    annotation auto-passes it."""
    declared = [FeatureReq("A", "v1"), FeatureReq("B", "v1")]
    bumped = _schema([("mom", "A", "v1"), ("vol", "B", "v2")])  # B re-versioned (re-computed)
    with pytest.raises(IncompatibleSchema) as exc:
        assert_compatible(bumped, declared, strategy="smoke")
    assert exc.value.version_changed == [("B", "v1", "v2")]
    assert exc.value.missing == []
    # value-identical fast-path: producer annotates B's v2 as value-identical -> auto-pass.
    assert_compatible(bumped, declared, strategy="smoke", value_identical_bumps={"B": "v2"})


def test_publish_then_emit_ordering() -> None:
    """Test 11 (B1) — the producer SETs+confirms the schema BEFORE the first frame of the fingerprint."""
    fake = _OrderRecordingRedis(SCHEMA_X.fingerprint)
    pub = BusPublisher.__new__(BusPublisher)  # bypass redis.from_url; inject the fake
    pub._redis = fake  # type: ignore[attr-defined]
    pub._schema = SCHEMA_X  # type: ignore[attr-defined]
    pub._maxlen = 240  # type: ignore[attr-defined]
    pub._prefix = "fv"  # type: ignore[attr-defined]
    pub._registry = SchemaRegistry(RedisSchemaBackend(fake), compiled_schema=SCHEMA_X)  # type: ignore[attr-defined,arg-type]
    pub._schema_published = False  # type: ignore[attr-defined]
    pub.publish("AAA", MINUTE, {"A": 1.0, "B": 2.0})
    assert fake.set_before_first_xadd is True  # schema SET landed BEFORE the first frame
    assert fake.xadds == ["fv:AAA"]


def test_gate_fails_closed_on_missing_contract() -> None:
    """Test 12 (B3) — an expected-live strategy with no published contract -> MissingContract, not green."""
    contracts = {
        "smoke": [FeatureReq("A", "v1")],
        "reversion": [FeatureReq("A", "v1")],
    }
    with pytest.raises(MissingContract) as exc:
        run_gate(SCHEMA_Y, contracts, ["smoke", "reversion", "overnight_beta"])
    assert exc.value.strategies == ["overnight_beta"]
    # with all three present and compatible -> GREEN (no raise)
    contracts["overnight_beta"] = [FeatureReq("B", "v1")]
    run_gate(SCHEMA_Y, contracts, ["smoke", "reversion", "overnight_beta"])


def test_view_present_but_nan_is_not_this_layers_job() -> None:
    """B5 — a present-but-NaN feature passes has(name) and is returned as NaN (finiteness != presence)."""
    registry = _registry_with(SCHEMA_X)
    view = decode_view(_frame("AAA", SCHEMA_X, {"A": 1.0}), registry)  # B omitted -> NaN
    assert view.has("B")
    assert np.isnan(view.to_model_vector(["A", "B"])[1])


def test_header_size_unchanged() -> None:
    """The wire frame layout is unchanged — the decouple is consumer-side, not a wire change."""
    assert HEADER_SIZE == 26
