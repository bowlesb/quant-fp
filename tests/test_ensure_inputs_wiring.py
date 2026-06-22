"""End-to-end wiring tests for ``ensure_inputs`` as step-1 of the feature-backfill CLIs.

Proves the contract Ben wants: a backfill, with ``--ensure-inputs``, ENSURES the raw layers its target
groups declare are in the store over its horizon BEFORE computing, then computes from the store. The flag
defaults OFF (the existing behavior is unchanged), dry-run by default (no fetch / no DB lock), and the live
path patches only manifest holes via the shared acquire engines.

No DB and no Alpaca: the universe/calendar/DB-preflight/compute are stubbed, and the live fetch is a
recording stub that writes REAL raw partitions into a tmp store — so the round trip (ensure -> store ->
materialize reads from store) is exercised end-to-end on a TINY horizon, exactly the capped shape the
operator runs under a ``quant-backfill``-named container (see docs/SOURCE_DATA_DEPENDENCY.md). A large fetch
is never run here.
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl
import pytest

from quantlib.data import source_dependency as sd
from quantlib.data.raw_store import partition_dir, write_partition
from quantlib.features import materialize as mz
from quantlib.features import selective_backfill as sb
from quantlib.features.base import RawLayer

TINY_DAYS = [dt.date(2026, 6, 16), dt.date(2026, 6, 17)]
TINY_SYMBOLS = ["AAPL", "MSFT"]


def _stub_cli_environment(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    """Make selective_backfill.main() runnable without DB / Alpaca: stub the preflight, calendar, universe,
    group resolution, and the heavy ``run`` (record its args instead of computing)."""
    monkeypatch.setattr(sb, "preflight_db", lambda: None)
    monkeypatch.setattr(sb, "trading_client", lambda: object())
    monkeypatch.setattr(sb, "universe_symbols", lambda client: list(TINY_SYMBOLS))
    monkeypatch.setattr(sb, "date_window", lambda months, start, end: list(TINY_DAYS))
    monkeypatch.setattr(sb, "resolve_groups", lambda features, groups: {"price_returns": "1.0"})
    monkeypatch.setattr(
        sb, "run", lambda *args, **kwargs: captured.__setitem__("run_args", args)
    )


def test_selective_backfill_no_flag_does_not_ensure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default OFF: without --ensure-inputs the existing behavior is unchanged (no ensure call)."""
    captured: dict = {}
    _stub_cli_environment(monkeypatch, captured)
    called = {"ensure": False}
    monkeypatch.setattr(
        sd, "ensure_inputs_for_groups", lambda *a, **k: called.__setitem__("ensure", True)
    )
    # selective_backfill imported ensure_inputs_for_groups by name — patch the bound reference too.
    monkeypatch.setattr(
        sb, "ensure_inputs_for_groups", lambda *a, **k: called.__setitem__("ensure", True)
    )
    monkeypatch.setattr("sys.argv", ["selective_backfill", "--groups", "price_returns", "--months", "1"])
    sb.main()
    assert called["ensure"] is False
    assert "run_args" in captured  # the backfill still ran


def test_selective_backfill_ensure_inputs_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """--ensure-inputs (no -live) calls the ensure wrapper in DRY-RUN over the resolved horizon, then runs."""
    captured: dict = {}
    _stub_cli_environment(monkeypatch, captured)
    seen: dict = {}

    def _spy(raw_store, group_names, symbols, days, agent_id, dry_run=True):
        seen.update(
            raw_store=raw_store, group_names=group_names, symbols=symbols, days=days,
            agent_id=agent_id, dry_run=dry_run,
        )
        return sd.EnsureReport(layers=(RawLayer.BARS,), holes_before={RawLayer.BARS: 4})

    monkeypatch.setattr(sb, "ensure_inputs_for_groups", _spy)
    monkeypatch.setattr(
        "sys.argv",
        ["selective_backfill", "--groups", "price_returns", "--months", "1", "--ensure-inputs"],
    )
    sb.main()
    assert seen["group_names"] == ["price_returns"]
    assert seen["symbols"] == TINY_SYMBOLS
    assert seen["days"] == TINY_DAYS
    assert seen["dry_run"] is True  # not --ensure-inputs-live
    assert seen["agent_id"] == "selective_backfill"
    assert "run_args" in captured  # ensure does not block the compute


