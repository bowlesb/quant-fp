"""Feature-vector bus: serialize computed feature vectors and stream them to strategy containers.

The bus is the CONSUME stage of the platform spine (acquire -> materialize -> consume). When the live
pipeline computes a feature vector for a (symbol, minute), it is packed into a compact, versioned binary
frame and published to a Redis stream. Independent strategy containers subscribe, decode with ZERO
per-message parsing overhead (the float payload is a zero-copy view), and address features by name —
``vector.value("momentum_fast_1")`` or ``vector.momentum.momentum_fast_1`` — without caring how fast or
correct the (de)serialization is. That correctness/speed is THIS package's job, proven by tests.

Public API:
    BusSchema      - the canonical (group, feature) -> offset layout + a stable fingerprint
    encode/decode  - the wire codec (header + packed float64 array)
    FeatureVector  - the decoded accessor (name-indexed + nested group.feature attribute access)
"""
from __future__ import annotations

from quantlib.bus.codec import decode, encode
from quantlib.bus.schema import BusField, BusSchema
from quantlib.bus.vector import FeatureRef, FeatureVector

__all__ = [
    "BusSchema",
    "BusField",
    "encode",
    "decode",
    "FeatureVector",
    "FeatureRef",
]
