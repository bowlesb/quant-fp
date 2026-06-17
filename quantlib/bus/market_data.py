"""Opt-in RAW market-data streams (``md:``) — a channel SEPARATE from the feature-vector bus (``fv:``).

The feature-vector bus (codec.py / publisher.py / consumer.py) carries the packed, fingerprinted feature
vector. This module carries the *raw inputs* — the minute's OHLCV bar and its individual trades/quotes —
so a strategy container or the research/Modelling Agent can subscribe to ticks and minute bars LIVE
without re-deriving them. It is entirely opt-in: nothing here is on the feature fingerprint, and the
producer publishes nothing unless an env flag is set (see ``quantlib.features.real_capture``).

Streams (one per symbol per tier):

    md:bar:<symbol>           one OHLCV bar frame per minute (per-minute tier)
    md:trades:<symbol>        the minute's trades, one batched frame per minute (per-minute tier)
    md:quotes:<symbol>        the minute's quotes, one batched frame per minute (per-minute tier)
    md:tick_trades:<symbol>   one frame per individual trade (tick-firehose tier, heavy, opt-in)
    md:tick_quotes:<symbol>   one frame per individual quote (tick-firehose tier, heavy, opt-in)

Frames have their OWN compact little-endian layout with a small magic + version — wholly independent of
the ``fv:`` ``FVB1`` fingerprint. A bar frame is a fixed header. A tick frame is a header + a contiguous
float64 payload of N records, decoded as a zero-copy numpy VIEW (no per-record allocation), mirroring the
codec.py style.

Bar frame ``MDB1`` (little-endian):

    magic       4 bytes   b"MDB1"
    minute_us   int64     UTC epoch microseconds of the bar minute
    open        float64
    high        float64
    low         float64
    close       float64
    volume      float64
    symbol_len  uint16
    symbol      symbol_len bytes

Tick frame ``MDT1`` (trades) / ``MDQ1`` (quotes), little-endian:

    magic       4 bytes   b"MDT1" | b"MDQ1"
    minute_us   int64     UTC epoch microseconds of the bucketing minute
    n_records   uint32    number of records in the payload
    n_cols      uint16    floats per record (trades=3 [ts_us, price, size]; quotes=5 [ts_us, bid, ask, bid_sz, ask_sz])
    symbol_len  uint16    UTF-8 byte length of the symbol
    symbol      symbol_len bytes
    payload     n_records * n_cols * float64   row-major (record-major) raw values
"""
from __future__ import annotations

import datetime as dt
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import redis

from quantlib.bus.publisher import DEFAULT_REDIS_URL

MD_STREAM_PREFIX = "md"

BAR_MAGIC = b"MDB1"
TRADES_MAGIC = b"MDT1"
QUOTES_MAGIC = b"MDQ1"

TRADE_COLS = 3  # ts_us, price, size
QUOTE_COLS = 5  # ts_us, bid_price, ask_price, bid_size, ask_size

_BAR_HEADER_FMT = "<4sqdddddH"
_BAR_HEADER_SIZE = struct.calcsize(_BAR_HEADER_FMT)
_TICK_HEADER_FMT = "<4sqIHH"
_TICK_HEADER_SIZE = struct.calcsize(_TICK_HEADER_FMT)

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

# md: stream tiers, keyed by the short stream name a consumer declares. magic is None for per-minute
# trades/quotes whose magic depends on kind — handled explicitly in the decode dispatch.
BAR_STREAM = "bar"
TRADES_STREAM = "trades"
QUOTES_STREAM = "quotes"
TICK_TRADES_STREAM = "tick_trades"
TICK_QUOTES_STREAM = "tick_quotes"
ALL_STREAMS = (BAR_STREAM, TRADES_STREAM, QUOTES_STREAM, TICK_TRADES_STREAM, TICK_QUOTES_STREAM)

FRAME_FIELD = b"d"