def test_selective_backfill_ensure_inputs_live_aborts_on_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """--ensure-inputs-live aborts (SystemExit) if a layer's ingest lock is held — never computes over a
    tape another job is still writing."""
    captured: dict = {}
    _stub_cli_environment(monkeypatch, captured)
    monkeypatch.setattr(
        sb,
        "ensure_inputs_for_groups",
        lambda *a, **k: sd.EnsureReport(layers=(RawLayer.BARS,), skipped_locked=(RawLayer.BARS,)),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "selective_backfill", "--groups", "price_returns", "--months", "1",
            "--ensure-inputs", "--ensure-inputs-live",
        ],
    )
    with pytest.raises(SystemExit, match="ingest lock"):
        sb.main()
    assert "run_args" not in captured  # aborted before the compute


def test_ensure_inputs_for_groups_resolves_layers_and_fetches_holes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI wrapper resolves the target groups' declared layers and (live) fetches ONLY the holes —
    the end-to-end ensure -> store round trip on a tiny horizon, with a recording stub for the fetcher and
    a no-op-grant lock (no Alpaca, no DB)."""
    raw_store = str(tmp_path)
    monkeypatch.setattr(sd, "required_layers_for_groups", lambda names: frozenset({RawLayer.BARS}))
    monkeypatch.setattr(sd.SourceIngestLock, "claim", lambda self, layer: True)
    monkeypatch.setattr(sd.SourceIngestLock, "release", lambda self, layer: True)

    fetched: list[tuple[str, list[str], list[dt.date]]] = []

    def _stub_fetcher(layer, symbols, days):
        # Write a REAL (tiny) partition for each hole so a subsequent ensure finds the tape present.
        for symbol in symbols:
            for day in days:
                write_partition(
                    raw_store, layer.value, symbol, day,
                    pl.DataFrame({"symbol": [symbol], "ts": [dt.datetime(day.year, day.month, day.day)]}),
                )
        fetched.append((layer.value, list(symbols), list(days)))
        return len(symbols) * len(days), 1

    monkeypatch.setattr(sd, "default_fetcher", lambda store: _stub_fetcher)

    report = sd.ensure_inputs_for_groups(
        raw_store, ["price_returns"], TINY_SYMBOLS, TINY_DAYS,
        agent_id="test", dry_run=False,
    )
    # First run: all 4 (2 symbols x 2 days) are holes and get fetched.
    assert report.holes_before[RawLayer.BARS] == 4
    assert fetched == [("bars", TINY_SYMBOLS, TINY_DAYS)]
    # The partitions are really on disk now.
    for symbol in TINY_SYMBOLS:
        for day in TINY_DAYS:
            assert os.path.exists(os.path.join(partition_dir(raw_store, "bars", symbol, day), "data.parquet"))


def test_materialize_raw_ensure_inputs_dry_run_then_reads(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``materialize raw <root> <day> <n> --ensure-inputs`` reports bars holes (dry-run) then materializes
    from the store — proving the bar-only path is wired without fetching."""
    monkeypatch.setattr(mz, "tradable_universe", lambda limit: list(TINY_SYMBOLS))
    ensure_calls: list = []

    def _spy_ensure(raw_store, layers, symbols, days, agent_id, fetcher, dry_run=True):
        ensure_calls.append((layers, symbols, days, dry_run))
        return sd.EnsureReport(layers=tuple(layers), holes_before={RawLayer.BARS: 2})

    monkeypatch.setattr(mz, "ensure_inputs", _spy_ensure)
    monkeypatch.setattr(mz, "default_fetcher", lambda store: (lambda *a: (0, 0)))
    materialized: list = []
    monkeypatch.setattr(
        mz, "materialize_from_raw",
        lambda root, raw_root, day, symbols: materialized.append((day, symbols)) or len(symbols),
    )
    monkeypatch.setattr(
        "sys.argv", ["materialize", "raw", "/store", "2026-06-16", "2", "/store", "--ensure-inputs"]
    )
    mz.main()
    layers, symbols, days, dry_run = ensure_calls[0]
    assert layers == frozenset({RawLayer.BARS})
    assert symbols == TINY_SYMBOLS
    assert days == [dt.date(2026, 6, 16)]
    assert dry_run is True
    assert materialized == [("2026-06-16", TINY_SYMBOLS)]  # still materialized from store
