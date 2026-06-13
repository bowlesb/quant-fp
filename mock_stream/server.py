"""Mock streaming server — behaves like the live data websocket (per-minute bars) so the capture
client can be BUILT and STRESS-TESTED before Monday with fake but structurally-identical data.

Standalone (own Dockerfile), zero coupling to quantlib. Same schema (T/S/o/h/l/c/v/t/n/vw), same
per-minute frequency, same websocket connection type as the real feed. A fast interval
(MOCK_INTERVAL_SEC=0.01) compresses a session into seconds for quick test loops.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime, timedelta, timezone

import websockets

SESSION_OPEN = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)  # Mon 09:30 ET


def _bar(symbol: str, idx: int, minute_index: int, minute: datetime) -> dict:
    price = 100.0 + idx + math.sin((minute_index + idx) / 4.0)
    return {
        "T": "b", "S": symbol, "o": price - 0.05, "h": price + 0.1, "l": price - 0.1,
        "c": price, "v": 1000 + minute_index, "t": minute.isoformat(), "n": 50, "vw": price,
    }


def make_handler(minutes: int, interval: float):
    async def handler(websocket, *args) -> None:
        message = json.loads(await websocket.recv())  # the client's subscribe
        symbols = message.get("symbols", [])
        await websocket.send(json.dumps([{"T": "subscription", "bars": symbols}]))
        for minute_index in range(minutes):
            minute = SESSION_OPEN + timedelta(minutes=minute_index)
            batch = [_bar(symbol, i, minute_index, minute) for i, symbol in enumerate(symbols)]
            await websocket.send(json.dumps(batch))
            await asyncio.sleep(interval)
        await websocket.close()

    return handler


async def serve(host: str = "0.0.0.0", port: int = 9001) -> None:
    minutes = int(os.environ.get("MOCK_MINUTES", "390"))
    interval = float(os.environ.get("MOCK_INTERVAL_SEC", "60"))
    async with websockets.serve(make_handler(minutes, interval), host, port):
        print(f"mock stream on {host}:{port} — {minutes} minutes @ {interval}s/minute", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(serve())
