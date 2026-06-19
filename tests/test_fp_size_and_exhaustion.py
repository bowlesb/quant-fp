"""Unit tests for the trade_size_dist (Layer C, trades frame) and volume_exhaustion (Layer A,
minute_agg) feature groups.

Hand-built frames with known geometry pin the per-cell values. ``trade_size_dist`` is a trades-frame
group whose synthetic parity is covered by the T+1 real-data harness (the generic ``test_fp_latest``
skips it because the standard test frames carry no ``trades`` frame — same as tick_runlength /
microstructure_burst); these tests pin its math directly. ``volume_exhaustion`` is a ReductionGroup, so
its compute_latest==compute parity is auto-guarded by ``test_fp_latest``; here we pin the formulas.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _row(out: pl.DataFrame, minute: datetime) -> dict:
    return out.filter(pl.col("minute") == minute).row(0, named=True)


# --- trade_size_dist ---


def _trades(rows: list[tuple[datetime, float]]) -> pl.DataFrame:
    """rows = list of (ts, size); price is constant (size buckets don't depend on price)."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(rows),
            "ts": [r[0] for r in rows],
            "price": [100.0] * len(rows),
            "size": [r[1] for r in rows],
        }
    )


def test_trade_size_dist_buckets() -> None:
    # Minute 0: 4 trades — 50 (odd), 100 (round, not odd), 200 (round), 10000 (round AND institutional).
    #   odd = 1/4 = 0.25 ; round (multiple of 100) = 3/4 = 0.75 ; institutional (>=10k) = 1/4 = 0.25
    m0 = BASE
    rows = [
        (m0 + timedelta(seconds=1), 50.0),
        (m0 + timedelta(seconds=2), 100.0),
        (m0 + timedelta(seconds=3), 200.0),
        (m0 + timedelta(seconds=4), 10_000.0),
    ]
    # Minute 1: 2 trades — both odd (1 and 99), neither a round lot, neither institutional.
    m1 = BASE + timedelta(minutes=1)
    rows += [(m1 + timedelta(seconds=1), 1.0), (m1 + timedelta(seconds=2), 99.0)]
    out = run_group(REGISTRY.get_group("trade_size_dist"), BatchContext(frames={"trades": _trades(rows)}))

    r0 = _row(out, m0)
    assert r0["odd_lot_ratio_1m"] == pytest.approx(0.25)
    assert r0["round_lot_ratio_1m"] == pytest.approx(0.75)
    assert r0["institutional_trade_ratio_1m"] == pytest.approx(0.25)

    r1 = _row(out, m1)
    assert r1["odd_lot_ratio_1m"] == pytest.approx(1.0)
    assert r1["round_lot_ratio_1m"] == pytest.approx(0.0)
    assert r1["institutional_trade_ratio_1m"] == pytest.approx(0.0)


def test_trade_size_dist_empty_frame() -> None:
    empty = pl.DataFrame(schema={"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64})
    out = run_group(REGISTRY.get_group("trade_size_dist"), BatchContext(frames={"trades": empty}))
    assert out.height == 0
    assert set(out.columns) == {"symbol", "minute", "odd_lot_ratio_1m", "round_lot_ratio_1m", "institutional_trade_ratio_1m"}


# --- volume_exhaustion ---


def _ohlcv(bars: list[tuple[float, float, float]]) -> pl.DataFrame:
    """bars = list of (open, close, volume) on a contiguous one-minute AAA grid. high/low are filled
    consistently (not read by this group) so the frame is well-formed."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(bars),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(bars))],
            "open": [b[0] for b in bars],
            "high": [max(b[0], b[1]) for b in bars],
            "low": [min(b[0], b[1]) for b in bars],
            "close": [b[1] for b in bars],
            "volume": [b[2] for b in bars],
        }
    )


def test_volume_exhaustion_down_up_and_dryup() -> None:
    # 5 bars. up bars (close>open): m0 (vol 100), m2 (vol 100). down bars (close<open): m1 (vol 300),
    # m3 (vol 100). m4 is a doji-ish flat-ish but make it up with small volume to test dry-up at T.
    bars = [
        (100.0, 101.0, 100.0),  # m0 up, vol 100
        (101.0, 100.0, 300.0),  # m1 down, vol 300
        (100.0, 101.0, 100.0),  # m2 up, vol 100
        (101.0, 100.0, 100.0),  # m3 down, vol 100
        (100.0, 100.5, 20.0),   # m4 up, vol 20 (the latest minute — quiet)
    ]
    out = run_group(REGISTRY.get_group("volume_exhaustion"), BatchContext(frames={"minute_agg": _ohlcv(bars)}))
    m4 = BASE + timedelta(minutes=4)
    r = _row(out, m4)

    # 5m window covers m0..m4: down vol = 300 (m1) + 100 (m3) = 400 ; up vol = 100 (m0) + 100 (m2) + 20 (m4) = 220
    assert r["vol_down_up_ratio_5m"] == pytest.approx(400.0 / 220.0)

    # dry-up 5m: latest vol (20) / mean vol over the 5 bars ((100+300+100+100+20)/5 = 124) = 20/124
    assert r["vol_dryup_5m"] == pytest.approx(20.0 / 124.0)


def test_volume_exhaustion_contraction_ratio() -> None:
    # 60 contiguous bars: first 30 with volume 200, last 30 with volume 100 (participation halved).
    # vol_contraction_10_60m at the last bar = mean(last 10) / mean(last 60).
    #   last 10 bars are all volume 100 -> mean 100.
    #   last 60 bars: 30*200 + 30*100 = 9000 over 60 -> mean 150.
    #   ratio = 100/150 = 0.6667 (< 1 -> contracting), all up bars so down/up handled elsewhere.
    bars = [(100.0, 101.0, 200.0) for _ in range(30)] + [(100.0, 101.0, 100.0) for _ in range(30)]
    out = run_group(REGISTRY.get_group("volume_exhaustion"), BatchContext(frames={"minute_agg": _ohlcv(bars)}))
    last = BASE + timedelta(minutes=59)
    r = _row(out, last)
    assert r["vol_contraction_10_60m"] == pytest.approx(100.0 / 150.0)
    # No down bars in the whole frame -> down/up ratio is 0 (down vol 0, up vol > 0), not null.
    assert r["vol_down_up_ratio_60m"] == pytest.approx(0.0)


def test_volume_exhaustion_no_up_volume_is_null() -> None:
    # All down bars -> up volume is 0 over every window -> down/up ratio undefined -> null.
    bars = [(101.0, 100.0, 100.0) for _ in range(6)]
    out = run_group(REGISTRY.get_group("volume_exhaustion"), BatchContext(frames={"minute_agg": _ohlcv(bars)}))
    r = _row(out, BASE + timedelta(minutes=5))
    assert r["vol_down_up_ratio_5m"] is None
