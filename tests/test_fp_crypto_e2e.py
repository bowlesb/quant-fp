"""The crypto END-TO-END parity rehearsal (docs/CRYPTO_E2E.md): persist crypto inputs -> recompute the
backfill side -> compare stream vs backfill -> grade -> (crypto trust). These pin the OFFLINE mechanism
without a live websocket or a DB: the parity recompute is byte-deterministic, so stream==backfill by
construction, and the grading earns trust on the synthetic clean crypto tape. They also pin the SEPARATION:
the sweep grades over ALL minutes (no equity RTH mask) and writes a crypto-namespaced ledger, never equity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.aggregates import bucket_minute
from quantlib.features import crypto_input_store, store, trust_binary
from quantlib.features.capture import CaptureState, _bars_to_frame, process_bars
from quantlib.features.crypto_capture import EXCLUDED_GROUPS, aggregate_crypto_ticks
from quantlib.features.crypto_validation_sweep import (
    compare_crypto_groups,
    crypto_groups,
    crypto_tiers,
    materialize_crypto_backfill,
    sweep_crypto_day,
)
from quantlib.features.trust_lifecycle import clean_feature_day

# A 12:00-13:00 UTC crypto window — OUTSIDE the equity RTH session (09:30-16:00 ET == 13:30-21:00 UTC in
# summer DST), so a sweep that wrongly applied the equity rth_mask would drop every minute and grade nothing.
_BASE = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD"]
_DAY = "2026-06-19"


def _synthetic_minute(minute: datetime, symbols: list[str]) -> tuple[list[dict], list[dict]]:
    bars = [
        {
            "S": s,
            "o": 100.0 + i,
            "c": 100.4 + i,
            "h": 100.7 + i,
            "l": 99.5 + i,
            "v": 1000.0 + 10 * i,
            "t": minute.isoformat(),
        }
        for i, s in enumerate(symbols)
    ]
    trades = [
        {"S": s, "p": 100.0 + k * 0.01, "s": 0.5 + k * 0.05, "ts_epoch": minute.timestamp() + k}
        for s in symbols
        for k in range(15)
    ]
    return bars, trades


def _drive_crypto_capture(crypto_root: str, n_minutes: int = 12) -> None:
    """Drive the SAME crypto compute core the live container runs: write source=stream features AND persist
    the minute_agg/trades inputs (the live path with FP_CRYPTO_PERSIST_INPUTS on)."""
    state = CaptureState()
    tick_states: dict = {}
    for offset in range(n_minutes):
        minute = _BASE + timedelta(minutes=offset)
        bars, trades = _synthetic_minute(minute, _SYMBOLS)
        enriched, trades_df = aggregate_crypto_ticks(
            bars, trades, bucket_minute(minute.timestamp()), tick_states
        )
        extra = {"trades": trades_df} if trades_df.height else None
        # Persist inputs (what crypto_capture.dispatch does under FP_CRYPTO_PERSIST_INPUTS=1).
        crypto_input_store.write_input(crypto_root, "minute_agg", _DAY, _bars_to_frame(enriched), minute)
        if trades_df.height:
            crypto_input_store.write_input(crypto_root, "trades", _DAY, trades_df, minute)
        process_bars(
            state,
            enriched,
            crypto_root,
            "real",
            day=None,
            window=300,
            snapshots=None,
            exclude_groups=EXCLUDED_GROUPS,
            extra_frames=extra,
        )


def test_input_store_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Persisted minute_agg inputs read back as the union of the per-minute files; empty frames write no file."""
    root = str(tmp_path)
    minute = _BASE
    frame = _bars_to_frame(_synthetic_minute(minute, _SYMBOLS)[0])
    crypto_input_store.write_input(root, "minute_agg", _DAY, frame, minute)
    crypto_input_store.write_input(root, "minute_agg", _DAY, pl.DataFrame(), minute + timedelta(minutes=1))
    loaded = crypto_input_store.load_input(root, "minute_agg", _DAY)
    assert loaded.height == len(_SYMBOLS)  # one minute's rows; the empty frame wrote nothing
    assert crypto_input_store.input_days(root) == [_DAY]


def test_persist_gate_is_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(crypto_input_store.PERSIST_ENV, raising=False)
    assert crypto_input_store.persist_inputs_enabled() is False
    monkeypatch.setenv(crypto_input_store.PERSIST_ENV, "1")
    assert crypto_input_store.persist_inputs_enabled() is True


def test_backfill_recompute_matches_stream(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The crypto backfill recompute (source=backfill, from persisted inputs) == the live source=stream emit,
    cell-for-cell, for the universal groups — the core parity claim. Graded over a 12:00 UTC window (NOT RTH),
    proving the sweep does NOT apply the equity rth_mask."""
    root = str(tmp_path)
    _drive_crypto_capture(root)
    materialized = materialize_crypto_backfill(root, _DAY)
    assert materialized == len(_SYMBOLS)

    discovered = store.stream_symbols_on(root, _DAY)
    assert sorted(discovered) == sorted(_SYMBOLS)

    groups = crypto_groups(root, _DAY)
    assert "price_returns" in groups and "volatility" in groups  # universal groups present
    assert not (set(groups) & set(EXCLUDED_GROUPS))  # SPY-relative groups excluded

    tiers = crypto_tiers(discovered)
    tolerance_of = trust_binary.cell_tolerance_map()
    result = compare_crypto_groups(root, _DAY, discovered, tiers, groups, tolerance_of)
    assert result.feature_day.height > 0, "expected graded features on the crypto window"
    # Deterministic recompute -> zero mismatches across every graded feature (parity-true by construction).
    assert int(result.feature_day["n_mismatch"].sum()) == 0
    assert result.exceptions.height == 0
    # And every graded feature actually compared cells (not all-missing) over the non-RTH window.
    assert int(result.feature_day["n_compared"].sum()) > 0


def test_clean_grade_earns_trust(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The contamination-aware grade (clean_feature_day -> earned_features) earns trust for the parity-true
    crypto features — the same grading the equity sweep runs, here on crypto. (Stops BEFORE the DB write, which
    crypto_trust.write_crypto_grants owns; this pins that the grade FEEDS a non-empty earned set.)"""
    root = str(tmp_path)
    _drive_crypto_capture(root)
    materialize_crypto_backfill(root, _DAY)
    discovered = store.stream_symbols_on(root, _DAY)
    groups = crypto_groups(root, _DAY)
    tiers = crypto_tiers(discovered)
    result = compare_crypto_groups(root, _DAY, discovered, tiers, groups, trust_binary.cell_tolerance_map())

    clean_today = clean_feature_day(result.cell, discovered, _DAY)
    assert clean_today.height > 0 and bool(clean_today["passed"].any())
    earned = trust_binary.earned_features(clean_today, trust_binary.feature_policy_map())
    assert earned, "a parity-true clean crypto day should earn at least one trusted feature"


def test_sweep_no_inputs_is_noop(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A day with no persisted crypto inputs sweeps to a clean no-op (nothing to recompute), never crashes."""
    summary = sweep_crypto_day(str(tmp_path), str(tmp_path), _DAY)
    assert summary["day"] == _DAY
    assert "no persisted crypto inputs" in summary["note"]


def test_crypto_tiers_single_tier() -> None:
    tiers = crypto_tiers(["BTCUSD", "ETHUSD"])
    assert tiers["tier"].to_list() == [1, 1]
    assert tiers.schema["tier"] == pl.Int32
