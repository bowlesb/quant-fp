"""Unit tests for WDPC version-awareness (quantlib.features.within_day_version).

The core (compare_status) is PURE — "is the deployed content hash the one trust was earned on?" — so the
four verdicts are tested directly without a DB. The registry-backed helpers (version_status,
is_deployed_version_trusted, reset_trust_on_content_change, group_by_name) are exercised in dry_run against a
real registered group: dry_run reports against an empty trust table (everything NOT_REGISTERED → trivially
trusted, nothing to reset) and opens no DB connection. Named test_fp_* so the CI gate runs these on itself.
"""

from __future__ import annotations

import logging

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.features import within_day_version as wv
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_version import VersionStatus, compare_status


def test_compare_status_untrusted_when_no_row() -> None:
    assert compare_status("hash_live", None, None) is VersionStatus.NOT_REGISTERED


def test_compare_status_untrusted_when_non_trusted() -> None:
    assert compare_status("hash_live", "NON_TRUSTED", "hash_old") is VersionStatus.UNTRUSTED


def test_compare_status_matches_when_hashes_equal() -> None:
    assert compare_status("hash_live", "TRUSTED", "hash_live") is VersionStatus.LIVE_MATCHES_TRUST


def test_compare_status_diverged_when_hashes_differ() -> None:
    # The deployed code changed since the grant → trust was earned on different code → must re-earn.
    assert compare_status("hash_NEW", "TRUSTED", "hash_old") is VersionStatus.LIVE_DIVERGED


def test_compare_status_diverged_when_grant_hash_missing() -> None:
    # A trusted row with no recorded content hash can't be confirmed to match the deployed code → diverged.
    assert compare_status("hash_live", "TRUSTED", None) is VersionStatus.LIVE_DIVERGED


def test_version_status_dry_run_reports_not_registered_and_no_db(caplog) -> None:  # type: ignore[no-untyped-def]
    group = REGISTRY.get_group("momentum")
    with caplog.at_level(logging.INFO, logger="within_day_version"):
        reports = wv.version_status(group)
    assert reports, "momentum declares at least one feature"
    assert all(report.status is VersionStatus.NOT_REGISTERED for report in reports)
    # The live content hash is the machine-derived digest, populated even in dry_run.
    assert all(report.live_content_hash for report in reports)
    assert "DRY-RUN version_status" in caplog.text


def test_is_deployed_version_trusted_true_in_dry_run() -> None:
    # No grants in dry_run → no LIVE_DIVERGED → trivially "trusted on the deployed version".
    group = REGISTRY.get_group("momentum")
    assert wv.is_deployed_version_trusted(group) is True


def test_reset_trust_dry_run_resets_nothing_when_nothing_diverged() -> None:
    group = REGISTRY.get_group("momentum")
    assert wv.reset_trust_on_content_change(group) == []


def test_group_by_name_finds_registered_and_misses_unknown() -> None:
    assert wv.group_by_name("momentum") is REGISTRY.get_group("momentum")
    assert wv.group_by_name("no_such_group_xyz") is None
