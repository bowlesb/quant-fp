"""Tests for the per-group lifecycle ORCHESTRATOR (quantlib/features/within_day_run.py).

The orchestrator strings the WDPC primitives into one call (claim -> version snapshot -> monitor-to-certify
-> on-certify version reset -> deploy-queue peek). We pin its composition + the dry-run-safe defaults by
stubbing the primitives (no DB, no live state), so the wiring is verified offline:

  * a refused claim aborts early (no monitor run, no certify);
  * a certify fires the version reset + the deploy-queue peek and surfaces them in the result;
  * a no-certify run still peeks the queue and never resets trust;
  * the lock is released on every exit path (the group is never left locked);
  * the monitor is invoked with claim_lock=False (the orchestrator already holds the lock).
"""

from __future__ import annotations

import datetime as dt

import pytest

from quantlib.features import within_day_run
from quantlib.features.within_day_trust import CertResult
from quantlib.features.within_day_version import VersionStatus

GROUP = "momentum"  # a real registered group (fail-fast lookup must pass)


def _cert(feature: str = "up_ratio_3m") -> CertResult:
    return CertResult(
        feature=feature,
        version="1.0.0",
        group_name=GROUP,
        cert_day="2026-06-18",
        status="certified",
        value_rate=1.0,
        stable_cycles=3,
        window_minutes=20,
        n_clean_symbols=10,
        n_compared=400,
        settle_lag_min=20.0,
    )


@pytest.fixture
def stub_primitives(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stub claim/release/monitor/version/queue so the orchestrator runs with no DB or live state. Returns a
    call recorder the test asserts over."""
    calls: dict[str, list] = {"claim": [], "release": [], "monitor": [], "reset": [], "pending": []}

    def claim(group: str, agent: str, **kw: object) -> bool:
        calls["claim"].append((group, agent))
        return True

    def release(group: str, agent: str, **kw: object) -> bool:
        calls["release"].append((group, agent))
        return True

    monkeypatch.setattr(within_day_run.within_day_assignment, "claim", claim)
    monkeypatch.setattr(within_day_run.within_day_assignment, "release", release)
    # version snapshot: one diverged + one matching feature (the §4 just-refactored signal).
    monkeypatch.setattr(
        within_day_run,
        "_version_snapshot",
        lambda group, dry_run: [
            ("up_ratio_3m", VersionStatus.LIVE_DIVERGED.value),
            ("mean_abs_ret_3m", VersionStatus.LIVE_MATCHES_TRUST.value),
        ],
    )
    monkeypatch.setattr(
        within_day_run,
        "_fire_version_reset",
        lambda group, dry_run: calls["reset"].append(group) or ["up_ratio_3m"],
    )
    monkeypatch.setattr(
        within_day_run, "_consult_deploy_queue", lambda group, dry_run: calls["pending"].append(group) or 2
    )
    return calls


def test_refused_claim_aborts_without_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(within_day_run.within_day_assignment, "claim", lambda *a, **k: False)
    monitored: list[str] = []
    monkeypatch.setattr(
        within_day_run.within_day_monitor, "monitor", lambda *a, **k: monitored.append("ran")
    )
    result = within_day_run.run_group_lifecycle("/store", GROUP, "agent-x")
    assert result.claimed is False
    assert result.did_certify is False
    assert monitored == []  # never ran the monitor on a refused claim


def test_certify_fires_reset_and_queue_peek(
    monkeypatch: pytest.MonkeyPatch, stub_primitives: dict[str, list]
) -> None:
    captured: dict[str, object] = {}

    def fake_monitor(feature_root: str, group: str, agent: str, **kw: object) -> CertResult:
        captured.update(kw)
        return _cert()

    monkeypatch.setattr(within_day_run.within_day_monitor, "monitor", fake_monitor)
    result = within_day_run.run_group_lifecycle(
        "/store", GROUP, "agent-x", mode="replay", day=dt.date(2026, 6, 18), max_cycles=5
    )

    assert result.did_certify is True
    assert result.certified is not None and result.certified.feature == "up_ratio_3m"
    assert result.diverged_features == ["up_ratio_3m"]  # the §4 just-refactored signal surfaced
    assert result.reset_features == ["up_ratio_3m"]  # version reset fired on certify
    assert result.queued_jobs == 2  # deploy-queue peek surfaced
    assert stub_primitives["reset"] == [GROUP]
    assert stub_primitives["pending"] == [GROUP]
    # the orchestrator already holds the lock -> the monitor must NOT re-claim it
    assert captured["claim_lock"] is False
    # released exactly once on the success path (the orchestrator's finally)
    assert stub_primitives["release"] == [(GROUP, "agent-x")]


def test_no_certify_peeks_queue_without_reset(
    monkeypatch: pytest.MonkeyPatch, stub_primitives: dict[str, list]
) -> None:
    monkeypatch.setattr(within_day_run.within_day_monitor, "monitor", lambda *a, **k: None)
    result = within_day_run.run_group_lifecycle("/store", GROUP, "agent-x", max_cycles=1)

    assert result.did_certify is False
    assert result.reset_features == []  # no reset on a no-certify run
    assert stub_primitives["reset"] == []
    assert result.queued_jobs == 2  # but still peeks the queue
    assert stub_primitives["pending"] == [GROUP]
    assert stub_primitives["release"] == [(GROUP, "agent-x")]  # lock released on the no-certify path too


def test_monitor_raise_still_releases_lock(
    monkeypatch: pytest.MonkeyPatch, stub_primitives: dict[str, list]
) -> None:
    def boom(*a: object, **k: object) -> CertResult:
        raise RuntimeError("monitor blew up")

    monkeypatch.setattr(within_day_run.within_day_monitor, "monitor", boom)
    with pytest.raises(RuntimeError, match="monitor blew up"):
        within_day_run.run_group_lifecycle("/store", GROUP, "agent-x", max_cycles=1)
    # the finally releases the lock even when the monitor raises (never leave the group locked)
    assert stub_primitives["release"] == [(GROUP, "agent-x")]


def test_unknown_group_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed: list[str] = []
    monkeypatch.setattr(
        within_day_run.within_day_assignment, "claim", lambda *a, **k: claimed.append("c") or True
    )
    with pytest.raises(KeyError):
        within_day_run.run_group_lifecycle("/store", "not_a_real_group", "agent-x")
    assert claimed == []  # fail-fast BEFORE any claim
