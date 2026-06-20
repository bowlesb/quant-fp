"""The pre-deploy compatibility gate — what makes an ``fc``-only deploy provably safe BEFORE it ships.

Each strategy declares the features it consumes as ``FeatureReq(name, version)`` — the version pin is what
makes this a gate and not just a presence check. The fingerprint is a hash over ``group:name:VERSION``, so
a version bump changes the fingerprint but KEEPS the name; resolving by name alone would silently feed the
model the new-version (differently-computed) feature. ``assert_compatible`` therefore checks both presence
AND version: a version change of a CONSUMED feature is RED, exactly like a removal — the strategy must opt
in deliberately (B2). See docs/BUS_FEATURE_ACCESS.md §2.6.

Subset on names (not equality) is why feature ADDITIONS are non-breaking; the version check is why a
re-computation of a consumed feature can never slip in silently.

``run_gate`` is the multi-strategy entry point the deploy script/CI calls: it FAILS CLOSED if an
expected-live strategy hasn't published a contract (no green-by-omission, B3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from quantlib.bus.schema import BusSchema

CONTRACT_KEY_PREFIX = "strategy:features:"


def contract_key(strategy: str) -> str:
    return f"{CONTRACT_KEY_PREFIX}{strategy}"


@dataclass(frozen=True)
class FeatureReq:
    """One declared feature dependency: its name and the group version the strategy was certified against."""

    name: str
    version: str


def contract_to_json(contract: Sequence[FeatureReq]) -> str:
    """Serialize a strategy's declared contract for publishing to ``strategy:features:<name>``."""
    return json.dumps([{"name": req.name, "version": req.version} for req in contract])


def contract_from_json(text: str) -> tuple[FeatureReq, ...]:
    return tuple(FeatureReq(entry["name"], entry["version"]) for entry in json.loads(text))


def publish_contract(redis_client: object, strategy: str, contract: Sequence[FeatureReq]) -> None:
    """Publish a strategy's contract to ``strategy:features:<name>`` via a raw redis client. Used by a
    container that has no ``BusConsumer`` (e.g. overnight_beta reads no bus features) but must still
    register its (possibly empty) contract so the gate doesn't fail closed on it (B3)."""
    redis_client.set(contract_key(strategy), contract_to_json(contract))  # type: ignore[attr-defined]


class IncompatibleSchema(Exception):
    """A strategy needs a feature the candidate lacks, or at a different version (with the exact list)."""

    def __init__(
        self,
        strategy: str,
        fingerprint: int,
        missing: list[str],
        version_changed: list[tuple[str, str, str]],
    ) -> None:
        self.strategy = strategy
        self.fingerprint = fingerprint
        self.missing = missing
        self.version_changed = version_changed
        super().__init__(
            f"strategy '{strategy}' vs candidate {fingerprint:#018x}: "
            f"missing={missing} version_changed={version_changed}"
        )


class MissingContract(Exception):
    """An expected-live strategy did not publish a contract — the gate fails closed rather than skip it."""

    def __init__(self, strategies: list[str]) -> None:
        self.strategies = strategies
        super().__init__(f"no published contract for expected-live strategies: {strategies}")


def assert_compatible(
    candidate: BusSchema,
    declared: Sequence[FeatureReq],
    *,
    strategy: str,
    value_identical_bumps: Mapping[str, str] | None = None,
) -> None:
    """Raise ``IncompatibleSchema`` unless every declared ``(name, version)`` resolves in ``candidate`` at
    the same version. ``value_identical_bumps`` (name -> new version the PRODUCER annotated value-identical)
    lets a verified value-identical restructure auto-pass — an optional friction-reducer; the safe default
    (empty map) is RED on any consumed-feature version change."""
    annotated = value_identical_bumps or {}
    missing: list[str] = []
    version_changed: list[tuple[str, str, str]] = []
    for req in declared:
        field = candidate.field(req.name)
        if field is None:
            missing.append(req.name)
        elif field.version != req.version and annotated.get(req.name) != field.version:
            version_changed.append((req.name, req.version, field.version))
    if missing or version_changed:
        raise IncompatibleSchema(strategy, candidate.fingerprint, missing, version_changed)


def run_gate(
    candidate: BusSchema,
    contracts: Mapping[str, Sequence[FeatureReq]],
    expected_live: Sequence[str],
    *,
    value_identical_bumps: Mapping[str, str] | None = None,
) -> None:
    """Run the gate for every expected-live strategy against ``candidate``. FAILS CLOSED (``MissingContract``)
    if any expected strategy has no published contract; otherwise raises the FIRST ``IncompatibleSchema``.

    GREEN (no raise) clears an ``fc``-only deploy; any raise blocks it and names the exact fault."""
    absent = [name for name in expected_live if name not in contracts]
    if absent:
        raise MissingContract(sorted(absent))
    for name in expected_live:
        assert_compatible(
            candidate, contracts[name], strategy=name, value_identical_bumps=value_identical_bumps
        )
