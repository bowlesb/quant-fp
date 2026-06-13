"""Feature platform package — registry, engine, introspection, and the self-registering groups.

Public surface (FEATURE_PLATFORM.md §3.5). Importing this package registers all groups into the
global ``REGISTRY``.
"""
from quantlib.features import groups  # noqa: F401  (importing self-registers every group)
from quantlib.features.base import (
    KEY_COLUMNS,
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.engine import (
    ContractError,
    assert_deterministic,
    run_all,
    run_group,
)
from quantlib.features.introspect import IntrospectionError, assert_sane, introspect
from quantlib.features.registry import (
    REGISTRY,
    RegistrationError,
    Registry,
    register,
)

__all__ = [
    "KEY_COLUMNS",
    "BatchContext",
    "FeatureGroup",
    "FeatureSpec",
    "FeatureType",
    "InputSpec",
    "ContractError",
    "assert_deterministic",
    "run_all",
    "run_group",
    "IntrospectionError",
    "assert_sane",
    "introspect",
    "REGISTRY",
    "RegistrationError",
    "Registry",
    "register",
]
