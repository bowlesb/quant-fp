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
import json
import os
import struct
from dataclasses import dataclass
from functools import lru_cache

import quantlib.features.groups  # noqa: F401  (import populates REGISTRY via @register side effects)
from quantlib.features.registry import REGISTRY

# The engine-version tag folded into the fingerprint ONLY when the clean engine is armed (FP_CLEAN_ENGINE=1).
# The clean engine produces DIFFERENT feature VALUES for the same (group, feature, version) — but the
# fingerprint is value-blind (schema-only), so without this tag an armed engine would emit the OLD fingerprint
# and a downstream consumer would silently read new values under the old contract (and the store would mix
# old/new rows in one v= partition). The tag = a clean fingerprint break = a new v= partition + a recal signal.
# It is FLAG-CONDITIONAL by design: flag OFF → payload unchanged → fingerprint identical to the OLD engine's, so
# a rollback (flag off) reproduces the exact old fingerprint (the byte-identical-rollback invariant). One global
# tag — NOT per-group version bumps — because the change is "the engine changed", not 64 feature redefinitions.
_CLEAN_ENGINE_VERSION_TAG = "engine=clean1"


def _clean_engine_armed() -> bool:
    """``FP_CLEAN_ENGINE=1`` — same predicate as ``capture.clean_engine_enabled`` (read the env var directly here
    to avoid importing the capture module into the schema layer). When armed, the fingerprint folds in the
    engine-version tag so the contract reflects the value change."""
    return os.environ.get("FP_CLEAN_ENGINE") == "1"


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
        # When the clean engine is armed, prepend the engine-version tag so the fingerprint reflects the value
        # change. Flag OFF leaves the payload byte-for-byte identical → the OLD fingerprint is reproduced exactly
        # (rollback-safe). Same blake2b algorithm both ways; only the payload's first line differs when armed.
        if _clean_engine_armed():
            payload = f"{_CLEAN_ENGINE_VERSION_TAG}\n{payload}"
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

    def offsets(self) -> dict[str, int]:
        """The full name -> offset map (a copy) — safe for callers that may mutate."""
        return dict(self._offset_by_name)

    def offset_map(self) -> dict[str, int]:
        """The schema's name -> offset map by REFERENCE (no copy) — the per-frame hot path. The map is
        owned by the schema, which is cached per fingerprint, so a FeatureView shares it without copying
        694 entries on every frame. Treat as read-only."""
        return self._offset_by_name

    def field(self, name: str) -> BusField | None:
        """The field for ``name`` (group, offset, version) or None if absent — version-aware lookup the
        compat gate uses to check a consumed feature's version, not just its presence."""
        offset = self._offset_by_name.get(name)
        return None if offset is None else self.fields[offset]

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

    def to_json(self) -> str:
        """Serialize the layout for publishing to ``bus:schema:<fp>`` — the producer writes this so any
        consumer can rebuild a fingerprint-faithful schema for a frame it didn't compile against."""
        payload = {
            "fingerprint": self.fingerprint,
            "n_features": self.n_features,
            "fields": [
                {"name": field.name, "offset": field.offset, "group": field.group, "version": field.version}
                for field in self.fields
            ],
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> BusSchema:
        """Rebuild a schema from its published JSON. The reconstructed schema recomputes the SAME
        fingerprint from the fields (it does not trust the serialized one) — so a tampered/garbled payload
        whose fields don't hash to the advertised fingerprint is caught loudly, not silently misaligned."""
        payload = json.loads(text)
        fields = [
            BusField(
                group=entry["group"], name=entry["name"], offset=entry["offset"], version=entry["version"]
            )
            for entry in sorted(payload["fields"], key=lambda entry: entry["offset"])
        ]
        schema = cls(fields)
        advertised = int(payload["fingerprint"])
        if schema.fingerprint != advertised:
            raise ValueError(
                f"schema JSON fingerprint {advertised:#018x} != recomputed {schema.fingerprint:#018x} "
                "(corrupt or tampered bus:schema payload)"
            )
        return schema


@lru_cache(maxsize=1)
def default_schema() -> BusSchema:
    """Process-wide cached schema built from the registry — the layout publishers/consumers share."""
    return BusSchema.from_registry()
