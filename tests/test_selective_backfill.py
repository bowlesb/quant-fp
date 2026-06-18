"""Unit tests for the selective feature/group backfill driver.

No network/DB: the registry resolution and partition-skip logic are tested with monkeypatched store +
registry helpers. The materialize worker itself is exercised only through a recording stub.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from quantlib.features import selective_backfill as sb


class _FakeCursor:
    def execute(self, *args: object) -> None:
        return None

    def fetchone(self) -> tuple[int]:
        return (1,)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_preflight_db_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable DB returns silently — the SELECT 1 round-trips."""
    monkeypatch.setattr(sb.psycopg, "connect", lambda **kwargs: _FakeConn())
    sb.preflight_db()  # no raise


def test_preflight_db_unreachable_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable DB (the missing-`--network` case that failed a run as an opaque ProcessPool
    RemoteTraceback) raises SystemExit immediately with an actionable message naming the network fix.
    """

    def _boom(**kwargs: object) -> _FakeConn:
        raise psycopg.OperationalError("failed to resolve host 'timescaledb'")

    monkeypatch.setattr(sb.psycopg, "connect", _boom)
    with pytest.raises(SystemExit, match="--network quant_default"):
        sb.preflight_db()


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


def test_materialize_day_symbol_sharding_clears_once_then_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``symbol_shard_size`` the target partitions are cleared EXACTLY ONCE up front, then each
    symbol chunk is materialized as its own shard (``data-<shard>.parquet``) so the chunks UNION on read.
    Re-clearing per chunk would delete the prior chunk's shard — the clear must NOT repeat.
    """
    calls: list[tuple] = []
    monkeypatch.setattr(
        sb.store,
        "clear_backfill_groups_day",
        lambda root, day, groups: calls.append(("clear", day, list(groups))) or [],
    )
    monkeypatch.setattr(
        sb,
        "materialize_from_raw_groups",
        lambda root, raw_root, day, symbols, groups, shard=None: calls.append(
            ("materialize", shard, list(symbols))
        )
        or len(symbols),
    )
    day, count = sb.materialize_day(
        "/store",
        "/store",
        "2025-01-02",
        ["AAPL", "NVDA", "TSLA", "AMD", "INTC"],
        ["gA", "gB"],
        symbol_shard_size=2,
    )
    # 5 symbols / chunk 2 -> chunks [AAPL,NVDA], [TSLA,AMD], [INTC]; count sums the chunks
    assert (day, count) == ("2025-01-02", 5)
    assert calls == [
        ("clear", "2025-01-02", ["gA", "gB"]),
        ("materialize", 0, ["AAPL", "NVDA"]),
        ("materialize", 1, ["TSLA", "AMD"]),
        ("materialize", 2, ["INTC"]),
    ]


def test_run_refuses_symbol_sharding_for_universe_reduce_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symbol-sharding a universe-reduce group (breadth/rank) would write a partial-universe reduction per
    chunk, silently corrupting the feature. ``run`` must REFUSE rather than produce wrong data.
    """
    monkeypatch.setattr(sb, "cross_sectional_groups", lambda: ["breadth", "gReduce"])
    with pytest.raises(SystemExit, match="universe-reduce"):
        sb.run(
            "/store",
            "/store",
            {"gA": "1.0", "gReduce": "1.0"},
            [dt.date(2025, 1, 2)],
            ["AAPL"],
            1,
            force=False,
            symbol_shard_size=500,
        )


class _InlineExecutor:
    """A synchronous stand-in for ProcessPoolExecutor so run() can be unit-tested without pickling the
    submitted callable across a process boundary."""

    def __init__(self, max_workers: int = 1) -> None:
        self._results: list[_InlineFuture] = []

    def __enter__(self) -> "_InlineExecutor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def submit(self, fn, *args):  # type: ignore[no-untyped-def]
        return _InlineFuture(fn(*args))


class _InlineFuture:
    def __init__(self, value: object) -> None:
        self._value = value

    def result(self) -> object:
        return self._value


def test_run_allows_symbol_sharding_for_per_symbol_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-symbol cohort (no universe-reduce group) passes the guard and threads the shard size through
    to the materialize worker."""
    monkeypatch.setattr(sb, "cross_sectional_groups", lambda: ["breadth"])
    monkeypatch.setattr(sb.store, "settled_dates", lambda root, g, v: set())
    monkeypatch.setattr(sb, "ProcessPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(sb, "as_completed", lambda futures: list(futures))
    seen: list[int | None] = []
    monkeypatch.setattr(
        sb,
        "materialize_day",
        lambda root, raw_root, day, symbols, groups, shard_size: seen.append(shard_size)
        or (day, 1),
    )
    sb.run(
        "/store",
        "/store",
        {"price_returns": "1.0"},
        [dt.date(2025, 1, 2)],
        ["AAPL"],
        1,
        force=False,
        symbol_shard_size=500,
    )
    assert seen == [500]


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
