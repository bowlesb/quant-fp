"""Unit tests for count_fano — the Fano factor (var/mean) of the per-minute trade count.

Hand-built minute_agg with known n_trades locks in the Fano math + the no-trade (mean=0) -> null edge and
the warmup edge. Live==backfill parity for this ReductionGroup is covered by the shared tests/test_fp_latest.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _minute_agg(counts: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(counts),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(counts))],
            "n_trades": counts,
        }
    )


def _row(out: pl.DataFrame, i: int) -> dict:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=i)).row(0, named=True)


def _run(frame: pl.DataFrame) -> pl.DataFrame:
    return run_group(REGISTRY.get_group("count_fano"), BatchContext(frames={"minute_agg": frame}))


def _fano(counts: list[float]) -> float:
    mean = sum(counts) / len(counts)
    var = sum((c - mean) ** 2 for c in counts) / (len(counts) - 1)  # ddof=1
    return var / mean


def test_fano_known_value() -> None:
    counts = [10.0, 20.0, 10.0, 40.0, 10.0]
    out = _run(_minute_agg(counts))
    assert _row(out, 4)["count_fano_60m"] == pytest.approx(_fano(counts))


def test_constant_count_fano_is_zero() -> None:
    # A perfectly regular (constant-count) stream has zero variance -> Fano = 0 (not null: mean > 0).
    out = _run(_minute_agg([7.0, 7.0, 7.0, 7.0]))
    assert _row(out, 3)["count_fano_60m"] == pytest.approx(0.0)


def test_clustered_counts_fano_above_one() -> None:
    # A bursty stream (idle minutes then a spike) is over-dispersed -> Fano > 1.
    out = _run(_minute_agg([0.0, 0.0, 0.0, 100.0]))
    assert _row(out, 3)["count_fano_60m"] > 1.0


def test_no_trade_window_is_null() -> None:
    # All-zero counts -> mean 0 -> ratio undefined -> null (NOT 0).
    out = _run(_minute_agg([0.0, 0.0, 0.0]))
    assert _row(out, 2)["count_fano_60m"] is None


def test_first_minute_is_warmup_null() -> None:
    # A single minute -> variance needs >= 2 points -> null.
    out = _run(_minute_agg([5.0]))
    assert _row(out, 0)["count_fano_60m"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
