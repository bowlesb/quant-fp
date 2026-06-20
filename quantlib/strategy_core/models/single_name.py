"""A model-like signal interface plus a deterministic mock, so day-2 a real model drops in unchanged.

``Model`` is the contract a strategy consumes: ``predict(vector) -> Prediction`` where ``Prediction``
carries a probability in [0, 1] (e.g. P(up over the horizon)). The smoke strategy bets when the
probability clears a threshold — exactly how it will treat a trained classifier.

``MockMLModel`` returns a deterministic-but-varied pseudo-probability derived ONLY from the vector's
identity (symbol + bar minute) and, optionally, a couple of named feature values folded into the hash.
It uses NO wall-clock time and NO RNG — given the same FeatureVector it always returns the same
probability, so it is safe on a feature-time path and reproducible in tests. It is NOT alpha; it exists
to exercise the betting logic with a signal that varies across symbols and minutes.
"""
from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from quantlib.strategy_core.feature_row import FeatureRow


@dataclass(frozen=True)
class Prediction:
    """A model's output for one (symbol, minute) vector: a probability and the model's name."""

    symbol: str
    probability: float
    model: str


class Model(Protocol):
    """The interface a strategy consumes. A real trained model implements the same ``predict``."""

    def predict(self, vector: FeatureRow) -> Prediction: ...


def _hash_to_unit_interval(payload: bytes) -> float:
    """Map bytes -> a uniform-ish float in [0, 1) deterministically (first 8 bytes of blake2b)."""
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    raw = struct.unpack("<Q", digest)[0]
    return raw / float(1 << 64)


class MockMLModel:
    """Deterministic pseudo-probability from (symbol, minute[, feature values]). No wall-clock, no RNG.

    The probability is stable per (symbol, minute): the same vector always yields the same number, so it
    is reproducible and safe in feature-time code. ``feature_names`` (if given) fold those feature values
    into the hash so the signal also moves with the data, not just identity — still fully deterministic.
    Non-finite feature values are coerced to 0.0 before hashing so a NaN cell can never crash predict.
    """

    name = "mock_ml"

    def __init__(self, feature_names: Sequence[str] = ()) -> None:
        self._feature_names = list(feature_names)

    def predict(self, vector: FeatureRow) -> Prediction:
        minute_us = int(vector.minute.timestamp() * 1_000_000)
        parts = [vector.symbol.encode("utf-8"), struct.pack("<q", minute_us)]
        for name in self._feature_names:
            value = vector.value(name)
            if not np.isfinite(value):
                value = 0.0
            parts.append(struct.pack("<d", float(value)))
        probability = _hash_to_unit_interval(b"|".join(parts))
        return Prediction(symbol=vector.symbol, probability=probability, model=self.name)
