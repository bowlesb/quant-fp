"""The canonical bus layout: every registered feature's fixed offset in the packed vector + a stable
fingerprint identifying the exact (group, feature, version) set.

The layout is derived ONCE from the global feature registry (the same source of truth the compute and
store use), so a published vector and a consumer that share a fingerprint agree cell-for-cell on what
each offset means. The fingerprint is a deterministic 64-bit blake2b over the ordered
``group:feature:version`` lines — identical across processes and re-implementable in any language, so a
non-Python container can validate it is decoding against the schema it was built for.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from functools import lru_cache

import quantlib.features.groups  # noqa: F401  (import populates REGISTRY via @register side effects)
from quantlib.features.registry import REGISTRY


@dataclass(frozen=True)
class BusField:
    """One feature's place in the packed vector: its group, name, array offset and group version."""

    group: str
    name: str
    offset: int
    version: str


class BusSchema:
    """Ordered (group, feature) -> offset map for the packed float64 vector, plus its fingerprint."""

    def __init__(self, fields: list[BusField]) -> None:
        self.fields = fields
        self.n_features = len(fields)
        self._offset_by_name: dict[str, int] = {field.name: field.offset for field in fields}
        self._fields_by_group: dict[str, list[BusField]] = {}
        for field in fields:
            self._fields_by_group.setdefault(field.group, []).append(field)
        self.fingerprint = self._compute_fingerprint(fields)

    @staticmethod
    def _compute_fingerprint(fields: list[BusField]) -> int:
        payload = "\n".join(f"{field.group}:{field.name}:{field.version}" for field in fields)
        digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest()
        return struct.unpack("<Q", digest)[0]

    @classmethod
    def from_registry(cls) -> BusSchema:
        """Build the layout from the global registry, in canonical ``feature_specs()`` order."""
        fields = [
            BusField(group=group.name, name=spec.name, offset=index, version=group.version)
            for index, (group, spec) in enumerate(REGISTRY.feature_specs())
        ]
        return cls(fields)

    def offset(self, name: str) -> int:
        if name not in self._offset_by_name:
            raise KeyError(f"unknown feature '{name}'")
        return self._offset_by_name[name]

    def has(self, name: str) -> bool:
        return name in self._offset_by_name

    def names(self) -> list[str]:
        return [field.name for field in self.fields]

    def group_names(self) -> list[str]:
        return list(self._fields_by_group.keys())

    def group_fields(self, group: str) -> list[BusField]:
        if group not in self._fields_by_group:
            raise KeyError(f"unknown group '{group}'")
        return self._fields_by_group[group]


@lru_cache(maxsize=1)
def default_schema() -> BusSchema:
    """Process-wide cached schema built from the registry — the layout publishers/consumers share."""
    return BusSchema.from_registry()
