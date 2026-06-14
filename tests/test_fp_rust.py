"""Rust kernel parity: the Rust tick-run-length kernel MUST equal a pure-Python reference.

The whole point of a Rust sub-kernel is speed without giving up parity. So the Rust output is held to
byte-equality against an independent Python implementation of the identical sequential logic. If they
ever diverge, this fails — the same discipline as live-vs-backfill, applied to the Rust↔Python seam.
The group itself (tick_runlength) is then a single code path used for both the live tape and backfill,
so live↔backfill parity follows from the tick parity harness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features import BatchContext, REGISTRY

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _ref(trades: pl.DataFrame) -> dict[tuple[str, datetime], tuple[float, float, float]]:
    """Pure-Python reference: the identical sequential per-(symbol, minute) tick run-length logic."""
    rows = trades.with_columns(pl.col("ts").dt.truncate("1m").alias("_m")).sort(["symbol", "_m", "ts"])
    out: dict[tuple[str, datetime], tuple[float, float, float]] = {}
    for (symbol, minute), grp in rows.group_by(["symbol", "_m"], maintain_order=True):
        prev = None
        cur_sign = 0
        cur_len = 0.0
        max_run = 0.0
        run_count = 0.0
        signed_vol = 0.0
        for price, size in zip(grp["price"].to_list(), grp["size"].to_list()):
            sign = 0 if prev is None else (1 if price > prev else (-1 if price < prev else 0))
            if sign != 0 and sign == cur_sign:
                cur_len += 1.0
            elif sign != 0:
                cur_sign, cur_len = sign, 1.0
                run_count += 1.0
            else:
                cur_sign, cur_len = 0, 0.0
            max_run = max(max_run, cur_len)
            signed_vol += sign * size
            prev = price
        out[(symbol, minute)] = (max_run, run_count, signed_vol)
    return out


def _trades() -> pl.DataFrame:
    # Two symbols, two minutes, hand-built price paths with known run structure.
    rows = []
    # AAA minute0: prices 10,11,12,13 (3 upticks -> run of 3), then 12 (downtick), 12 (zero), 11 (down) -> down run 2
    for k, (p, sz) in enumerate([(10.0, 5), (11.0, 5), (12.0, 5), (13.0, 5), (12.0, 5), (12.0, 5), (11.0, 5)]):
        rows.append({"symbol": "AAA", "ts": BASE + timedelta(seconds=k), "price": p, "size": float(sz)})
    # AAA minute1: 20, 19, 18 (down run 2)
    for k, p in enumerate([20.0, 19.0, 18.0]):
        rows.append({"symbol": "AAA", "ts": BASE + timedelta(minutes=1, seconds=k), "price": p, "size": 10.0})
    # BBB minute0: alternating 100,101,100,101 -> runs of length 1, several
    for k, p in enumerate([100.0, 101.0, 100.0, 101.0, 102.0, 103.0]):
        rows.append({"symbol": "BBB", "ts": BASE + timedelta(seconds=k), "price": p, "size": 2.0})
    return pl.DataFrame(rows)


def test_rust_tick_kernel_matches_python_reference() -> None:
    trades = _trades()
    out = REGISTRY.get_group("tick_runlength").compute(BatchContext(frames={"trades": trades}))
    ref = _ref(trades)
    assert out.height == len(ref)
    for row in out.iter_rows(named=True):
        key = (row["symbol"], row["minute"].replace(tzinfo=timezone.utc))
        # the reference key minute is tz-aware UTC truncated; align by (symbol, minute)
        expected = next(v for (s, m), v in ref.items() if s == row["symbol"] and m == row["minute"])
        assert row["max_signed_run_1m"] == expected[0]
        assert row["signed_run_count_1m"] == expected[1]
        assert row["tick_signed_volume_1m"] == expected[2]


def test_rust_known_values() -> None:
    trades = _trades()
    out = REGISTRY.get_group("tick_runlength").compute(BatchContext(frames={"trades": trades})).sort(["symbol", "minute"])
    aaa0 = out.filter((pl.col("symbol") == "AAA") & (pl.col("minute") == BASE)).row(0, named=True)
    assert aaa0["max_signed_run_1m"] == 3.0  # the 10->11->12->13 up-run (length 3)
    # 3 runs: up(10-13), down(13->12), then the zero-tick (12->12) breaks the run so the final
    # down-tick (12->11) starts a 3rd run.
    assert aaa0["signed_run_count_1m"] == 3.0