# Bounded retention per stream (approximate MAXLEN trim — cheap): minute tiers keep ~4h; the tick
# firehose keeps a short rolling window so a heavy symbol can't grow Redis without bound.
DEFAULT_MINUTE_MAXLEN = 240
DEFAULT_TICK_MAXLEN = 20_000


def md_stream_key(stream: str, symbol: str, prefix: str = MD_STREAM_PREFIX) -> str:
    return f"{prefix}:{stream}:{symbol}"


def _epoch_us(minute: dt.datetime | int) -> int:
    if isinstance(minute, int):
        return minute
    if minute.tzinfo is None:
        minute = minute.replace(tzinfo=dt.timezone.utc)
    delta = minute - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _us_to_datetime(epoch_us: int) -> dt.datetime:
    return _EPOCH + dt.timedelta(microseconds=epoch_us)


@dataclass(frozen=True)
class BarRecord:
    """A decoded raw OHLCV minute bar from ``md:bar:<symbol>``."""

    symbol: str
    minute: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class TickBatch:
    """A decoded batch of raw trades or quotes. ``rows`` is a zero-copy ``(n_records, n_cols)`` view.

    For trades the columns are ``[ts_us, price, size]``; for quotes ``[ts_us, bid_price, ask_price,
    bid_size, ask_size]``. ``minute`` is the bucketing minute (per-minute tier) or the tick's minute
    (firehose tier). ``kind`` is ``"trades"`` or ``"quotes"``.
    """

    symbol: str
    minute: dt.datetime
    kind: str
    rows: np.ndarray


def encode_bar(symbol: str, minute: dt.datetime | int, ohlcv: Sequence[float]) -> bytes:
    """Pack one OHLCV minute bar into an ``MDB1`` frame. ``ohlcv`` is (open, high, low, close, volume)."""
    open_, high, low, close, volume = (float(value) for value in ohlcv)
    symbol_bytes = symbol.encode("utf-8")
    header = struct.pack(
        _BAR_HEADER_FMT, BAR_MAGIC, _epoch_us(minute), open_, high, low, close, volume, len(symbol_bytes)
    )
    return header + symbol_bytes


def decode_bar(buf: bytes) -> BarRecord:
    magic, minute_us, open_, high, low, close, volume, symbol_len = struct.unpack_from(
        _BAR_HEADER_FMT, buf, 0
    )
    if magic != BAR_MAGIC:
        raise ValueError(f"bad magic {magic!r} (not an md: bar frame)")
    symbol = buf[_BAR_HEADER_SIZE : _BAR_HEADER_SIZE + symbol_len].decode("utf-8")
    return BarRecord(symbol, _us_to_datetime(minute_us), open_, high, low, close, volume)


def _tick_rows(records: Sequence[Mapping[str, float]], kind: str) -> np.ndarray:
    """Build the row-major float64 matrix for a trades/quotes batch from the reader's raw tick dicts."""
    if kind == "trades":
        rows = np.empty((len(records), TRADE_COLS), dtype="<f8")
        for index, record in enumerate(records):
            rows[index, 0] = record["ts_epoch"] * 1_000_000.0
            rows[index, 1] = record["p"]
            rows[index, 2] = record["s"]
        return rows
    rows = np.empty((len(records), QUOTE_COLS), dtype="<f8")
    for index, record in enumerate(records):
        rows[index, 0] = record["ts_epoch"] * 1_000_000.0
        rows[index, 1] = record["bp"]
        rows[index, 2] = record["ap"]
        rows[index, 3] = record["bs"]
        rows[index, 4] = record["as"]
    return rows


def encode_ticks(
    symbol: str, minute: dt.datetime | int, kind: str, records: Sequence[Mapping[str, float]]
) -> bytes:
    """Pack a batch of raw trade/quote dicts into an ``MDT1``/``MDQ1`` frame (one frame, N records)."""
    rows = _tick_rows(records, kind)
    return encode_tick_rows(symbol, minute, kind, rows)


