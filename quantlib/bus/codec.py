"""The wire codec: pack a feature vector into a compact binary frame and unpack it zero-copy.

Frame layout (little-endian), designed so decode is a header parse + a numpy VIEW over the payload —
no per-feature deserialization, no allocation of the float data:

    magic        4 bytes   b"FVB1"
    fingerprint  uint64    schema identity (must match the consumer's BusSchema)
    minute_us    int64     UTC epoch microseconds of the bar minute
    n_features   uint32    payload length (== schema.n_features)
    symbol_len   uint16    UTF-8 byte length of the symbol
    symbol       symbol_len bytes
    payload      n_features * float64   feature values in canonical schema-offset order (NaN = absent)

Encode accepts either a name->value mapping (missing/unknown names -> NaN / ignored) or a pre-ordered
numpy array (the fast producer path: no per-name lookup). Decode validates magic + fingerprint so a
schema-mismatched frame fails loudly instead of silently misaligning offsets.
"""

from __future__ import annotations

import datetime as dt
import struct
from collections.abc import Mapping

import numpy as np

from quantlib.bus.registry import SchemaRegistry
from quantlib.bus.schema import BusSchema
from quantlib.bus.vector import FeatureVector
from quantlib.bus.view import FeatureView

MAGIC = b"FVB1"
_HEADER_FMT = "<4sQqIH"
HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 26
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _epoch_us(minute: dt.datetime | int) -> int:
    """UTC epoch microseconds. Accepts an int (already-µs) or a datetime (naive treated as UTC)."""
    if isinstance(minute, int):
        return minute
    if minute.tzinfo is None:
        minute = minute.replace(tzinfo=dt.timezone.utc)
    delta = minute - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _us_to_datetime(epoch_us: int) -> dt.datetime:
    return _EPOCH + dt.timedelta(microseconds=epoch_us)


def _to_array(values: Mapping[str, float] | np.ndarray, schema: BusSchema) -> np.ndarray:
    """Canonical-order float64 array. A mapping fills by offset (missing -> NaN, unknown names ignored);
    a numpy array is used as-is after a length check (the zero-lookup producer path)."""
    if isinstance(values, np.ndarray):
        if values.shape != (schema.n_features,):
            raise ValueError(f"array length {values.shape} != schema.n_features {schema.n_features}")
        return np.ascontiguousarray(values, dtype="<f8")
    array = np.full(schema.n_features, np.nan, dtype="<f8")
    for name, value in values.items():
        if value is not None and schema.has(name):
            array[schema.offset(name)] = value
    return array


def encode(
    symbol: str,
    minute: dt.datetime | int,
    values: Mapping[str, float] | np.ndarray,
    schema: BusSchema,
) -> bytes:
    """Pack one (symbol, minute) feature vector into a bus frame."""
    array = _to_array(values, schema)
    symbol_bytes = symbol.encode("utf-8")
    header = struct.pack(
        _HEADER_FMT, MAGIC, schema.fingerprint, _epoch_us(minute), schema.n_features, len(symbol_bytes)
    )
    return header + symbol_bytes + array.tobytes()


def decode(buf: bytes, schema: BusSchema) -> FeatureVector:
    """Unpack a bus frame into a FeatureVector. The float payload is a zero-copy view over ``buf``."""
    magic, fingerprint, minute_us, n_features, symbol_len = struct.unpack_from(_HEADER_FMT, buf, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r} (not a feature-vector-bus frame)")
    if fingerprint != schema.fingerprint:
        raise ValueError(
            f"schema fingerprint mismatch: frame={fingerprint:#018x} schema={schema.fingerprint:#018x} "
            "(producer and consumer feature sets differ)"
        )
    if n_features != schema.n_features:
        raise ValueError(f"n_features mismatch: frame={n_features} schema={schema.n_features}")
    symbol_start = HEADER_SIZE
    payload_start = symbol_start + symbol_len
    symbol = buf[symbol_start:payload_start].decode("utf-8")
    array = np.frombuffer(buf, dtype="<f8", count=n_features, offset=payload_start)
    return FeatureVector(schema, symbol, _us_to_datetime(minute_us), array, fingerprint)


def decode_view(buf: bytes, registry: SchemaRegistry, *, blocking: bool = True) -> FeatureView:
    """Unpack a frame into a ``FeatureView``, resolving its schema BY FINGERPRINT (resolve-not-reject).

    The fingerprint is no longer required to equal a single compiled schema — the registry resolves the
    frame's own schema (cached per fingerprint). The magic check and the n_features length guard STAY: a
    non-``FVB1`` frame is corruption, and a header n_features that disagrees with the resolved schema is a
    genuine misalignment we must never index past. Per-name alignment is then validated at access time
    (``FeatureView.value`` / ``to_model_vector`` raise ``MissingFeature``), so the loud-failure property is
    preserved exactly where it matters and dropped only for a benign superset. ``blocking`` uses the
    registry's retry-with-backoff (B1) so a not-yet-propagated publish self-heals instead of raising.
    """
    magic, fingerprint, minute_us, n_features, symbol_len = struct.unpack_from(_HEADER_FMT, buf, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r} (not a feature-vector-bus frame)")
    schema = registry.resolve_blocking(fingerprint) if blocking else registry.resolve(fingerprint)
    if n_features != schema.n_features:
        raise ValueError(
            f"n_features mismatch: frame={n_features} resolved-schema={schema.n_features} "
            f"(fingerprint {fingerprint:#018x})"
        )
    symbol_start = HEADER_SIZE
    payload_start = symbol_start + symbol_len
    symbol = buf[symbol_start:payload_start].decode("utf-8")
    array = np.frombuffer(buf, dtype="<f8", count=n_features, offset=payload_start)
    return FeatureView(schema, symbol, _us_to_datetime(minute_us), array, fingerprint)
