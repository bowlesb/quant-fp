"""No-look-ahead test (FP2.f / FP_GOALS C) — the point-in-time guarantee, enforced.

A feature value at minute T must depend ONLY on data with timestamp ≤ T. We prove it by computing
features over a buffer ending at T, then appending FUTURE minutes and recomputing: every value at
minutes ≤ T must be byte-identical. A feature that peeked ahead (centered/forward window) would
change and FAIL. The audit found this guarantee had zero enforcement; this is the enforcement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features.compare import vectors

BASE = datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc)


def _minute_agg(n: int) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                "minute": BASE + timedelta(minutes=i),
                "close": 100.0 + i * 0.05 + (i % 7) * 0.3,
                "high": 100.6 + i * 0.05 + (i % 7) * 0.3,
                "low": 99.4 + i * 0.05 + (i % 7) * 0.3,
            }
            for symbol in ("AAA", "BBB")
            for i in range(n)
        ]
    )


def test_no_lookahead_bar_features() -> None:
    t = 100
    cutoff = BASE + timedelta(minutes=t)
    partial = vectors({"minute_agg": _minute_agg(t + 1)})          # minutes 0..T
    full = vectors({"minute_agg": _minute_agg(t + 60)})            # 0..T plus 59 FUTURE minutes
    a = partial.filter(pl.col("minute") <= cutoff).sort(["symbol", "minute"])
    b = full.filter(pl.col("minute") <= cutoff).sort(["symbol", "minute"])
    assert a.columns == b.columns and a.height > 0
    assert a.equals(b)  # adding future data did not change any value at minutes <= T
