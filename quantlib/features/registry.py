"""The feature registry: self-registration, uniqueness, metadata validation, catalog.

Adding a feature is adding one group file under ``groups/`` decorated with ``@register`` — no
edits to any shared module. Registration fails fast on a duplicate group name, a duplicate feature
name, or incomplete/stub metadata, so concurrent agents can never silently shadow each other
(FEATURE_PLATFORM.md §3.6).
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (MIN_DESCRIPTION_CHARS, FeatureGroup,
                                    FeatureSpec, FeatureType)

REQUIRED_ATTRS = ("name", "version", "owner", "type")


class RegistrationError(Exception):
    """Raised when a group violates the registration contract (FEATURE_PLATFORM.md §3.6)."""


class Registry:
    """Holds registered groups and enforces uniqueness + metadata at registration time."""

    def __init__(self) -> None:
        self._groups: dict[str, FeatureGroup] = {}
        self._feature_owner: dict[str, str] = {}

    def register(self, group_cls: type[FeatureGroup]) -> type[FeatureGroup]:
        group = group_cls()
        self._validate(group)
        self._groups[group.name] = group
        for spec in group.declare():
            self._feature_owner[spec.name] = group.name
        return group_cls

    def _validate(self, group: FeatureGroup) -> None:
        for attr in REQUIRED_ATTRS:
            value = getattr(group, attr, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise RegistrationError(f"{type(group).__name__}: missing required attribute '{attr}'")
        if not isinstance(group.type, FeatureType):
            raise RegistrationError(f"{group.name}: type must be a FeatureType, got {group.type!r}")
        if group.name in self._groups:
            raise RegistrationError(f"duplicate group name '{group.name}'")
        specs = group.declare()
        if not specs:
            raise RegistrationError(f"{group.name}: declares no features")
        for spec in specs:
            self._validate_spec(group.name, spec)

    def _validate_spec(self, group_name: str, spec: FeatureSpec) -> None:
        if spec.name in self._feature_owner:
            owner = self._feature_owner[spec.name]
            raise RegistrationError(
                f"duplicate feature name '{spec.name}' (already owned by group '{owner}')"
            )
        if len(spec.description) < MIN_DESCRIPTION_CHARS:
            raise RegistrationError(
                f"{group_name}.{spec.name}: description must be >= {MIN_DESCRIPTION_CHARS} chars"
            )
        if spec.description.strip() == spec.name:
            raise RegistrationError(f"{group_name}.{spec.name}: description must not equal the name")

    def swap_group(self, group_cls: type[FeatureGroup]) -> FeatureGroup:
        """Replace an ALREADY-registered group's instance IN PLACE with a fresh instance of ``group_cls``.

        The within-day hot-swap path (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §3): re-importing a group's
        module re-runs ``@register`` for a name that already exists, which ``register`` correctly REJECTS
        (duplicate-name guard). A hot-swap is the explicit, intentional in-place replacement of one group's
        compute LOGIC under the SAME name + feature set — so it clears the old group's feature-owner entries
        first (they belong to the same name), validates the replacement declares the SAME feature names (no
        add/remove/rename — that would change the fingerprint), then overwrites ``_groups[name]``.

        Refuses (raises ``RegistrationError``) if the group is not already registered, or if the replacement's
        declared feature-name set differs from the incumbent's — both are fingerprint-affecting and MUST NOT
        go through the silent swap path (they require a coordinated, versioned deploy, not a hot-swap)."""
        candidate = group_cls()
        name = candidate.name
        if name not in self._groups:
            raise RegistrationError(
                f"swap_group: '{name}' is not registered — cannot hot-swap an absent group"
            )
        incumbent = self._groups[name]
        old_names = {spec.name for spec in incumbent.declare()}
        new_names = {spec.name for spec in candidate.declare()}
        if old_names != new_names:
            raise RegistrationError(
                f"swap_group '{name}': feature set changed (added={new_names - old_names}, "
                f"removed={old_names - new_names}) — fingerprint-affecting, not hot-swappable"
            )
        for spec in incumbent.declare():
            self._feature_owner.pop(spec.name, None)
        for spec in candidate.declare():
            self._feature_owner[spec.name] = name
        self._groups[name] = candidate
        return candidate

    def groups(self) -> list[FeatureGroup]:
        return list(self._groups.values())

    def get_group(self, name: str) -> FeatureGroup:
        if name not in self._groups:
            raise KeyError(f"no registered group '{name}'")
        return self._groups[name]

    def feature_specs(self) -> list[tuple[FeatureGroup, FeatureSpec]]:
        return [(group, spec) for group in self._groups.values() for spec in group.declare()]

    def feature_names(self) -> list[str]:
        return [spec.name for _, spec in self.feature_specs()]

    def catalog(self) -> pl.DataFrame:
        rows = [
            {
                "feature": spec.name,
                "group": group.name,
                "type": group.type.value,
                "version": group.version,
                "owner": group.owner,
                "layer": spec.layer,
                "parity_method": spec.parity_method,
                "dtype": spec.dtype,
                "nan_policy": spec.nan_policy,
                "valid_range": str(spec.valid_range),
                "description": spec.description,
            }
            for group, spec in self.feature_specs()
        ]
        return pl.DataFrame(rows)


REGISTRY = Registry()


def register(group_cls: type[FeatureGroup]) -> type[FeatureGroup]:
    """Class decorator registering a group into the global registry.

    RELOAD-AWARE: a module reload (the within-day hot-swap path) re-runs this decorator for a name that is
    already registered. If the re-registration is a pure SWAP of the SAME group (same name + same declared
    feature-name set), route to ``swap_group`` (in-place replacement) instead of raising the duplicate-name
    error. A genuinely NEW group, or a name collision with a DIFFERENT feature set, still raises — the
    uniqueness guard that stops two agents shadowing each other is preserved for every real conflict."""
    name = getattr(group_cls, "name", None)
    if isinstance(name, str) and name in REGISTRY._groups:
        incumbent = REGISTRY._groups[name]
        candidate = group_cls()
        if {spec.name for spec in incumbent.declare()} == {spec.name for spec in candidate.declare()}:
            REGISTRY.swap_group(group_cls)
            return group_cls
    return REGISTRY.register(group_cls)
