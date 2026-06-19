"""Unit tests for the SYSTEM PROGRESS scorecard (services/dashboard/scorecard + scorecard_store).

No live DB / store / gh: the trust-frontier DB reads are monkeypatched, a tiny raw manifest is written into a
tmp ``<store>/raw/_manifest_<tier>.d/`` part, a tmp latency-audit markdown is parsed, and the open-PR gh call
is stubbed — so ``build_scorecard`` is exercised end-to-end against a controlled fixture. The append-only
snapshot store and the latency-table parser are tested directly.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl
import pytest

from quantlib.data.raw_store import MANIFEST_SCHEMA

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import scorecard as sc  # noqa: E402  (path inserted above)
import scorecard_store as store  # noqa: E402


def _write_manifest(root: Path, tier: str, rows: list[dict]) -> None:
    parts_dir = root / "raw" / f"_manifest_{tier}.d"
    parts_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [
            {
                "tier": tier,
                "symbol": row["symbol"],
                "date": row["date"],
                "rows": row["rows"],
                "bytes": 100,
                "fetched_at": dt.datetime(2026, 6, 18, tzinfo=dt.timezone.utc),
            }
            for row in rows
        ],
        schema=MANIFEST_SCHEMA,
    ).write_parquet(parts_dir / "part-1-00000001.parquet")


_FAKE_FRONTIER = {
    "n_features": 694,
    "n_trusted": 106,
    "n_eligible": 532,
    "n_blocked": 56,
    "n_open_defects": 56,
    "trusted_pct": 15.3,
    "eligible_pct": 76.7,
    "blocked_pct": 8.1,
    "projected_trusted_pct": 91.9,
    "groups": [],
}


def test_parse_latency_baseline_takes_last_real_row(tmp_path: Path) -> None:
    audit = tmp_path / "SIM_LATENCY_AUDIT.md"
    audit.write_text(
        "| date | universe / shards | features | end-to-end p50 | p95 | p99 |\n"
        "|---|---|---|---|---|---|\n"
        "| 2026-06-15 | 1000 / 16 | 519 | — | — | **~603ms** |\n"
        "| 2026-06-18 | 1000 / 16 | 682 | 401ms | 616ms | **761ms** |\n",
        encoding="utf-8",
    )
    reading = sc.parse_latency_baseline(audit)
    assert reading["available"] is True
    assert reading["p50_ms"] == 401
    assert reading["p99_ms"] == 761
    assert reading["budget_ms"] == 100


def test_parse_latency_baseline_missing_file_is_unavailable(tmp_path: Path) -> None:
    reading = sc.parse_latency_baseline(tmp_path / "nope.md")
    assert reading["available"] is False
    assert reading["p50_ms"] is None and reading["p99_ms"] is None


def test_parse_latency_baseline_no_table_is_unavailable(tmp_path: Path) -> None:
    audit = tmp_path / "SIM_LATENCY_AUDIT.md"
    audit.write_text("# no table here\nsome prose without a latency row\n", encoding="utf-8")
    reading = sc.parse_latency_baseline(audit)
    assert reading["available"] is False


def test_build_scorecard_all_six_axes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_manifest(
        tmp_path,
        "bars",
        [
            {"symbol": "AAA", "date": "2025-01-02", "rows": 390},
            {"symbol": "BBB", "date": "2025-01-02", "rows": 390},
            {"symbol": "AAA", "date": "2026-06-18", "rows": 390},
        ],
    )
    _write_manifest(tmp_path, "trades", [{"symbol": "AAA", "date": "2026-06-18", "rows": 2000}])

    audit = tmp_path / "SIM_LATENCY_AUDIT.md"
    audit.write_text(
        "| date | universe / shards | features | end-to-end p50 | p95 | p99 |\n"
        "|---|---|---|---|---|---|\n"
        "| 2026-06-18 | 1000 / 16 | 682 | 401ms | 616ms | **761ms** |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sc, "build_trust_frontier", lambda: dict(_FAKE_FRONTIER))
    monkeypatch.setattr(sc, "LATENCY_AUDIT_PATH", audit)
    monkeypatch.setattr(sc, "open_pr_count", lambda repo_dir=sc.REPO_DIR: 3)

    view = sc.build_scorecard(root=str(tmp_path), repo_dir="/nonexistent")
    axes = view["axes"]

    # A — trusted (straight from the frontier)
    assert axes["A_trusted"] == {"value": 106, "total": 694, "pct": 15.3}

    # B — deployed: the LIVE bus schema (real registry), so just assert it is a sane positive shape.
    assert axes["B_deployed"]["value"] > 0
    assert axes["B_deployed"]["groups"] > 0
    assert axes["B_deployed"]["fingerprint"].startswith("0x")

    # C — process health
    assert axes["C_process_health"] == {
        "eligible": 532,
        "blocked": 56,
        "open_defects": 56,
        "projected_trusted_pct": 91.9,
    }

    # D — latency (parsed)
    assert axes["D_latency"]["p50_ms"] == 401 and axes["D_latency"]["p99_ms"] == 761

    # E — raw coverage: bars layer present with depth+breadth
    bars = axes["E_raw_coverage"]["layers"]["bars"]
    assert bars["earliest"] == "2025-01-02" and bars["latest"] == "2026-06-18"
    assert bars["n_symbols"] == 2

    # F — open issues (defects + quarantined from frontier, PRs from the stub)
    assert axes["F_open_issues"] == {"open_defects": 56, "open_prs": 3, "quarantined": 56}

    # snapshot is the headline-scalar form
    snap = view["snapshot"]
    assert snap["A_trusted"] == {"value": 106, "pct": 15.3}
    assert snap["D_latency"] == {"p50_ms": 401, "p99_ms": 761}
    assert snap["F_open_issues"]["open_prs"] == 3
    assert "bars" in snap["E_raw_coverage"]


def test_open_pr_count_handles_missing_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(sc.subprocess, "run", _raise)
    assert sc.open_pr_count("/tmp") is None


def test_snapshot_store_append_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "snaps.json"
    monkeypatch.setattr(store, "SCORECARD_STORE_PATH", path)

    store.append_snapshot({"A_trusted": {"value": 100}}, ts="2026-06-19T16:00:00Z")
    store.append_snapshot({"A_trusted": {"value": 110}}, ts="2026-06-19T17:00:00Z")
    snaps = store.read_snapshots()
    assert [s["axes"]["A_trusted"]["value"] for s in snaps] == [100, 110]  # oldest-first
    # round-trips through JSON on disk
    on_disk = json.loads(path.read_text())
    assert len(on_disk["snapshots"]) == 2


def test_snapshot_store_dedupes_same_minute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "snaps.json"
    monkeypatch.setattr(store, "SCORECARD_STORE_PATH", path)

    store.append_snapshot({"A_trusted": {"value": 100}}, ts="2026-06-19T16:00:01Z")
    store.append_snapshot({"A_trusted": {"value": 105}}, ts="2026-06-19T16:00:45Z")  # same minute -> replace
    store.append_snapshot({"A_trusted": {"value": 110}}, ts="2026-06-19T16:01:05Z")  # new minute -> append
    snaps = store.read_snapshots()
    assert len(snaps) == 2
    assert snaps[0]["axes"]["A_trusted"]["value"] == 105  # the same-minute replacement won
    assert snaps[1]["axes"]["A_trusted"]["value"] == 110


def test_snapshot_store_empty_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "SCORECARD_STORE_PATH", tmp_path / "missing.json")
    assert store.read_snapshots() == []
