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

from quantlib.features import BatchContext, REGISTRY

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


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
