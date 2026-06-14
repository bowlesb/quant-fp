"""Protocol-faithful Alpaca market-data mock — speaks the SAME msgpack wire protocol as Alpaca's live
data websocket, so the REAL ``alpaca-py`` ``StockDataStream`` connects to it via ``url_override`` and the
EXACT Monday capture code (``real_capture.run_sharded_capture``) runs unchanged. Flip one env var
(``STREAM_URL_OVERRIDE``) and the same client talks to this mock instead of the real feed.

Handshake (all frames msgpack arrays of dicts, matching alpaca-py's DataStream):
  server → [{"T":"success","msg":"connected"}]      (pushed on connect)
  client → {"action":"auth","key":..,"secret":..}   (single frame)
  server → [{"T":"success","msg":"authenticated"}]
  client → {"action":"subscribe","bars":[...]}       (alpaca-py sends this fragmented; recv reassembles)
  server → [{"T":"subscription","bars":[...]}]
  server → [{"T":"b","S":..,"o":..,"h":..,"l":..,"c":..,"v":..,"t":<ts>,"n":..,"vw":..}, ...]  per minute

The bar ``t`` field is a real ``datetime`` packed with ``msgpack.packb(..., datetime=True)`` (the msgpack
Timestamp extension), because alpaca-py decodes it via ``msg["t"].to_datetime()``. Bars are chunked
(<= CHUNK per frame) like the real feed so no single frame approaches the client's max_size.

Standalone, zero coupling to quantlib. Env: MOCK_MINUTES (session length), MOCK_INTERVAL_SEC (seconds
between minutes; 0 = flood as fast as the pipeline drains, for latency benchmarking).
"""
from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timedelta, timezone

import msgpack
import websockets

SESSION_OPEN = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # a Monday 09:30 ET
CHUNK = 1000  # bars per websocket frame (real feed also splits large universes across frames)


def _pack(obj: object) -> bytes:
    """msgpack with datetime=True so a python datetime encodes as the Timestamp ext alpaca-py expects."""
    return msgpack.packb(obj, datetime=True)


def _bar(symbol: str, idx: int, minute_index: int, minute: datetime) -> dict:
    """A structurally-identical 1-minute bar with deterministic, mildly-varying OHLCV (no randomness so
    runs are reproducible). Price separates symbols and drifts per minute so returns/vol are non-trivial."""
    price = 100.0 + (idx % 500) + math.sin((minute_index + idx) / 4.0)
    return {
        "T": "b", "S": symbol, "o": price - 0.05, "h": price + 0.1, "l": price - 0.1,
        "c": price, "v": 1000.0 + (minute_index * 7 + idx) % 500, "t": minute, "n": 50, "vw": price,
    }


def make_handler(minutes: int, interval: float):  # type: ignore[no-untyped-def]
    async def handler(websocket, *args) -> None:  # type: ignore[no-untyped-def]
        await websocket.send(_pack([{"T": "success", "msg": "connected"}]))
        await websocket.recv()  # client auth frame (creds ignored — this is a mock)
        await websocket.send(_pack([{"T": "success", "msg": "authenticated"}]))
        subscribe = msgpack.unpackb(await websocket.recv())
        symbols = list(subscribe.get("bars", []))
        await websocket.send(_pack([{"T": "subscription", "trades": [], "quotes": [], "bars": symbols}]))
        for minute_index in range(minutes):
            minute = SESSION_OPEN + timedelta(minutes=minute_index)
            for start in range(0, len(symbols), CHUNK):
                chunk = symbols[start : start + CHUNK]
                batch = [_bar(symbol, start + offset, minute_index, minute) for offset, symbol in enumerate(chunk)]
                await websocket.send(_pack(batch))
            if interval > 0:
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(0)  # yield so the client drains the queue before the next minute
        await websocket.close()

    return handler


async def serve(host: str = "0.0.0.0", port: int = 9001) -> None:
    minutes = int(os.environ.get("MOCK_MINUTES", "390"))
    interval = float(os.environ.get("MOCK_INTERVAL_SEC", "60"))
    # ping_interval=None: don't keepalive-drop a client whose event loop is briefly busy computing a
    # minute (under interval=0 flood the client's loop is busy back-to-back); the real feed tolerates this.
    async with websockets.serve(make_handler(minutes, interval), host, port, max_size=2**24, ping_interval=None):
        print(f"alpaca mock (msgpack) on {host}:{port} — {minutes} minutes @ {interval}s/minute", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(serve())
