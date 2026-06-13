"""End-to-end streaming simulation (scenario B): mock feed → live capture → mock-tagged store.

Runs the REAL capture client against the mock websocket server over a real websocket connection, at
a compressed interval, and verifies features land in a mock-tagged store (never the real one).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import websockets

from mock_stream.server import make_handler
from quantlib.features import store
from quantlib.features.capture import capture


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
