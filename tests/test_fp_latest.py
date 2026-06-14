"""Latest-minute (aggregate-at-T) parity: the fast live form must equal the rolling form's last row.

The live path computes only minute T's value per symbol via a windowed aggregate (group_by over each
window's slice) instead of a rolling pass over the whole buffer — ~window× less work. That is a SECOND
formulation, so it is only safe behind this test: compute_latest(buffer) must equal
compute(buffer).filter(minute == T) for every feature. If it ever diverged, live would disagree with
the backfill (which keeps the rolling form) — the exact failure the platform exists to prevent.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl

import pytest

from quantlib.features import BatchContext, REGISTRY
from quantlib.features.compare import runnable
from quantlib.features.profile import build_frames

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
ALL_GROUPS = [group.name for group in REGISTRY.groups()]


@pytest.mark.parametrize("group_name", ALL_GROUPS)
def test_compute_latest_matches_rolling_for_every_group(group_name: str) -> None:
    """The generic guard: for EVERY group, the live aggregate-at-T form (compute_latest) must equal the
    backfill rolling form's last minute. Default groups pass trivially (compute_latest derives from
    compute); a group that OVERRIDES compute_latest for speed is held to byte-equality here, so a fast
    live path can never silently diverge from the certified backfill values."""
    frames = build_frames(n_tickers=40, window_min=250, daily_days=60)  # > 240m windows, warm
    if group_name not in {g.name for g in runnable(frames)}:
        pytest.skip("group inputs not present in the standard test frames")
    group = REGISTRY.get_group(group_name)
    ctx = BatchContext(frames=frames)
    rolling = group.compute(ctx)
    latest = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == latest).sort("symbol")
    actual = group.compute_latest(ctx).filter(pl.col("minute") == latest).sort("symbol").select(expected.columns)
    assert actual.height == expected.height
    # hold compute_latest to each feature's DECLARED parity tolerance — the same standard as
    # live-vs-backfill (a Rust/aggregate-at-T form may differ from the Polars rolling form only by
    # float-algorithm noise within that bound).
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    for feature in [c for c in expected.columns if c not in ("symbol", "minute")]:
        tol = tolerances[feature]
        joined = expected.select("symbol", feature).join(
            actual.select("symbol", pl.col(feature).alias("_a")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(feature).is_null() & pl.col("_a").is_null())
                | ((pl.col(feature) - pl.col("_a")).abs() <= 1e-12 + tol * pl.col(feature).abs())
            )
        )
        assert bad.height == 0, f"{group_name}.{feature}: compute_latest != compute().last on {bad.height} (tol={tol})"


def _buffer(symbols: tuple[str, ...], n: int) -> pl.DataFrame:
    rows = []
    for offset, symbol in enumerate(symbols):
        for i in range(n):
            close = 100.0 + offset * 2.0 + 5.0 * math.sin((i + offset) / 9.0) + i * 0.02
            rows.append({"symbol": symbol, "minute": BASE + timedelta(minutes=i), "close": close,
                         "volume": 800.0 + ((i * 7 + offset) % 40) * 25.0})
    return pl.DataFrame(rows)


def test_volume_latest_matches_rolling() -> None:
    frame = _buffer(("AAA", "BBB", "CCC"), 220)  # > longest window (180m), so windows are warm
    ctx = BatchContext(frames={"minute_agg": frame})
    group = REGISTRY.get_group("volume")
    latest = frame["minute"].max()
    rolling_t = group.compute(ctx).filter(pl.col("minute") == latest).sort("symbol")
    fast = group.compute_latest(ctx).sort("symbol").select(rolling_t.columns)

    assert fast.height == rolling_t.height
    for feature in [c for c in rolling_t.columns if c not in ("symbol", "minute")]:
        joined = rolling_t.select("symbol", feature).join(
            fast.select("symbol", pl.col(feature).alias("_fast")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(feature).is_null() & pl.col("_fast").is_null())
                | ((pl.col(feature) - pl.col("_fast")).abs() <= 1e-9 + 1e-9 * pl.col(feature).abs())
            )
        )
        assert bad.height == 0, f"{feature}: latest != rolling.last on {bad.height} symbols"
