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

# Realistic per-symbol-per-minute tick rates for a liquid name, so the sim exercises the trade_flow /
# quote_spread / liquidity groups under a Monday-like firehose by default (configurable via env). The
# REFERENCE measurement (after-hours live, 24 liquid symbols) was ~550 trades/min + ~1700 quotes/min
# AGGREGATE across the 24, i.e. ~23 trades + ~71 quotes per symbol per minute; liquid names run hundreds
# of trades + thousands of quotes/min at the open, so these are a conservative, representative default.
DEFAULT_TRADES_PER_MIN = 24
DEFAULT_QUOTES_PER_MIN = 72


def _pack(obj: object) -> bytes:
    """msgpack with datetime=True so a python datetime encodes as the Timestamp ext alpaca-py expects."""
    return msgpack.packb(obj, datetime=True)


def _price(idx: int, minute_index: int) -> float:
    """Deterministic per-(symbol,minute) price — shared by bars/trades/quotes so they're mutually
    consistent (no randomness; runs reproducible). Separates symbols and drifts per minute."""
    return 100.0 + (idx % 500) + math.sin((minute_index + idx) / 4.0)


def _bar(symbol: str, idx: int, minute_index: int, minute: datetime) -> dict:
    """A structurally-identical 1-minute bar. OHLCV is consistent with the minute's trades."""
    price = _price(idx, minute_index)
    return {
        "T": "b", "S": symbol, "o": price - 0.05, "h": price + 0.1, "l": price - 0.1,
        "c": price, "v": 1000.0 + (minute_index * 7 + idx) % 500, "t": minute, "n": 50, "vw": price,
    }


def _trade(symbol: str, idx: int, minute_index: int, minute: datetime, seq: int, per_min: int) -> dict:
    """A protocol-faithful Alpaca trade ("T":"t"). Timestamp is SUB-MINUTE (binned into this minute by
    exchange ts — the class-H binning the capture path must agree with). Price tracks the minute price."""
    ts = minute + timedelta(seconds=(seq + 1) * 60.0 / (per_min + 1))
    price = _price(idx, minute_index) + (seq - per_min / 2.0) * 0.001
    return {"T": "t", "S": symbol, "i": minute_index * 1000 + seq, "x": "V", "p": price,
            "s": 100 + seq, "t": ts, "c": ["@"], "z": "C"}


def _quote(symbol: str, idx: int, minute_index: int, minute: datetime, seq: int, per_min: int) -> dict:
    """A protocol-faithful Alpaca quote ("T":"q") with a bid/ask straddling the minute price."""
    ts = minute + timedelta(seconds=(seq + 1) * 60.0 / (per_min + 1))
    price = _price(idx, minute_index)
    return {"T": "q", "S": symbol, "bx": "V", "bp": price - 0.02, "bs": 5 + seq,
            "ax": "V", "ap": price + 0.02, "as": 6 + seq, "t": ts, "c": ["R"], "z": "C"}


async def _send_chunked(websocket, messages: list[dict]) -> None:  # type: ignore[no-untyped-def]
    """Split into <= CHUNK-row frames like the real feed (no frame approaches the client max_size)."""
    for start in range(0, len(messages), CHUNK):
        await websocket.send(_pack(messages[start : start + CHUNK]))


def make_handler(minutes: int, interval: float, trades_per_min: int, quotes_per_min: int):  # type: ignore[no-untyped-def]
    async def handler(websocket, *args) -> None:  # type: ignore[no-untyped-def]
        await websocket.send(_pack([{"T": "success", "msg": "connected"}]))
        await websocket.recv()  # client auth frame (creds ignored — this is a mock)
        await websocket.send(_pack([{"T": "success", "msg": "authenticated"}]))
        subscribe = msgpack.unpackb(await websocket.recv())
        # subscribe to whatever the client asked for; a bars-only client still works (trades/quotes empty).
        symbols = list(subscribe.get("bars") or subscribe.get("trades") or subscribe.get("quotes") or [])
        trade_syms = list(subscribe.get("trades", []))
        quote_syms = list(subscribe.get("quotes", []))
        await websocket.send(_pack([{"T": "subscription", "trades": trade_syms, "quotes": quote_syms, "bars": symbols}]))
        for minute_index in range(minutes):
            minute = SESSION_OPEN + timedelta(minutes=minute_index)
            # Monday flow within the minute: continuous trades + quotes (sub-minute), THEN the bar that
            # summarizes them — the same ordering the real feed delivers, so the capture path aggregates
            # ticks and reconciles the bar exactly as it will on Monday.
            for seq in range(trades_per_min):
                await _send_chunked(websocket, [_trade(s, i, minute_index, minute, seq, trades_per_min)
                                                for i, s in enumerate(trade_syms)])
            for seq in range(quotes_per_min):
                await _send_chunked(websocket, [_quote(s, i, minute_index, minute, seq, quotes_per_min)
                                                for i, s in enumerate(quote_syms)])
            await _send_chunked(websocket, [_bar(s, i, minute_index, minute) for i, s in enumerate(symbols)])
            await asyncio.sleep(interval if interval > 0 else 0)  # 0 = flood for latency benchmarking
        await websocket.close()

    return handler


async def serve(host: str = "0.0.0.0", port: int = 9001) -> None:
    minutes = int(os.environ.get("MOCK_MINUTES", "390"))
    interval = float(os.environ.get("MOCK_INTERVAL_SEC", "60"))
    # Default to a realistic liquid-name tick firehose so the sim drives the full tick path (trade_flow /
    # quote_spread / liquidity); set MOCK_TRADES_PER_MIN=0 / MOCK_QUOTES_PER_MIN=0 for a bars-only run.
    trades_per_min = int(os.environ.get("MOCK_TRADES_PER_MIN", str(DEFAULT_TRADES_PER_MIN)))
    quotes_per_min = int(os.environ.get("MOCK_QUOTES_PER_MIN", str(DEFAULT_QUOTES_PER_MIN)))
    handler = make_handler(minutes, interval, trades_per_min, quotes_per_min)
    # ping_interval=None: don't keepalive-drop a client whose event loop is briefly busy computing a
    # minute (under interval=0 flood the client's loop is busy back-to-back); the real feed tolerates this.
    async with websockets.serve(handler, host, port, max_size=2**24, ping_interval=None):
        print(f"alpaca mock (msgpack) on {host}:{port} — {minutes} min @ {interval}s, "
              f"{trades_per_min} trades + {quotes_per_min} quotes/symbol/min", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(serve())
