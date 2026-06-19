"""The trust-gate consumers read the BINARY surface, not the legacy lifecycle_state.

After the binary-trust redesign (docs/TRUST_REDESIGN.md) the gate is a single predicate —
``feature_trust.trust_state = 'TRUSTED'`` — exposed through ``quantlib.features.trusted_list``. The legacy
``11_trusted_features.sql`` surface (``lifecycle_state`` column + the old ``trusted_features`` view shape)
is superseded; ``12_trust_binary.sql`` redefined the ``trusted_features`` view onto ``trust_state`` so the
view and the code predicate are the SAME set.

These tests pin that equivalence WITHOUT a DB: every consumer of the trust gate (the trusted-list accessor,
the selective-backfill driver, the dashboard frontier) must key on ``trust_state = 'TRUSTED'`` and NEVER on
``lifecycle_state = 'VALIDATED'``. A regression that re-points any of them at the legacy column would, on the
live DB, silently return an EMPTY trusted set (lifecycle_state has 0 VALIDATED rows under the binary model),
which is exactly the behavior change this guards against.
"""

from __future__ import annotations

from quantlib.features import trusted_list

TRUST_GATE_PREDICATE = "trust_state = 'TRUSTED'"
LEGACY_GATE_PREDICATE = "lifecycle_state = 'VALIDATED'"


def test_trusted_list_names_query_uses_binary_gate() -> None:
    # The names predicate the backfill + dashboard consumers intersect against IS the binary gate.
    assert TRUST_GATE_PREDICATE in trusted_list._NAMES_QUERY
    assert "lifecycle_state" not in trusted_list._NAMES_QUERY


def test_trusted_list_rich_query_uses_binary_gate() -> None:
    # The rich-rows predicate (feature_data joins on these) is the binary gate, and carries the binary
    # provenance columns (trust_reason / trust_value_rate), not the legacy clean_days/clean_value_rate.
    assert TRUST_GATE_PREDICATE in trusted_list._TRUSTED_QUERY
    assert "lifecycle_state" not in trusted_list._TRUSTED_QUERY
    assert "trust_reason" in trusted_list._TRUSTED_QUERY
    assert "trust_value_rate" in trusted_list._TRUSTED_QUERY


def test_no_consumer_keys_on_legacy_validated_predicate() -> None:
    # Defense in depth: the legacy gate predicate must not appear in the consumable trust accessor.
    assert LEGACY_GATE_PREDICATE not in trusted_list._NAMES_QUERY
    assert LEGACY_GATE_PREDICATE not in trusted_list._TRUSTED_QUERY
