"""End-to-end streaming simulation (scenario B): mock feed → live capture → mock-tagged store.

Runs the REAL capture client against the mock websocket server over a real websocket connection, at
a compressed interval, and verifies features land in a mock-tagged store (never the real one).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import websockets

from mock_stream.server import make_handler
from quantlib.features import store
from quantlib.features.capture import CaptureState, capture, process_bars

DEDUP_BASE = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)


def _dbar(symbol: str, minute: int, close: float) -> dict:
    return {"S": symbol, "o": close - 0.05, "c": close, "h": close + 0.1, "l": close - 0.1, "v": 1000.0,
            "t": (DEDUP_BASE + timedelta(minutes=minute)).isoformat()}


def test_capture_dedups_redelivered_minutes(tmp_path: Path) -> None:
    root = str(tmp_path / "mock")
    state = CaptureState()
    process_bars(state, [_dbar("AAA", 0, 100.0), _dbar("BBB", 0, 200.0)], root, "mock", "2026-06-16", 60)
    process_bars(state, [_dbar("AAA", 1, 101.0), _dbar("BBB", 1, 201.0)], root, "mock", "2026-06-16", 60)
    process_bars(state, [_dbar("AAA", 1, 101.0), _dbar("BBB", 1, 201.0)], root, "mock", "2026-06-16", 60)  # RE-DELIVERY
    df = store.get_features(["ret_1m"], "universe", DEDUP_BASE, DEDUP_BASE + timedelta(minutes=5), root, source="stream")
    assert df.height == 4  # 2 symbols x 2 minutes — re-delivered minute did NOT duplicate
    assert int(df.select(["symbol", "minute"]).is_duplicated().sum()) == 0


def test_streaming_mock_to_store(tmp_path: Path) -> None:
    root = tmp_path / "mock"

    async def run() -> int:
        async with websockets.serve(make_handler(minutes=5, interval=0.01), "localhost", 9137):
            return await capture("ws://localhost:9137", ["AAA", "BBB"], str(root), "mock", day="2026-06-16")

    minutes = asyncio.run(asyncio.wait_for(run(), timeout=20))

    assert minutes == 5
    assert store.store_mode(root) == "mock"  # data is tagged mock, kept separate from real
    df = store.get_features(
        ["ret_1m", "high_low_range_1m"],
        "universe",
        datetime(2026, 6, 16, tzinfo=timezone.utc),
        datetime(2026, 6, 16, 23, 59, tzinfo=timezone.utc),
        str(root),
        source="stream",
    )
    assert df.height > 0
    assert set(df["symbol"].unique().to_list()) >= {"AAA", "BBB"}
    assert df.filter(pl.col("ret_1m").is_not_null()).height > 0  # returns computed across minutes