def encode_tick_rows(symbol: str, minute: dt.datetime | int, kind: str, rows: np.ndarray) -> bytes:
    """Pack an already-built ``(n_records, n_cols)`` float64 matrix into a tick frame."""
    magic = TRADES_MAGIC if kind == "trades" else QUOTES_MAGIC
    n_cols = TRADE_COLS if kind == "trades" else QUOTE_COLS
    if rows.ndim != 2 or rows.shape[1] != n_cols:
        raise ValueError(f"{kind} rows must be (n, {n_cols}); got {rows.shape}")
    contiguous = np.ascontiguousarray(rows, dtype="<f8")
    symbol_bytes = symbol.encode("utf-8")
    header = struct.pack(
        _TICK_HEADER_FMT, magic, _epoch_us(minute), contiguous.shape[0], n_cols, len(symbol_bytes)
    )
    return header + symbol_bytes + contiguous.tobytes()


def decode_ticks(buf: bytes) -> TickBatch:
    """Unpack a tick frame into a ``TickBatch``; ``rows`` is a zero-copy view over ``buf``."""
    magic, minute_us, n_records, n_cols, symbol_len = struct.unpack_from(_TICK_HEADER_FMT, buf, 0)
    if magic == TRADES_MAGIC:
        kind = "trades"
    elif magic == QUOTES_MAGIC:
        kind = "quotes"
    else:
        raise ValueError(f"bad magic {magic!r} (not an md: tick frame)")
    symbol_start = _TICK_HEADER_SIZE
    payload_start = symbol_start + symbol_len
    symbol = buf[symbol_start:payload_start].decode("utf-8")
    flat = np.frombuffer(buf, dtype="<f8", count=n_records * n_cols, offset=payload_start)
    rows = flat.reshape(n_records, n_cols)
    return TickBatch(symbol, _us_to_datetime(minute_us), kind, rows)


class MarketDataPublisher:
    """Publishes raw ``md:`` frames to per-symbol, per-tier Redis streams. Used by the capture reader
    behind the opt-in env flags. Pipelines a minute's frames into one round-trip; tick-firehose XADDs
    use a bounded MAXLEN with approximate trim. This object holds the redis client; the caller owns
    fault-isolation (the capture hot path must catch ``redis.RedisError`` around publish calls).
    """

    def __init__(
        self,
        url: str = DEFAULT_REDIS_URL,
        prefix: str = MD_STREAM_PREFIX,
        minute_maxlen: int = DEFAULT_MINUTE_MAXLEN,
        tick_maxlen: int = DEFAULT_TICK_MAXLEN,
    ) -> None:
        self._redis = redis.Redis.from_url(url)
        self._prefix = prefix
        self._minute_maxlen = minute_maxlen
        self._tick_maxlen = tick_maxlen

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def publish_minute(
        self,
        bars: Sequence[Mapping[str, object]],
        trades_by_symbol: Mapping[str, Sequence[Mapping[str, float]]],
        quotes_by_symbol: Mapping[str, Sequence[Mapping[str, float]]],
        minute: dt.datetime | int,
    ) -> int:
        """Pipeline one minute's raw bars + (optional) per-symbol trades/quotes. ``bars`` are the
        reader's bar dicts ({"S","o","h","l","c","v",...}). Returns the number of frames published."""
        pipe = self._redis.pipeline(transaction=False)
        count = 0
        for bar in bars:
            symbol = str(bar["S"])
            ohlcv = (
                float(bar["o"]),  # type: ignore[arg-type]
                float(bar["h"]),  # type: ignore[arg-type]
                float(bar["l"]),  # type: ignore[arg-type]
                float(bar["c"]),  # type: ignore[arg-type]
                float(bar["v"]),  # type: ignore[arg-type]
            )
            pipe.xadd(
                md_stream_key(BAR_STREAM, symbol, self._prefix),
                {FRAME_FIELD: encode_bar(symbol, minute, ohlcv)},
                maxlen=self._minute_maxlen,
                approximate=True,
            )
            count += 1
        for symbol, trades in trades_by_symbol.items():
            if not trades:
                continue
            pipe.xadd(
                md_stream_key(TRADES_STREAM, symbol, self._prefix),
                {FRAME_FIELD: encode_ticks(symbol, minute, "trades", trades)},
                maxlen=self._minute_maxlen,
                approximate=True,
            )
            count += 1
        for symbol, quotes in quotes_by_symbol.items():
            if not quotes:
                continue
            pipe.xadd(
                md_stream_key(QUOTES_STREAM, symbol, self._prefix),
                {FRAME_FIELD: encode_ticks(symbol, minute, "quotes", quotes)},
                maxlen=self._minute_maxlen,
                approximate=True,
            )
            count += 1
        pipe.execute()
        return count

    def publish_tick(
        self, symbol: str, minute: dt.datetime | int, kind: str, record: Mapping[str, float]
    ) -> None:
        """Firehose: XADD one trade/quote to ``md:tick_{kind}:<symbol>`` (bounded, approximate trim)."""
        stream = TICK_TRADES_STREAM if kind == "trades" else TICK_QUOTES_STREAM
        self._redis.xadd(
            md_stream_key(stream, symbol, self._prefix),
            {FRAME_FIELD: encode_ticks(symbol, minute, kind, [record])},
            maxlen=self._tick_maxlen,
            approximate=True,
        )

    def close(self) -> None:
        self._redis.close()


