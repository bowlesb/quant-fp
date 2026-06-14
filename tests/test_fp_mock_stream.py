"""The Alpaca mock emits Monday's full flow — continuous trades + quotes + bars, protocol-faithful.

Drives the handler with a fake websocket (no real socket) and asserts the wire messages match Alpaca's
shapes and ordering, so `real_capture`'s unchanged client accepts exactly what Monday will send.
"""
from __future__ import annotations

import asyncio

import msgpack

from mock_stream.alpaca_server import make_handler


class FakeWebsocket:
    """Records sent frames; replays the client's auth + subscribe frames on recv."""

    def __init__(self, client_frames: list[bytes]) -> None:
        self.sent: list[bytes] = []
        self._incoming = list(client_frames)
        self.closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def _run(symbols: list[str], trades_per_min: int, quotes_per_min: int, minutes: int = 1) -> list[dict]:
    auth = msgpack.packb({"action": "auth", "key": "k", "secret": "s"})
    sub = msgpack.packb({"action": "subscribe", "trades": symbols, "quotes": symbols, "bars": symbols})
    ws = FakeWebsocket([auth, sub])
    asyncio.run(make_handler(minutes, 0.0, trades_per_min, quotes_per_min)(ws))
    assert ws.closed
    messages: list[dict] = []
    for frame in ws.sent:
        decoded = msgpack.unpackb(frame, timestamp=3)
        messages.extend(decoded if isinstance(decoded, list) else [decoded])
    return messages


def test_full_flow_emits_trades_quotes_and_bars() -> None:
    symbols = ["AAA", "BBB", "CCC"]
    messages = _run(symbols, trades_per_min=3, quotes_per_min=2)
    by_type: dict[str, list[dict]] = {}
    for message in messages:
        by_type.setdefault(message["T"], []).append(message)
    assert len(by_type["t"]) == 3 * len(symbols)  # trades_per_min x symbols
    assert len(by_type["q"]) == 2 * len(symbols)  # quotes_per_min x symbols
    assert len(by_type["b"]) == len(symbols)  # one bar per symbol per minute
    # protocol fields present on each type
    assert {"S", "p", "s", "t"} <= set(by_type["t"][0])
    assert {"S", "bp", "ap", "bs", "as", "t"} <= set(by_type["q"][0])


def test_trades_precede_the_bar_within_a_minute() -> None:
    types_in_order = [m["T"] for m in _run(["AAA"], trades_per_min=2, quotes_per_min=1) if m["T"] in ("t", "q", "b")]
    assert types_in_order == ["t", "t", "q", "b"]  # ticks stream, then the summarizing bar


def test_bars_only_by_default_is_backcompat() -> None:
    types = {m["T"] for m in _run(["AAA", "BBB"], trades_per_min=0, quotes_per_min=0)}
    assert types == {"success", "subscription", "b"}  # no trades/quotes when rates are 0
