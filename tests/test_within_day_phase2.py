"""Unit tests for WDPC Phase 2 — the root-cause classifier + the cert/trust-grant write path.

Phase 2 BUILDS + TESTS the write path; it does NOT live-grant (dry_run is the default; the live path is
exercised here only against a mocked cursor). Covers: the classifier screens the known non-bugs and routes
real mismatches to the right code path; plan_writes grants ONLY certified-not-already-trusted features with
reason='within_day_parity'; the dry-run opens no connection.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from quantlib.features import within_day_rootcause as rc
from quantlib.features import within_day_trust as wdt

DAY = dt.date(2026, 6, 18)


# ---- root-cause classifier ----------------------------------------------------------------------


def test_tick_feature_off_subscribed_set_is_coverage_gap_not_a_bug() -> None:
    cause = rc.classify_feature(
        "trade_freq_5m",
        n_mismatch=5,
        n_extra_live=0,
        n_missing_live=0,
        value_rate=0.9,
        is_tick_feature=True,
        on_tick_symbol=False,
    )
    assert cause.classification == rc.COVERAGE_GAP
    assert not cause.is_actionable()


def test_long_window_divergence_is_capture_start_artifact() -> None:
    cause = rc.classify_feature(
        "mean_abs_ret_180m",
        n_mismatch=300,
        n_extra_live=0,
        n_missing_live=0,
        value_rate=0.17,
        is_tick_feature=False,
        on_tick_symbol=True,
    )
    assert cause.classification == rc.ARTIFACT
    assert not cause.is_actionable()


def test_extra_live_only_is_coverage_not_math_bug() -> None:
    cause = rc.classify_feature(
        "ret_5m",
        n_mismatch=0,
        n_extra_live=12,
        n_missing_live=0,
        value_rate=1.0,
        is_tick_feature=False,
        on_tick_symbol=True,
    )
    assert cause.classification == rc.COVERAGE_GAP
    assert not cause.is_actionable()


def test_sign_flip_exemplars_route_to_sign_convention() -> None:
    exemplars: list[dict[str, object]] = [
        {"stream_value": 1.2, "backfill_value": -1.2},
        {"stream_value": 0.5, "backfill_value": -0.5},
    ]
    cause = rc.classify_feature(
        "signed_vol_5m",
        n_mismatch=2,
        n_extra_live=0,
        n_missing_live=0,
        value_rate=0.5,
        is_tick_feature=False,
        on_tick_symbol=True,
        exemplars=exemplars,
    )
    assert cause.classification == rc.SIGN_CONVENTION
    assert "stateful.py" in cause.suspected_modules
    assert cause.is_actionable()


def test_nan_vs_null_exemplars_route_to_degenerate_guard() -> None:
    exemplars: list[dict[str, object]] = [
        {"stream_value": float("nan"), "backfill_value": None},
        {"stream_value": None, "backfill_value": 0.3},
    ]
    cause = rc.classify_feature(
        "bb_position_20m",
        n_mismatch=2,
        n_extra_live=0,
        n_missing_live=0,
        value_rate=0.5,
        is_tick_feature=False,
        on_tick_symbol=True,
        exemplars=exemplars,
    )
    assert cause.classification == rc.DEGENERATE_GUARD
    assert cause.is_actionable()


def test_small_eps_exemplars_route_to_live_fast_path() -> None:
    exemplars: list[dict[str, object]] = [
        {"stream_value": 1.00000001, "backfill_value": 1.0},
        {"stream_value": 2.0000001, "backfill_value": 2.0},
    ]
    cause = rc.classify_feature(
        "volume_zscore_30m",
        n_mismatch=2,
        n_extra_live=0,
        n_missing_live=0,
        value_rate=0.99,
        is_tick_feature=False,
        on_tick_symbol=True,
        exemplars=exemplars,
    )
    assert cause.classification == rc.LIVE_FAST_PATH
    assert "incremental.py" in cause.suspected_modules
    assert cause.is_actionable()


def test_triage_report_sorts_actionable_first() -> None:
    causes = [
        rc.classify_feature("trade_freq_5m", 5, 0, 0, 0.9, True, False),  # coverage (not actionable)
        rc.classify_feature(
            "signed_vol_5m",
            2,
            0,
            0,
            0.5,
            False,
            True,
            exemplars=[{"stream_value": 1.0, "backfill_value": -1.0}],
        ),
    ]
    report = rc.triage_report(causes)
    assert report["actionable"][0] is True  # the real defect sorts first


# ---- cert + trust-grant write path -------------------------------------------------------------


def _certified(feature: str, rate: float) -> wdt.CertResult:
    return wdt.certify_result_from_summary(
        feature,
        "momentum",
        DAY,
        rate,
        n_compared=400,
        n_clean_symbols=25,
        stable_cycles=3,
        window_minutes=30,
        settle_lag_min=20.0,
        min_pass_rate=0.999,
    )


def test_certify_result_status_from_rate() -> None:
    assert _certified("up_ratio_3m", 0.9999).status == wdt.STATUS_CERTIFIED
    assert _certified("up_ratio_3m", 0.40).status == wdt.STATUS_DEFECTED


def test_plan_writes_grants_only_certified_with_within_day_reason() -> None:
    results = [_certified("up_ratio_3m", 0.9999), _certified("mean_abs_ret_3m", 0.40)]
    cert_rows, grant_rows, check_rows = wdt.plan_writes(results)
    # both get a cert stamp; only the certified one earns a grant.
    assert len(cert_rows) == 2
    assert len(grant_rows) == 1
    assert grant_rows[0]["feature"] == "up_ratio_3m"
    assert grant_rows[0]["reason"] == wdt.WITHIN_DAY_REASON
    assert check_rows[0]["check_kind"] == "within_day"
    assert check_rows[0]["action"] == "trusted"


def test_plan_writes_skips_already_trusted_grant() -> None:
    results = [_certified("up_ratio_3m", 0.9999)]
    # already trusted -> no grant row, but the cert stamp is still written.
    cert_rows, grant_rows, check_rows = wdt.plan_writes(results, trusted_already={"up_ratio_3m"})
    assert len(cert_rows) == 1
    assert grant_rows == []
    assert check_rows == []


def test_dry_run_opens_no_db_connection() -> None:
    results = [_certified("up_ratio_3m", 0.9999)]
    with patch("quantlib.features.within_day_trust.psycopg.connect") as mock_connect:
        counts = wdt.write_certifications(results, dry_run=True)
    mock_connect.assert_not_called()  # the make-or-break: a dry run NEVER touches the DB
    assert counts == {"cert_rows": 1, "grants": 1, "checks": 1}


def test_live_write_executes_cert_grant_and_check_sql() -> None:
    results = [_certified("up_ratio_3m", 0.9999), _certified("mean_abs_ret_3m", 0.40)]
    cursor = MagicMock()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cursor
    with (
        patch("quantlib.features.within_day_trust.psycopg.connect", return_value=conn),
        patch("quantlib.features.within_day_trust.already_trusted", return_value=set()),
    ):
        counts = wdt.write_certifications(results, dry_run=False)
    # three executemany calls: cert UPSERT, trust grant, trust check.
    assert cursor.executemany.call_count == 3
    conn.commit.assert_called_once()
    assert counts == {"cert_rows": 2, "grants": 1, "checks": 1}
