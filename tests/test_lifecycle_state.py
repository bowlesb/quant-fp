"""Unit tests for the certification-lifecycle read side (services/dashboard/lifecycle_state).

The DB-touching functions are thin (three small SELECTs); the lifecycle LOGIC — deriving each group's
furthest stage UNVERIFIED -> MONITORING -> CERTIFIED -> TRUSTED from the assignment/cert/trust rows, and
rolling a day's per-feature cert stamps up to one per-group verdict — is pure and is what these tests pin.
They exercise the contract ``GET /api/lifecycle-state`` depends on, with no database:

  * a group with NO owner / cert / trust is UNVERIFIED;
  * a group with a live ``active`` assignment lock (and nothing further) is MONITORING; a released or stale
    lock does NOT count as live monitoring;
  * a group whose latest within-day verdict is ``certified`` (but not all features trusted yet) is CERTIFIED;
  * a group with ALL features trusted is TRUSTED — the terminal stage wins even over a certified/monitoring row;
  * stages are mutually-ordered: the FURTHEST reached is reported;
  * summarize() counts groups per stage;
  * the cert roll-up takes the group's WEAKEST feature (min stable_cycles, worst value_rate) and only reports
    'certified' when EVERY stamp on the latest day is certified.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import lifecycle_state as ls  # noqa: E402  (path inserted above)


def _catalog(group_features: dict[str, list[str]]) -> dict[str, list[dict[str, object]]]:
    """A minimal registry-catalog shape: {group: [{feature: name, ...}, ...]}."""
    return {
        group: [{"feature": name, "version": "1.0.0"} for name in names]
        for group, names in group_features.items()
    }


def _assignment(
    group: str, agent: str = "agent-x", status: str = "active", stale: bool = False
) -> ls.AssignmentRow:
    return ls.AssignmentRow(
        group_name=group,
        agent_id=agent,
        status=status,
        claimed_at="2026-06-21T00:00:00Z",
        heartbeat_at="2026-06-21T00:00:00Z",
        released_at=None,
        stale=stale,
    )


def _cert(group: str, status: str = "certified", stable: int = 3, rate: float | None = 1.0) -> ls.CertRow:
    return ls.CertRow(
        group_name=group,
        cert_day="2026-06-18",
        status=status,
        n_certified=1,
        n_features_stamped=1,
        stable_cycles=stable,
        window_minutes=20,
        value_rate=rate,
        reason=None,
    )


def test_unverified_when_no_owner_cert_or_trust() -> None:
    catalog = _catalog({"alpha": ["a1", "a2"]})
    groups = ls.build_group_lifecycles(catalog, [], {}, set())
    assert len(groups) == 1
    assert groups[0].stage == ls.STAGE_UNVERIFIED
    assert groups[0].n_features == 2
    assert groups[0].n_trusted == 0
    assert groups[0].n_divergent == 0
    assert groups[0].fully_trusted is False


def test_divergent_when_a_feature_failed_clean_day_and_idle() -> None:
    # A group with a feature that failed a clean-day parity check, no owner/cert/trust, is DIVERGENT
    # (broken-and-idle) — NOT collapsed into the never-started UNVERIFIED bucket.
    catalog = _catalog({"alpha": ["a1", "a2"]})
    groups = ls.build_group_lifecycles(catalog, [], {}, set(), {"a1"})
    assert groups[0].stage == ls.STAGE_DIVERGENT
    assert groups[0].n_divergent == 1


def test_active_owner_outranks_divergent() -> None:
    # A live monitoring owner lifts a group above DIVERGENT (a fix-it agent is on it) — DIVERGENT only holds
    # when the group is otherwise idle. The divergent count is still carried for visibility.
    catalog = _catalog({"alpha": ["a1"]})
    groups = ls.build_group_lifecycles(catalog, [_assignment("alpha")], {}, set(), {"a1"})
    assert groups[0].stage == ls.STAGE_MONITORING
    assert groups[0].n_divergent == 1


def test_trusted_outranks_divergent() -> None:
    # The terminal TRUSTED grant wins even when a feature has a historical clean-day DIVERGENT mark.
    catalog = _catalog({"alpha": ["a1", "a2"]})
    groups = ls.build_group_lifecycles(catalog, [], {}, {"a1", "a2"}, {"a1"})
    assert groups[0].stage == ls.STAGE_TRUSTED
    assert groups[0].n_divergent == 1


def test_summarize_counts_divergent_stage() -> None:
    catalog = _catalog({"a": ["d1"], "b": ["u1"]})
    groups = ls.build_group_lifecycles(catalog, [], {}, set(), {"d1"})
    summary = ls.summarize(groups)
    assert summary[ls.STAGE_DIVERGENT] == 1
    assert summary[ls.STAGE_UNVERIFIED] == 1
    assert sum(summary.values()) == 2


def test_monitoring_with_active_lock() -> None:
    catalog = _catalog({"alpha": ["a1"]})
    groups = ls.build_group_lifecycles(catalog, [_assignment("alpha")], {}, set())
    assert groups[0].stage == ls.STAGE_MONITORING
    assert groups[0].owner == "agent-x"
    assert groups[0].owner_status == "active"


def test_released_or_stale_lock_is_not_monitoring() -> None:
    catalog = _catalog({"alpha": ["a1"], "beta": ["b1"]})
    released = _assignment("alpha", status="released")
    stale = _assignment("beta", status="active", stale=True)
    groups = {g.group: g for g in ls.build_group_lifecycles(catalog, [released, stale], {}, set())}
    # The lock is still surfaced (owner present), but neither counts as live MONITORING -> UNVERIFIED.
    assert groups["alpha"].stage == ls.STAGE_UNVERIFIED
    assert groups["alpha"].owner == "agent-x"
    assert groups["beta"].stage == ls.STAGE_UNVERIFIED
    assert groups["beta"].owner_stale is True


def test_certified_when_latest_verdict_certified() -> None:
    catalog = _catalog({"alpha": ["a1", "a2"]})
    certs = {"alpha": _cert("alpha", status="certified")}
    # Only one of two features trusted -> not yet TRUSTED, so the certified verdict is the furthest stage.
    groups = ls.build_group_lifecycles(catalog, [], certs, {"a1"})
    assert groups[0].stage == ls.STAGE_CERTIFIED
    assert groups[0].cert_status == "certified"
    assert groups[0].cert_stable_cycles == 3
    assert groups[0].cert_value_rate == 1.0


def test_fix_pending_cert_does_not_certify() -> None:
    catalog = _catalog({"alpha": ["a1"]})
    certs = {"alpha": _cert("alpha", status="fix_pending")}
    groups = ls.build_group_lifecycles(catalog, [_assignment("alpha")], certs, set())
    # A non-certified verdict cannot lift past MONITORING (the active owner), and the verdict is still shown.
    assert groups[0].stage == ls.STAGE_MONITORING
    assert groups[0].cert_status == "fix_pending"


def test_trusted_is_terminal_over_certified_and_monitoring() -> None:
    catalog = _catalog({"alpha": ["a1", "a2"]})
    certs = {"alpha": _cert("alpha", status="certified")}
    groups = ls.build_group_lifecycles(catalog, [_assignment("alpha")], certs, {"a1", "a2"})
    assert groups[0].stage == ls.STAGE_TRUSTED
    assert groups[0].fully_trusted is True
    assert groups[0].n_trusted == 2
    # The certified evidence is preserved even at the terminal stage (the staged story Ben wants to see).
    assert groups[0].cert_status == "certified"


def test_groups_ordered_least_to_most_advanced() -> None:
    catalog = _catalog({"a_trusted": ["t1"], "b_unver": ["u1"], "c_mon": ["m1"]})
    assignments = [_assignment("c_mon")]
    groups = ls.build_group_lifecycles(catalog, assignments, {}, {"t1"})
    stages = [g.stage for g in groups]
    # unverified -> monitoring -> ... -> trusted (most outstanding work first).
    assert stages == [ls.STAGE_UNVERIFIED, ls.STAGE_MONITORING, ls.STAGE_TRUSTED]


def test_summarize_counts_per_stage() -> None:
    catalog = _catalog({"a": ["t1"], "b": ["u1"], "c": ["m1"]})
    groups = ls.build_group_lifecycles(catalog, [_assignment("c")], {}, {"t1"})
    summary = ls.summarize(groups)
    assert summary[ls.STAGE_TRUSTED] == 1
    assert summary[ls.STAGE_MONITORING] == 1
    assert summary[ls.STAGE_UNVERIFIED] == 1
    assert summary[ls.STAGE_CERTIFIED] == 0
    assert sum(summary.values()) == 3


def test_empty_group_is_not_fully_trusted() -> None:
    # A group with zero features must never be reported TRUSTED (vacuous all()).
    catalog = _catalog({"empty": []})
    groups = ls.build_group_lifecycles(catalog, [], {}, set())
    assert groups[0].fully_trusted is False
    assert groups[0].stage == ls.STAGE_UNVERIFIED


_SNAPSHOT = {
    "generated_at": "2026-06-21T03:00:00Z",
    "stage_order": ls.STAGE_ORDER,
    "summary": {"divergent": 0, "unverified": 1, "monitoring": 0, "certified": 0, "trusted": 1},
    "n_groups": 2,
    "n_features": 3,
    "n_trusted_features": 1,
    "n_divergent_features": 0,
    "active_owners": [],
    "groups": [],
}


def test_build_trend_merges_series_and_carries_cumulative() -> None:
    # Three independent per-day series merge into one chronological trend; the cumulative line is the running
    # sum of trust_grants, and a day present in only one series fills the others with zeros.
    cert_events = {"2026-06-18": (4, 4, 1), "2026-06-24": (91, 91, 10)}
    trust_grants = {"2026-06-15": 68, "2026-06-18": 9, "2026-06-24": 82}
    untrust = {"2026-06-22": 18}
    trend = ls.build_lifecycle_trend(cert_events, trust_grants, untrust)
    days = [d.day for d in trend]
    assert days == ["2026-06-15", "2026-06-18", "2026-06-22", "2026-06-24"]
    cumulative = {d.day: d.cumulative_trusted for d in trend}
    assert cumulative == {
        "2026-06-15": 68,
        "2026-06-18": 77,
        "2026-06-22": 77,  # an untrust-only day does not advance the grant line
        "2026-06-24": 159,
    }
    by_day = {d.day: d for d in trend}
    assert by_day["2026-06-18"].certs_certified == 4 and by_day["2026-06-18"].cert_groups == 1
    assert by_day["2026-06-22"].untrust_events == 18 and by_day["2026-06-22"].trust_grants == 0
    assert by_day["2026-06-24"].certs_total == 91 and by_day["2026-06-24"].trust_grants == 82


def test_build_trend_empty_is_empty() -> None:
    assert ls.build_lifecycle_trend({}, {}, {}) == []


_TREND = {
    "generated_at": "2026-06-25T07:00:00Z",
    "trusted_now": 210,
    "trend": [
        {
            "day": "2026-06-15",
            "certs_total": 0,
            "certs_certified": 0,
            "cert_groups": 0,
            "trust_grants": 68,
            "untrust_events": 0,
            "cumulative_trusted": 68,
        }
    ],
}


def test_trend_route_serves_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402  (path inserted above)
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard_app, "lifecycle_trend", lambda: _TREND)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/lifecycle-trend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trusted_now"] == 210
    assert body["trend"][0]["cumulative_trusted"] == 68


def test_trend_route_booting_when_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402  (path inserted above)
    from fastapi.testclient import TestClient

    def _raise() -> dict[str, object]:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(dashboard_app, "lifecycle_trend", _raise)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/lifecycle-trend")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True


def test_route_serves_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # The route serves the snapshot dict straight through; we patch the snapshot so no DB is touched.
    import app as dashboard_app  # noqa: E402  (path inserted above)
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard_app, "lifecycle_snapshot", lambda: _SNAPSHOT)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/lifecycle-state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["trusted"] == 1
    assert body["stage_order"] == ls.STAGE_ORDER
    assert resp.headers["cache-control"] == "no-store"


def test_route_booting_when_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unreachable trust DB returns 503 booting (the grid's first-boot convention), never a 500.
    import app as dashboard_app  # noqa: E402  (path inserted above)
    from fastapi.testclient import TestClient

    def _raise() -> dict[str, object]:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(dashboard_app, "lifecycle_snapshot", _raise)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/lifecycle-state")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True
