"""Binary trust redesign (docs/TRUST_REDESIGN.md) — the pure logic, network-free.

Asserts the policy (per-type tolerance, determinism, per-feature override), the 1-day earn decision, the
deterministic-feature selection, and the random-check un-trust decision — all over plain values/frames, no
DB or store I/O.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureType
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_binary import deterministic_features, earned_features, feature_policy_map
from quantlib.features.trust_policy import (
    MIN_CLEAN_DAYS_TO_TRUST,
    TYPE_POLICY,
    current_git_commit,
    group_content_hash,
    policy_for,
)
from quantlib.features.trust_random_check import failed_check


def test_one_day_earns_trust() -> None:
    assert MIN_CLEAN_DAYS_TO_TRUST == 1


def test_calendar_is_deterministic_others_are_not() -> None:
    assert policy_for(FeatureType.CALENDAR).deterministic is True
    assert policy_for(FeatureType.PRICE).deterministic is False
    assert policy_for(FeatureType.CROSS_SECTIONAL).deterministic is False


def test_cross_sectional_requires_full_universe() -> None:
    assert policy_for(FeatureType.CROSS_SECTIONAL).full_universe is True
    assert policy_for(FeatureType.PRICE).full_universe is False


def test_windowed_types_get_looser_default_than_exact() -> None:
    # the whole point: a windowed feature at the engine-default 1e-6 should be compared at the looser type
    # default (1e-4), while an exact-numeric type stays tight.
    assert policy_for(FeatureType.TECHNICAL).rtol == 1e-4
    assert policy_for(FeatureType.PRICE).rtol == 1e-6


def test_per_feature_tolerance_overrides_type_default() -> None:
    # an author who set a non-default tolerance knows that feature — their value wins over the type default.
    assert policy_for(FeatureType.PRICE, spec_tolerance=0.02).rtol == 0.02
    # the engine default (1e-6) is NOT treated as an override — the type policy applies.
    assert policy_for(FeatureType.TECHNICAL, spec_tolerance=1e-6).rtol == 1e-4


def test_min_pass_rate_thresholds_earning() -> None:
    # a feature at/above its type min_pass_rate earns; below does not. Use a windowed feature (0.999 floor).
    policy_of = {
        "good": ("1.0.0", TYPE_POLICY[FeatureType.TECHNICAL]),
        "bad": ("1.0.0", TYPE_POLICY[FeatureType.TECHNICAL]),
        "calendar_feat": ("1.0.0", TYPE_POLICY[FeatureType.CALENDAR]),
    }
    clean_today = pl.DataFrame(
        {
            "feature": ["good", "bad", "calendar_feat"],
            "clean_value_rate": [0.9995, 0.95, 1.0],
        }
    )
    earned = earned_features(clean_today, policy_of)
    assert "good" in earned
    assert "bad" not in earned
    # deterministic features are NOT earned via the parity path — they are auto-trusted separately.
    assert "calendar_feat" not in earned


def test_earned_ignores_null_rate() -> None:
    policy_of = {"f": ("1.0.0", TYPE_POLICY[FeatureType.TECHNICAL])}
    clean_today = pl.DataFrame({"feature": ["f"], "clean_value_rate": [None]})
    assert earned_features(clean_today, policy_of) == []


def test_deterministic_features_are_calendar_typed() -> None:
    det = set(deterministic_features())
    calendar_feats = {
        spec.name for group, spec in REGISTRY.feature_specs() if group.type == FeatureType.CALENDAR
    }
    assert det == calendar_feats
    assert det, "expected at least one calendar feature to auto-trust"


def test_content_hash_is_stable_and_per_group() -> None:
    groups = list(REGISTRY.groups())
    first = group_content_hash(groups[0])
    assert first == group_content_hash(groups[0])  # deterministic
    assert len(first) == 16  # blake2b digest_size=8 -> 16 hex chars


def test_feature_policy_map_covers_registry() -> None:
    policy_of = feature_policy_map()
    all_features = {spec.name for _, spec in REGISTRY.feature_specs()}
    assert set(policy_of) == all_features


def test_random_check_untrust_decision() -> None:
    # below threshold on a compared day -> failed (un-trust); at/above -> passes; no comparison -> never fails.
    assert failed_check(value_rate=0.99, min_pass_rate=0.999) is True
    assert failed_check(value_rate=0.9995, min_pass_rate=0.999) is False
    assert failed_check(value_rate=None, min_pass_rate=0.999) is False
    assert failed_check(value_rate=0.5, min_pass_rate=None) is False


def test_current_git_commit_runs() -> None:
    # provenance helper must never raise (None is an acceptable result when git is unavailable).
    result = current_git_commit()
    assert result is None or isinstance(result, str)
