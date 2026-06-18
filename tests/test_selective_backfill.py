"""Unit tests for the selective feature/group backfill driver.

No network/DB: the registry resolution and partition-skip logic are tested with monkeypatched store +
registry helpers. The materialize worker itself is exercised only through a recording stub.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quantlib.features import selective_backfill as sb


def test_resolve_groups_features_and_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sb, "_resolve", lambda name: {"f1": ("gA", "1.0"), "f2": ("gB", "2.0")}[name]
    )

    class FakeGroup:
        def __init__(self, name: str, version: str) -> None:
            self.name = name
            self.version = version

    monkeypatch.setattr(sb.REGISTRY, "get_group", lambda name: FakeGroup(name, "9.9"))
    resolved = sb.resolve_groups(["f1", "f2"], ["gC"])
    assert resolved == {"gA": "1.0", "gB": "2.0", "gC": "9.9"}


def test_pending_dates_skips_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    days = [dt.date(2025, 1, 2), dt.date(2025, 1, 3), dt.date(2025, 1, 6)]
    # gA already has 2025-01-02 on disk; gB has nothing
    coverage = {"gA": {"2025-01-02"}, "gB": set()}
    monkeypatch.setattr(sb.store, "settled_dates", lambda root, g, v: coverage[g])
    pending = sb.pending_dates("/store", {"gA": "1.0", "gB": "1.0"}, days, force=False)
    assert pending["gA"] == ["2025-01-03", "2025-01-06"]
    assert pending["gB"] == ["2025-01-02", "2025-01-03", "2025-01-06"]


def test_pending_dates_force_ignores_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    days = [dt.date(2025, 1, 2)]
    monkeypatch.setattr(sb.store, "settled_dates", lambda root, g, v: {"2025-01-02"})
    pending = sb.pending_dates("/store", {"gA": "1.0"}, days, force=True)
    assert pending["gA"] == ["2025-01-02"]


def test_trusted_target_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "trusted_names", lambda: ["f1", "f2", "f3"])
    monkeypatch.setattr(
        sb,
        "_resolve",
        lambda name: {"f1": ("gA", "1.0"), "f2": ("gA", "1.0"), "f3": ("gB", "2.0")}[
            name
        ],
    )
    # f1 + f2 share group gA -> deduped to two distinct groups
    assert sb.trusted_target_groups() == {"gA": "1.0", "gB": "2.0"}


def test_materialize_day_clears_groups_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole-partition (shard=None) write must be preceded by a group-scoped clear, so a stale
    sweep-SHARDED ``data-<chunk>.parquet`` left for the day cannot UNION with the new ``data.parquet``
    and double-count symbols (the ``data*.parquet`` read glob unions both)."""
    calls: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(
        sb.store,
        "clear_backfill_groups_day",
        lambda root, day, groups: calls.append(("clear", day, list(groups))) or [],
    )
    monkeypatch.setattr(
        sb,
        "materialize_from_raw_groups",
        lambda root, raw_root, day, symbols, groups: calls.append(
            ("materialize", day, list(groups))
        )
        or 7,
    )
    day, count = sb.materialize_day(
        "/store", "/store", "2025-01-02", ["AAPL", "NVDA"], ["gA", "gB"]
    )
    assert (day, count) == ("2025-01-02", 7)
    # clear runs FIRST, scoped to exactly the requested groups, then the materialize
    assert calls == [
        ("clear", "2025-01-02", ["gA", "gB"]),
        ("materialize", "2025-01-02", ["gA", "gB"]),
    ]


def test_run_noop_when_nothing_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.store, "settled_dates", lambda root, g, v: {"2025-01-02"})
    called = []
    monkeypatch.setattr(sb, "materialize_day", lambda *a, **k: called.append(a))
    sb.run(
        "/store",
        "/store",
        {"gA": "1.0"},
        [dt.date(2025, 1, 2)],
        ["AAPL"],
        1,
        force=False,
    )
    assert called == []  # already on disk -> no materialize
