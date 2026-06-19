"""On-trust SUPERSEDE-PURGE: when a (group, version) earns trust, the older untrusted versions of that
group become stale and are reclaimable (Ben's directive). These tests pin the REFUSE-GUARDS (never purge a
trusted version, never purge the newest, only purge strictly-older) + the dry-run/apply/stream-refuse/
idempotency behaviour, all DB-free (the pure guard logic is driven directly; the filesystem side uses a
tmp store)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from quantlib.features import lifecycle, store, supersede_purge

BASE = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)


def _write(root: Path, group: str, version: str, source: str, day: str) -> None:
    store.write_group(
        root,
        group,
        version,
        source,
        day,
        pl.DataFrame({"symbol": ["AAA"], "minute": [BASE], "ret_1m": [1.0]}),
    )


def _on_disk(version: str, backfill: int = 1, stream: int = 0, mb_bytes: int = 100) -> dict[str, object]:
    return {"version": version, "backfill_dates": backfill, "stream_dates": stream, "bytes": mb_bytes}


def test_strictly_older_untrusted_version_is_purgeable() -> None:
    on_disk = [_on_disk("1.0.0"), _on_disk("2.0.0")]
    candidates = supersede_purge.purge_candidates("momentum_run", on_disk, trusted_versions={"2.0.0"})
    assert [c["version"] for c in candidates] == ["1.0.0"]
    assert candidates[0]["superseded_by_trusted"] == "2.0.0"


def test_guard_refuses_newest_version() -> None:
    # the newest on-disk version is never purged, even if some OTHER trusted version is higher than itself
    on_disk = [_on_disk("1.0.0"), _on_disk("1.1.0")]
    # trusted only at 1.1.0 (the newest) -> nothing older-and-trusted-successor to purge except 1.0.0,
    # but 1.1.0 is both newest AND trusted so it is safe; 1.0.0 is purgeable
    candidates = supersede_purge.purge_candidates("price_levels", on_disk, trusted_versions={"1.1.0"})
    assert [c["version"] for c in candidates] == ["1.0.0"]


def test_guard_refuses_when_old_version_is_itself_trusted() -> None:
    # if the OLD version is itself trusted, it must NOT be purged even though a newer trusted version exists
    on_disk = [_on_disk("1.0.0"), _on_disk("2.0.0")]
    candidates = supersede_purge.purge_candidates(
        "momentum_run", on_disk, trusted_versions={"1.0.0", "2.0.0"}
    )
    assert candidates == []


def test_guard_no_trust_means_no_purge() -> None:
    # a group that earned trust at NO version supersedes nothing -> nothing is purgeable
    on_disk = [_on_disk("1.0.0"), _on_disk("2.0.0")]
    assert supersede_purge.purge_candidates("technical", on_disk, trusted_versions=set()) == []


def test_guard_only_purges_strictly_lower_not_equal() -> None:
    # a version equal to the trusted one is not strictly-older -> not purged
    on_disk = [_on_disk("2.0.0")]
    assert supersede_purge.purge_candidates("momentum_run", on_disk, trusted_versions={"2.0.0"}) == []


def test_three_versions_purges_both_older_keeps_newest_trusted() -> None:
    on_disk = [_on_disk("1.0.0"), _on_disk("1.1.0"), _on_disk("2.0.0")]
    candidates = supersede_purge.purge_candidates("price_levels", on_disk, trusted_versions={"2.0.0"})
    assert sorted(c["version"] for c in candidates) == ["1.0.0", "1.1.0"]


def test_execute_dry_run_deletes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "momentum_run", "1.0.0", "backfill", "2026-06-01")
    _write(tmp_path, "momentum_run", "2.0.0", "backfill", "2026-06-02")
    candidates = supersede_purge.plan_group_purge_pure(tmp_path, "momentum_run", {"2.0.0"})
    assert [c["version"] for c in candidates] == ["1.0.0"]
    results = supersede_purge.execute_purge(tmp_path, candidates, apply=False)
    assert results[0]["applied"] is False and results[0]["deleted"] == "dry_run"
    # file is untouched
    assert list(tmp_path.glob("group=momentum_run/v=1.0.0/**/*.parquet"))


def test_execute_apply_deletes_backfill_and_refuses_stream(tmp_path: Path) -> None:
    _write(tmp_path, "momentum_run", "1.0.0", "backfill", "2026-06-01")
    _write(tmp_path, "momentum_run", "1.0.0", "stream", "2026-06-01")
    _write(tmp_path, "momentum_run", "2.0.0", "backfill", "2026-06-02")
    candidate = supersede_purge.plan_group_purge_pure(tmp_path, "momentum_run", {"2.0.0"})[0]

    # with a stream partition present, an apply that does NOT include_stream is refused (irreplaceable)
    with pytest.raises(ValueError, match="stream"):
        supersede_purge.execute_purge(tmp_path, [candidate], apply=True, include_stream=False)
    # the backfill cache deletes fine once stream is explicitly accepted
    results = supersede_purge.execute_purge(tmp_path, [candidate], apply=True, include_stream=True)
    assert results[0]["applied"] is True
    assert not list(tmp_path.glob("group=momentum_run/v=1.0.0/**/*.parquet"))
    # the newest trusted version is untouched
    assert list(tmp_path.glob("group=momentum_run/v=2.0.0/**/*.parquet"))


def test_apply_is_idempotent(tmp_path: Path) -> None:
    _write(tmp_path, "price_levels", "1.0.0", "backfill", "2026-06-01")
    _write(tmp_path, "price_levels", "1.1.0", "backfill", "2026-06-02")
    candidates = supersede_purge.plan_group_purge_pure(tmp_path, "price_levels", {"1.1.0"})
    supersede_purge.execute_purge(tmp_path, candidates, apply=True)
    # re-planning after the purge finds the version gone -> empty plan (idempotent)
    assert supersede_purge.plan_group_purge_pure(tmp_path, "price_levels", {"1.1.0"}) == []


def test_store_versions_counts_backfill_and_stream(tmp_path: Path) -> None:
    _write(tmp_path, "momentum_run", "1.0.0", "backfill", "2026-06-01")
    _write(tmp_path, "momentum_run", "1.0.0", "stream", "2026-06-01")
    _write(tmp_path, "momentum_run", "1.0.0", "stream", "2026-06-02")
    rows = lifecycle.store_versions(tmp_path, "momentum_run")
    assert len(rows) == 1
    row = rows[0]
    assert row["version"] == "1.0.0" and row["backfill_dates"] == 1 and row["stream_dates"] == 2