def _decode_frame(stream: str, frame: bytes) -> BarRecord | TickBatch:
    if stream == BAR_STREAM:
        return decode_bar(frame)
    return decode_ticks(frame)


class MarketDataConsumer:
    """Reads decoded raw ``md:`` records for a declared set of symbols + tiers — the same ``poll()``
    ergonomics as ``BusConsumer`` so a strategy can run BOTH side by side. A container/researcher asks
    only for what it wants, e.g. ``MarketDataConsumer(symbols=["AAPL"], streams=["bar", "tick_trades"])``.

    ``poll()`` returns a list of ``(stream, record)`` where ``record`` is a ``BarRecord`` for the bar
    tier and a ``TickBatch`` for any trade/quote tier (per-minute or firehose).
    """

    def __init__(
        self,
        symbols: Iterable[str],
        streams: Iterable[str] = (BAR_STREAM,),
        url: str = DEFAULT_REDIS_URL,
        prefix: str = MD_STREAM_PREFIX,
        start: str = "$",
    ) -> None:
        stream_list = list(streams)
        for stream in stream_list:
            if stream not in ALL_STREAMS:
                raise ValueError(f"unknown md stream '{stream}' (known: {ALL_STREAMS})")
        self._redis = redis.Redis.from_url(url)
        self._prefix = prefix
        # key -> (stream, last_id); one Redis key per (tier, symbol) the caller subscribed.
        self._streams: dict[str, str] = {}
        self._last_id: dict[str, str] = {}
        for stream in stream_list:
            for symbol in symbols:
                key = md_stream_key(stream, symbol, prefix)
                self._streams[key] = stream
                self._last_id[key] = start

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def keys(self) -> list[str]:
        return list(self._last_id)

    def poll(self, block_ms: int = 1000, count: int = 200) -> list[tuple[str, BarRecord | TickBatch]]:
        """Block up to ``block_ms`` for new frames across the subscribed (tier, symbol) streams."""
        response = self._redis.xread(self._last_id, count=count, block=block_ms)
        records: list[tuple[str, BarRecord | TickBatch]] = []
        for raw_key, entries in response:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            stream = self._streams[key]
            for entry_id, fields in entries:
                entry_id_str = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                self._last_id[key] = entry_id_str
                records.append((stream, _decode_frame(stream, fields[FRAME_FIELD])))
        return records

    def close(self) -> None:
        self._redis.close()
