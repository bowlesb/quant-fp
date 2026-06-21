"""Regression test for the battery intraday-panel JOIN FAN-OUT (cartesian explosion).

The bug: ``build_intraday_panel`` joins several feature groups on (symbol, minute). A group's data for a
date can be SHARDED across multiple parquet files where the SAME (symbol, minute) recurs across shards
(measured in the real store: 2026-06-18 = 7 files, ~11k cross-file dup keys per group). Without a
per-group dedup, the concat carries duplicate keys and the inner-join across N groups MULTIPLIES them
(k files per group -> up to k^N rows per key) — a cartesian explosion (observed 3.06M rows, one symbol
with 1.5M rows over 33 distinct minutes), which inflated the #326 look-ahead sweep's ICs.

This test reconstructs the exact condition on a synthetic store (cross-shard duplicate keys in 3 groups
across 2 dates) and asserts the panel has EXACTLY one row per (symbol, minute) — no explosion.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.battery import panel as panel_mod
from quantlib.battery.panel import build_intraday_panel, panel_from_intraday_frame

# the panel samples these RTH minutes (>=09:35 ET); use the first two for a small synthetic grid.
_SAMPLE = panel_mod.INTRADAY_SAMPLE_MINUTES_UTC[:2]
_SYMBOLS = ["AAA", "BBB", "CCC"]
_DATES = ["2026-03-02", "2026-03-03"]
_GROUPS = {"g_alpha": ["a0"], "g_beta": ["b0"], "g_gamma": ["c0"]}


def _write_group_sharded(store_root, group: str, feats: list[str], date_iso: str, rows: pl.DataFrame) -> None:
    """Write a group's date partition as TWO shard files that BOTH contain every (symbol, minute) row —
    the cross-shard duplicate-key condition the real store exhibits on multi-file dates."""
    base = store_root / f"group={group}" / "v=1.0.0" / "source=backfill" / f"date={date_iso}"
    base.mkdir(parents=True, exist_ok=True)
    rows.write_parquet(base / "data_0.parquet")
    rows.write_parquet(base / "data_1.parquet")  # duplicate shard -> recurring (symbol, minute) keys


def _write_bars_sharded(store_root, date_iso: str, bars: pl.DataFrame) -> None:
    """Raw bars per symbol partition, also duplicated across two shard files per symbol."""
    for sym in bars["symbol"].unique().to_list():
        sub = bars.filter(pl.col("symbol") == sym)
        base = store_root / "raw" / "bars" / f"symbol={sym}" / f"date={date_iso}"
        base.mkdir(parents=True, exist_ok=True)
        sub.write_parquet(base / "bars_0.parquet")
        sub.write_parquet(base / "bars_1.parquet")


def _build_synthetic_store(store_root) -> int:
    """Lay down 3 sharded feature groups + raw bars over 2 dates, each with cross-shard duplicate keys.
    Returns the expected clean row count (n_symbols x n_sample_minutes x n_dates)."""
    expected = 0
    for date_iso in _DATES:
        day = dt.date.fromisoformat(date_iso)
        minutes = [dt.datetime(day.year, day.month, day.day, h, m, tzinfo=dt.timezone.utc) for (h, m) in _SAMPLE]
        records = [(sym, minute) for sym in _SYMBOLS for minute in minutes]
        expected += len(records)
        symbols = [r[0] for r in records]
        mins = [r[1] for r in records]
        for group, feats in _GROUPS.items():
            frame = pl.DataFrame({"symbol": symbols, "minute": mins})
            for col in feats:
                frame = frame.with_columns(pl.Series(col, list(range(frame.height))).cast(pl.Float64))
            _write_group_sharded(store_root, group, feats, date_iso, frame)
        # raw bars: the sample minutes need a tradeable entry (close>=$1, dollar-vol>=floor) + a forward
        # bar for the excess label. Give each symbol bars across the whole sampled span.
        bar_minutes = mins + [m + dt.timedelta(minutes=30) for m in mins]
        bar_syms = symbols + symbols
        bars = pl.DataFrame(
            {
                "symbol": bar_syms,
                "ts": bar_minutes,
                "open": [100.0] * len(bar_syms),
                "high": [101.0] * len(bar_syms),
                "low": [99.0] * len(bar_syms),
                "close": [100.0] * len(bar_syms),
                "volume": [10_000] * len(bar_syms),
                "vwap": [100.0] * len(bar_syms),
                "trade_count": [50] * len(bar_syms),
            }
        )
        _write_bars_sharded(store_root, date_iso, bars)
    return expected


def test_intraday_panel_no_fanout(tmp_path, monkeypatch) -> None:
    """>=3 groups x >=2 dates with cross-shard duplicate keys must NOT fan out: exactly one row per
    (symbol, minute), i.e. n_rows == n_symbols x n_sample_minutes x n_dates."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    expected_rows = _build_synthetic_store(store_root)
    monkeypatch.setattr(panel_mod, "STORE", str(store_root))
    # realized-cost path reads the quote tape (absent here); force the store/stub fallback.
    monkeypatch.setattr(panel_mod, "USE_REALIZED_COST", False)
    monkeypatch.setattr(panel_mod, "MIN_DOLLAR_VOL", 0.0)  # synthetic bars have tiny volume

    frame = build_intraday_panel(
        (_DATES[0], _DATES[-1]),
        feature_groups=_GROUPS,
        horizons_min=[30],
        universe_top=None,
    )

    # NO explosion: one row per (symbol, minute), exactly the clean count.
    assert frame.height == expected_rows, f"fan-out: {frame.height} rows vs expected {expected_rows}"
    dup_keys = frame.height - frame.unique(subset=["symbol", "minute"]).height
    assert dup_keys == 0, f"{dup_keys} duplicate (symbol, minute) keys leaked into the panel"

    # the Panel materialization also carries one row per key (rows/symbol bounded by n_sample_minutes).
    feature_names = [f for feats in _GROUPS.values() for f in feats]
    built = panel_from_intraday_frame(frame, feature_names)
    assert built.n_rows == expected_rows
    import numpy as np

    _, counts = np.unique(built.symbol_code, return_counts=True)
    assert counts.max() <= len(_SAMPLE) * len(_DATES)


def test_load_features_dedups_cross_shard_keys(tmp_path, monkeypatch) -> None:
    """Unit-level: _load_features_for_date returns one row per (symbol, minute) even when each group is
    written as duplicate shards (the join-fanout root cause)."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    _build_synthetic_store(store_root)
    monkeypatch.setattr(panel_mod, "STORE", str(store_root))
    feats = panel_mod._load_features_for_date(_DATES[0], _GROUPS)
    assert feats is not None
    assert feats.height == feats.unique(subset=["symbol", "minute"]).height
