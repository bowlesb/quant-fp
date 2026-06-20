"""``FeatureView`` — a name-addressed read over ONE decoded frame, resolved against the FRAME's schema.

Unlike ``FeatureVector`` (which is bound to a single compiled schema), a ``FeatureView`` is bound to the
schema the frame's fingerprint resolves to (via the ``SchemaRegistry``). So a consumer reads its declared
features by NAME regardless of how the producer's feature set has grown/reordered — the decoupling that
removes the coordinated-rebuild tax. See docs/BUS_FEATURE_ACCESS.md §2.3–2.4.

It satisfies the ``FeatureRow`` protocol (``.symbol``, ``.minute``, ``.value(name)``), so the re-homed
single-name cores consume it unchanged. ``to_model_vector(expected_names)`` builds the dense, consumer-
ORDERED model input at the model boundary: a needed feature absent from the frame raises ``MissingFeature``
(never a silent NaN); extra features in the frame are ignored.

``to_model_vector`` guarantees PRESENCE and correct placement, NOT finiteness (B5): a feature that is
present but NaN that minute (warmup / sparse) is returned as NaN. Finiteness is the strategy's warmup
gate's job (STRATEGY_CONTAINERS.md), not this layer's.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np

from quantlib.bus.schema import BusSchema


class MissingFeature(Exception):
    """A consumer requested a feature its model needs that the frame's schema does not contain."""

    def __init__(self, name: str, fingerprint: int) -> None:
        self.name = name
        self.fingerprint = fingerprint
        super().__init__(f"feature '{name}' not in frame schema {fingerprint:#018x}")


class FeatureView:
    """Name-addressed view over one (symbol, minute) frame, resolved against the frame's schema."""

    def __init__(
        self,
        schema: BusSchema,
        symbol: str,
        minute: dt.datetime,
        array: np.ndarray,
        fingerprint: int,
    ) -> None:
        self._schema = schema
        self.symbol = symbol
        self.minute = minute
        self._array = array
        self.fingerprint = fingerprint
        # share the schema's name->offset map by reference (no per-frame copy); the schema is cached per
        # fingerprint, so this is O(1) construction and O(1) reads.
        self._offsets = schema.offset_map()

    @property
    def array(self) -> np.ndarray:
        """The raw float64 payload in the FRAME's schema order (read-only view)."""
        return self._array

    def has(self, name: str) -> bool:
        return name in self._offsets

    def value(self, name: str) -> float:
        offset = self._offsets.get(name)
        if offset is None:
            raise MissingFeature(name, self.fingerprint)
        return float(self._array[offset])

    def get(self, name: str, default: float = float("nan")) -> float:
        """NaN-safe read for a genuinely OPTIONAL feature: absent -> ``default`` (opt-in, not the default
        behavior — ``value`` and ``to_model_vector`` raise on a missing required feature)."""
        offset = self._offsets.get(name)
        return default if offset is None else float(self._array[offset])

    def __getitem__(self, name: str) -> float:
        return self.value(name)

    def to_model_vector(self, expected_names: Sequence[str]) -> np.ndarray:
        """Dense model input in the CONSUMER's order. Missing required feature -> ``MissingFeature``;
        order is the consumer's (a value-identical restructure is a non-event); present-but-NaN stays NaN."""
        out = np.empty(len(expected_names), dtype="<f8")
        for i, name in enumerate(expected_names):
            offset = self._offsets.get(name)
            if offset is None:
                raise MissingFeature(name, self.fingerprint)
            out[i] = self._array[offset]
        return out

    def __repr__(self) -> str:
        return (
            f"FeatureView(symbol={self.symbol!r}, minute={self.minute.isoformat()}, "
            f"fp={self.fingerprint:#018x}, n={self._schema.n_features})"
        )
