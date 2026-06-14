"""Declarative reduction engine — the batched executor must equal per-group compute_latest.

The batched path (one shared marshal + one windowed_sums kernel for many groups) is an optimization of
the per-group live path; it must produce the SAME features (within float tolerance) as calling each
group's own compute_latest. Each group's compute_latest is itself parity-guarded vs compute() by
tests/test_fp_latest.py, so this closes the loop: batched == per-group == rolling backfill.
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.declarative import compute_reduction_batch
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _minute_agg(n_sym: int = 5, n_min: int = 90) -> pl.DataFrame:
    rows = []
    for s in range(n_sym):
        for i in range(n_min):
            close = 100.0 + s * 2.0 + i * 0.1 + (0.6 if i % 3 == 0 else -0.25)
            rows.append(
                {"symbol": f"S{s}", "minute": BASE + dt.timedelta(minutes=i), "high": close + 0.3,
                 "low": close - 0.3, "close": close, "volume": 1000.0 + (i * 7 + s) % 50}
            )
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _assert_close(per: pl.DataFrame, bat: pl.DataFrame, label: str) -> None:
    assert set(bat.columns) == set(per.columns), f"{label}: column mismatch"
    per, bat = per.sort("symbol"), bat.sort("symbol").select(per.columns)
    assert per.height == bat.height
    for col in [c for c in per.columns if c not in ("symbol", "minute")]:
        joined = per.select("symbol", col).join(
            bat.select("symbol", pl.col(col).alias("_b")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_b").is_null())
                | ((pl.col(col) - pl.col("_b")).abs() <= 1e-9 + 1e-9 * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches"


def test_batched_equals_per_group() -> None:
    ctx = BatchContext(frames={"minute_agg": _minute_agg()})
    # mix reduction-only (volume, volatility) and OLS (return_dynamics) groups in one batch
    groups = [REGISTRY.get_group(name) for name in ("volume", "volatility", "return_dynamics")]
    batched = compute_reduction_batch(groups, ctx)
    for group in groups:
        _assert_close(group.compute_latest(ctx), batched[group.name], group.name)
