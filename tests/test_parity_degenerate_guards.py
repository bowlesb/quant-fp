"""Regression tests for the DataIntegrity-4 parity defect: degenerate flat/illiquid windows must emit
NULL (not +/-Infinity or NaN) so the stream and backfill paths AGREE (parity-true).

The defect: a numerically-flat 20m window gives std ~1e-9, a bare `std > 0` guard passes, and
(close - sma)/(2*std) overflows to +/-inf on the stream path while backfill emits null/finite at the
same cell -> a stream-vs-backfill divergence that blocks the feature from ever validating. The fix is
a RELATIVE-threshold guard emitting NULL. These tests assert: NO +/-inf, NO NaN, and NULL on the
degenerate window; and that normal windows are unaffected.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl

import pytest

from quantlib.features import BatchContext, REGISTRY, run_group
from quantlib.features.compare import runnable
from quantlib.features.profile import build_frames

BASE = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)


def _flat_then_move(
    symbol: str, flat_price: float, n_flat: int, move_to: float
) -> pl.DataFrame:
    """A perfectly-FLAT illiquid window (constant close/high/low/volume) followed by one move — the
    degenerate case (BZFD-style). All minutes present (no gaps)."""
    rows = []
    for i in range(n_flat):
        rows.append(
            {
                "symbol": symbol,
                "minute": BASE + timedelta(minutes=i),
                "open": flat_price,
                "high": flat_price,
                "low": flat_price,
                "close": flat_price,
                "volume": 100.0,
            }
        )
    rows.append(
        {
            "symbol": symbol,
            "minute": BASE + timedelta(minutes=n_flat),
            "open": flat_price,
            "high": move_to,
            "low": flat_price,
            "close": move_to,
            "volume": 100.0,
        }
    )
    return pl.DataFrame(rows)


def _assert_finite_or_null(out: pl.DataFrame, col: str) -> None:
    series = out[col]
    finite = series.drop_nulls()
    if finite.len() == 0:
        return
    arr = finite.to_list()
    assert all(
        math.isfinite(x) for x in arr
    ), f"{col} has non-finite (inf/NaN) values: {arr}"


def test_technical_bb_position_no_inf_on_flat_window() -> None:
    # 25 flat minutes -> the 20m Bollinger std is ~0 across the flat region (the BZFD inf case).
    ctx = BatchContext(frames={"minute_agg": _flat_then_move("BZFD", 1.23, 25, 1.50)})
    out = run_group(REGISTRY.get_group("technical"), ctx)
    for col in ("bb_position_20m", "bb_width_20m", "rsi_14m"):
        _assert_finite_or_null(out, col)
    # the fully-flat early minutes must be NULL for bb_position (degenerate, not +/-inf).
    early = out.filter(pl.col("minute") == BASE + timedelta(minutes=10)).row(
        0, named=True
    )
    assert early["bb_position_20m"] is None


def test_price_levels_position_in_range_no_nan_on_flat_window() -> None:
    ctx = BatchContext(frames={"minute_agg": _flat_then_move("BZFD", 1.23, 25, 1.50)})
    out = run_group(REGISTRY.get_group("price_levels"), ctx)
    for w in (5, 10, 15):
        _assert_finite_or_null(out, f"position_in_range_{w}m")
    early = out.filter(pl.col("minute") == BASE + timedelta(minutes=8)).row(
        0, named=True
    )
    assert (
        early["position_in_range_5m"] is None
    )  # flat window -> zero range -> NULL, not NaN


def test_volume_zscore_no_nan_on_constant_volume() -> None:
    # constant volume across the window -> std 0 -> z-score degenerate.
    ctx = BatchContext(frames={"minute_agg": _flat_then_move("BZFD", 5.0, 25, 5.0)})
    out = run_group(REGISTRY.get_group("volume"), ctx)
    for w in (5, 10, 15):
        _assert_finite_or_null(out, f"volume_zscore_{w}m")
    early = out.filter(pl.col("minute") == BASE + timedelta(minutes=10)).row(
        0, named=True
    )
    assert early["volume_zscore_5m"] is None


def _near_flat(symbol: str, base_price: float, n: int) -> pl.DataFrame:
    """A NEAR-flat window (sub-epsilon float jitter, NOT exactly constant) — the residual parity case the
    exact-zero-std tests miss. Backfill ``rolling_std_by`` yields a tiny FINITE std here while live
    ``rust_reductions`` yields NaN; polars orders NaN as the largest float, so a bare ``std > threshold``
    guard passes for the NaN and the live path emits NaN where backfill emits NULL."""
    rows = []
    for i in range(n):
        close = base_price + (1e-9 if i % 2 else 0.0)
        rows.append(
            {
                "symbol": symbol,
                "minute": BASE + timedelta(minutes=i),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 100.0,
            }
        )
    return pl.DataFrame(rows)


def test_technical_bb_position_parity_on_near_flat_window() -> None:
    # The live (NaN-std) and backfill (finite-tiny-std) paths must AGREE on a near-flat window: both NULL.
    group = REGISTRY.get_group("technical")
    ctx = BatchContext(frames={"minute_agg": _near_flat("ILLQ", 5.0, 25)})
    backfill = group.compute(ctx).sort("minute")
    last_backfill = backfill.filter(
        pl.col("minute") == backfill["minute"].max()
    ).row(0, named=True)
    live = group.compute_latest(ctx).row(0, named=True)
    for col in ("bb_position_20m", "bb_width_20m"):
        _assert_finite_or_null(group.compute_latest(ctx), col)
        assert last_backfill[col] is None, f"backfill {col} should be NULL on near-flat window"
        assert live[col] is None, f"live {col} should be NULL on near-flat window (was NaN)"


def _frames_with_near_flat_symbol(symbol: str, base_price: float) -> dict[str, pl.DataFrame]:
    """The standard profiler frames with ONE symbol's intraday window replaced by a NEAR-flat illiquid
    path (sub-epsilon jitter) — the degenerate condition that splits the live rust-kernel std (NaN) from
    the backfill rolling std (finite-tiny). Every other symbol keeps its varying data so a group's normal
    cells are unaffected; the near-flat symbol is the one that trips an unguarded ``value > threshold``."""
    frames = build_frames(n_tickers=8, window_min=120, daily_days=60)
    intraday = frames["minute_agg"]
    # Rename one existing symbol to ``symbol`` and OVERWRITE only its price columns with a near-flat path
    # (sub-epsilon jitter), preserving every other column the frame carries (volume / order-flow / quote).
    target = intraday["symbol"].unique().sort().to_list()[0]
    jitter = pl.when(pl.int_range(pl.len()).over("symbol") % 2 == 1).then(1e-9).otherwise(0.0)
    near_flat = intraday.filter(pl.col("symbol") == target).with_columns(
        pl.lit(symbol).alias("symbol"),
        *[(pl.lit(base_price) + jitter).alias(col) for col in ("open", "high", "low", "close")],
    )
    frames["minute_agg"] = pl.concat(
        [intraday.filter(pl.col("symbol") != target), near_flat]
    ).sort(["symbol", "minute"])
    return frames


@pytest.mark.parametrize("group_name", [group.name for group in REGISTRY.groups()])
def test_compute_latest_parity_on_near_flat_symbol_for_every_group(group_name: str) -> None:
    """Preventive net for the #122 NaN>threshold bug class across EVERY group, not just the three with a
    hand-written test above.

    The class: a degenerate near-flat window makes the LIVE ``rust_reductions`` std (or any reduction) emit
    NaN while the BACKFILL polars rolling form emits a tiny FINITE value; polars orders NaN as the largest
    float, so an unguarded ``value > threshold`` guard passes for the NaN and the live path emits NaN where
    backfill emits NULL — a stream-vs-backfill divergence (fixed in technical/bb_position by #122 with an
    ``is_finite()`` gate). The generic ``test_fp_latest`` per-group check uses only VARYING synthetic data,
    so it never hits a flat window and would miss a NEW group that reintroduces this class. This injects a
    near-flat illiquid symbol into the standard frames and holds ``compute_latest`` to ``compute().last``
    on THAT symbol for every runnable group: any null-vs-value mismatch or non-finite live cell fails."""
    symbol = "ILLQ"
    frames = _frames_with_near_flat_symbol(symbol, 5.0)
    if group_name not in {g.name for g in runnable(frames)}:
        pytest.skip("group inputs not present in the standard test frames")
    group = REGISTRY.get_group(group_name)
    ctx = BatchContext(frames=frames)
    rolling = group.compute(ctx)
    if rolling.height == 0 or symbol not in rolling["symbol"].to_list():
        pytest.skip("group emits no row for the near-flat symbol")
    latest = rolling["minute"].max()
    expected = (
        rolling.filter((pl.col("minute") == latest) & (pl.col("symbol") == symbol))
        .sort("symbol")
    )
    actual = (
        group.compute_latest(ctx)
        .filter(pl.col("symbol") == symbol)
        .sort("symbol")
        .select(expected.columns)
    )
    assert actual.height == expected.height
    for feature in [c for c in expected.columns if c not in ("symbol", "minute")]:
        back_val = expected[feature].to_list()[0] if expected.height else None
        live_val = actual[feature].to_list()[0] if actual.height else None
        # A non-finite LIVE value is the bug's fingerprint (NaN sailed through an unguarded guard).
        if live_val is not None:
            assert math.isfinite(
                live_val
            ), f"{group_name}.{feature}: live compute_latest emitted non-finite {live_val} on a near-flat window"
        # null-vs-value mismatch is the #122 parity break (live NaN/value where backfill is NULL or vice versa).
        assert (back_val is None) == (
            live_val is None
        ), f"{group_name}.{feature}: live={live_val} backfill={back_val} disagree on null-ness (near-flat parity break)"


def test_normal_window_values_are_finite_and_present() -> None:
    # a genuinely varying window must still PRODUCE finite values (the guard didn't over-null).
    rows = []
    for i in range(30):
        px = 100.0 + (i % 7) * 0.5  # real variation -> non-degenerate std/range
        rows.append(
            {
                "symbol": "AAA",
                "minute": BASE + timedelta(minutes=i),
                "open": px,
                "high": px + 0.3,
                "low": px - 0.3,
                "close": px,
                "volume": 1000.0 + (i % 5) * 200.0,
            }
        )
    ctx = BatchContext(frames={"minute_agg": pl.DataFrame(rows)})
    tech = run_group(REGISTRY.get_group("technical"), ctx)
    last = tech.filter(pl.col("minute") == BASE + timedelta(minutes=29)).row(
        0, named=True
    )
    assert last["bb_position_20m"] is not None and math.isfinite(
        last["bb_position_20m"]
    )
    assert last["rsi_14m"] is not None and math.isfinite(last["rsi_14m"])
