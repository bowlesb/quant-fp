"""Unit tests for the feature-data coverage surface (gaps, trust join). No DB: trust + store mocked."""

from __future__ import annotations

import pytest

from quantlib.features import feature_data as fd


def test_gaps_against_expected_window() -> None:
    covered = ["2025-01-02", "2025-01-06"]
    expected = ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert fd.gaps(covered, expected) == ["2025-01-03"]


def test_gaps_empty_when_no_window() -> None:
    assert fd.gaps(["2025-01-02"], []) == []


def test_source_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fd.store, "_date_dirs", lambda root, g, v, s: {"2025-01-06", "2025-01-02"}
    )
    cov = fd.source_coverage("/store", "gA", "1.0", "backfill")
    assert cov["n_dates"] == 2
    assert cov["first_date"] == "2025-01-02"
    assert cov["last_date"] == "2025-01-06"
    assert cov["dates"] == ["2025-01-02", "2025-01-06"]


def test_trust_by_feature_reuses_trusted_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fd,
        "trusted_features",
        lambda: [
            {
                "feature": "f1",
                "trust_reason": "parity_1day",
                "trust_value_rate": 0.9995,
            },
            {
                "feature": "f2",
                "trust_reason": "deterministic",
                "trust_value_rate": None,
            },
        ],
    )
    trust = fd.trust_by_feature()
    assert set(trust) == {"f1", "f2"}
    assert trust["f1"]["trust_reason"] == "parity_1day"
