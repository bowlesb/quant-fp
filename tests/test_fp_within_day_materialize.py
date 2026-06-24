"""Tests for the live-intraday backfill MATERIALIZATION (quantlib/features/within_day_materialize.py).

Pin the routing + the dry-run safety offline (no /store/raw read, no store write):

  * a cross-sectional universe-reduce group is NOT sample-materializable intraday (returns 0 -> caller falls
    back to the swept-day backfill);
  * a bar-only group routes to the bar materialize; a tick group routes to the tick materialize;
  * dry-run writes NOTHING (no materialize call) and returns 0;
  * an empty symbol sample is a no-op.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quantlib.features import within_day_materialize as wim

DAY = dt.date(2026, 6, 18)
SYMS = ["AAA", "BBB"]


def test_cross_sectional_group_is_not_sample_materializable() -> None:
    # cross_sectional_rank reduces over the full universe -> a sample materialize would be a partial-universe
    # reduction the live stream can never match; the helper refuses (0) so the monitor falls back.
    assert wim._group_needs_full_universe("cross_sectional_rank") is True
    count = wim.materialize_settled_window(
        "/store", "/store/raw", "cross_sectional_rank", DAY, SYMS, dry_run=False
    )
    assert count == 0


def test_bar_only_group_with_raw_present_routes_to_raw_materialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A PAST swept day has the raw bar tape on disk -> read it (the download-once from-raw path).
    monkeypatch.setattr(wim, "_raw_bars_present", lambda *a, **k: True)
    routed: dict[str, object] = {}
    monkeypatch.setattr(
        wim,
        "materialize_from_raw_bar_groups",
        lambda root, raw, day, syms, groups: routed.update(kind="bar", groups=groups) or len(syms),
    )
    monkeypatch.setattr(
        wim,
        "materialize_alpaca_bar_groups",
        lambda *a, **k: routed.update(kind="alpaca") or 0,
    )
    monkeypatch.setattr(
        wim,
        "materialize_from_raw_groups",
        lambda *a, **k: routed.update(kind="tick") or 0,
    )
    count = wim.materialize_settled_window("/store", "/store", "momentum", DAY, SYMS, dry_run=False)
    assert routed["kind"] == "bar"  # momentum needs only bars; raw present -> from-raw
    assert routed["groups"] == ["momentum"]
    assert count == len(SYMS)


def test_bar_only_group_intraday_no_raw_routes_to_alpaca(monkeypatch: pytest.MonkeyPatch) -> None:
    # On the CURRENT day the raw bar tape is not yet acquired -> fetch from Alpaca (the live-intraday source).
    monkeypatch.setattr(wim, "_raw_bars_present", lambda *a, **k: False)
    routed: dict[str, object] = {}
    monkeypatch.setattr(
        wim,
        "materialize_alpaca_bar_groups",
        lambda root, day, syms, groups: routed.update(kind="alpaca", groups=groups) or len(syms),
    )
    monkeypatch.setattr(
        wim,
        "materialize_from_raw_bar_groups",
        lambda *a, **k: routed.update(kind="bar") or 0,
    )
    count = wim.materialize_settled_window("/store", "/store", "momentum", DAY, SYMS, dry_run=False)
    assert routed["kind"] == "alpaca"  # raw absent intraday -> Alpaca source
    assert routed["groups"] == ["momentum"]
    assert count == len(SYMS)


def test_tick_group_routes_to_tick_materialize(monkeypatch: pytest.MonkeyPatch) -> None:
    routed: dict[str, object] = {}
    monkeypatch.setattr(
        wim,
        "materialize_from_raw_groups",
        lambda root, raw, day, syms, groups: routed.update(kind="tick", groups=groups) or len(syms),
    )
    monkeypatch.setattr(
        wim,
        "materialize_from_raw_bar_groups",
        lambda *a, **k: routed.update(kind="bar") or 0,
    )
    count = wim.materialize_settled_window("/store", "/store/raw", "trade_flow", DAY, SYMS, dry_run=False)
    assert routed["kind"] == "tick"  # trade_flow needs trades -> tick-enriched materialize
    assert routed["groups"] == ["trade_flow"]
    assert count == len(SYMS)


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(wim, "materialize_from_raw_bar_groups", lambda *a, **k: called.append("bar"))
    monkeypatch.setattr(wim, "materialize_from_raw_groups", lambda *a, **k: called.append("tick"))
    count = wim.materialize_settled_window("/store", "/store/raw", "momentum", DAY, SYMS, dry_run=True)
    assert count == 0
    assert called == []  # dry-run never calls a materialize


def test_empty_symbols_is_noop() -> None:
    assert wim.materialize_settled_window("/store", "/store/raw", "momentum", DAY, [], dry_run=False) == 0


def test_ensure_inputs_first_runs_before_materialize(monkeypatch: pytest.MonkeyPatch) -> None:
    # ensure_inputs patches the RAW source, so it only applies on the from-raw path (raw present).
    monkeypatch.setattr(wim, "_raw_bars_present", lambda *a, **k: True)
    order: list[str] = []
    monkeypatch.setattr(
        wim,
        "ensure_window_inputs",
        lambda raw, group, day, syms, agent, dry_run: order.append("ensure") or True,
    )
    monkeypatch.setattr(
        wim,
        "materialize_from_raw_bar_groups",
        lambda *a, **k: order.append("materialize") or len(SYMS),
    )
    wim.materialize_settled_window(
        "/store", "/store", "momentum", DAY, SYMS, ensure_inputs_first=True, dry_run=False
    )
    assert order == ["ensure", "materialize"]  # source patched BEFORE the compute


def test_ensure_inputs_lock_held_raises_under_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # If ensure_inputs leaves a layer locked (source not present) a LIVE materialize must NOT compute off a
    # partial tape -> it raises so a parity 'mismatch' is never a missing-download artifact.
    monkeypatch.setattr(wim, "_raw_bars_present", lambda *a, **k: True)
    monkeypatch.setattr(wim, "ensure_window_inputs", lambda *a, **k: False)
    monkeypatch.setattr(wim, "materialize_from_raw_bar_groups", lambda *a, **k: 1)
    with pytest.raises(RuntimeError, match="source not present"):
        wim.materialize_settled_window(
            "/store", "/store", "momentum", DAY, SYMS, ensure_inputs_first=True, dry_run=False
        )
