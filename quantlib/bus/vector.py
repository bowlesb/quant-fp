"""The decoded feature vector — the consumer-facing accessor.

A strategy container should never touch offsets or bytes. It addresses features by name, three ways:

    vec.value("momentum_fast_1")        # O(1) name lookup -> float (the hot path)
    vec["momentum_fast_1"]              # same, dict style
    vec.momentum.momentum_fast_1        # nested group.feature attribute access (readable/debuggable)

plus ``vec.array`` (the raw numpy view for vectorized math), ``vec.to_dict()``, and ``vec.ref(name)``
for a labelled FeatureRef (group + name + value + offset) handy when logging/debugging. Nested access
validates the feature actually belongs to that group, so a typo or wrong-group reference raises rather
than silently resolving.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from quantlib.bus.schema import BusSchema


@dataclass(frozen=True)
class FeatureRef:
    """A labelled feature reading — name, owning group, value and offset — for logging/debugging."""

    group: str
    name: str
    value: float
    offset: int


class _GroupView:
    """Attribute view over one group's features, e.g. ``vec.momentum.momentum_fast_1``."""

    def __init__(self, vector: FeatureVector, group: str, feature_names: frozenset[str]) -> None:
        object.__setattr__(self, "_vector", vector)
        object.__setattr__(self, "_group", group)
        object.__setattr__(self, "_feature_names", feature_names)

    def __getattr__(self, feature: str) -> float:
        feature_names: frozenset[str] = object.__getattribute__(self, "_feature_names")
        if feature not in feature_names:
            group = object.__getattribute__(self, "_group")
            raise AttributeError(f"group '{group}' has no feature '{feature}'")
        vector: FeatureVector = object.__getattribute__(self, "_vector")
        return vector.value(feature)

    def __getitem__(self, feature: str) -> float:
        return self.__getattr__(feature)


class FeatureVector:
    """One decoded (symbol, minute) vector. The float payload is a zero-copy numpy view over the frame."""

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

    @property
    def array(self) -> np.ndarray:
        """The raw float64 vector in canonical schema order (read-only view) for vectorized math."""
        return self._array

    def value(self, name: str) -> float:
        return float(self._array[self._schema.offset(name)])

    def __getitem__(self, name: str) -> float:
        return self.value(name)

    def ref(self, name: str) -> FeatureRef:
        offset = self._schema.offset(name)
        field = self._schema.fields[offset]
        return FeatureRef(group=field.group, name=name, value=float(self._array[offset]), offset=offset)

    def to_dict(self) -> dict[str, float]:
        return {field.name: float(self._array[field.offset]) for field in self._schema.fields}

    def __getattr__(self, group: str) -> _GroupView:
        # Only reached when `group` is not a real instance attribute. Resolve it as a feature group.
        if group.startswith("_"):
            raise AttributeError(group)
        schema: BusSchema = object.__getattribute__(self, "_schema")
        if group not in schema.group_names():
            raise AttributeError(f"no feature group '{group}'")
        names = frozenset(field.name for field in schema.group_fields(group))
        return _GroupView(self, group, names)

    def __repr__(self) -> str:
        return f"FeatureVector(symbol={self.symbol!r}, minute={self.minute.isoformat()}, n={self._schema.n_features})"
