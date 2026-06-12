"""Symbol-hash sharding + the reader<->worker message substrate.

The substrate is a swappable seam: today it's multiprocessing queues (shared-memory
pipes — lowest latency for an in-host reader->worker fan-out, no extra infra). The
reader routes each tick/bar to `shard_for(symbol, n_shards)`; a worker owns one
shard's symbols, so its per-symbol `tick_state` is touched by exactly one process —
no cross-worker coordination, the tick rule stays correct per symbol.

Messages are plain tuples (cheap to pickle across the queue). `kind` selects the
handler in the worker; payloads carry only primitives so a worker never needs the
Alpaca SDK types.
"""
import hashlib
from dataclasses import dataclass
from multiprocessing import Queue

# Message kinds on the reader->worker queue.
KIND_TRADE = "t"
KIND_QUOTE = "q"
KIND_BAR = "b"  # minute-close signal: flush this symbol's buffered minute(s)


def shard_for(symbol: str, n_shards: int) -> int:
    """Stable shard index for a symbol. md5 (not Python hash()) so the partition is
    identical across processes and restarts — PYTHONHASHSEED would make hash()
    non-deterministic, silently splitting a symbol's state across workers."""
    digest = hashlib.md5(symbol.encode("utf-8")).digest()
    return digest[0] % n_shards


@dataclass
class TradeMsg:
    symbol: str
    ts_epoch: float
    price: float
    size: float
    exchange: str | None
    conditions: str | None
    tape: str | None


@dataclass
class QuoteMsg:
    symbol: str
    ts_epoch: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass
class BarMsg:
    symbol: str
    ts_epoch: float


def make_queues(n_shards: int, maxsize: int) -> list["Queue"]:
    """One bounded queue per shard. Bounded so a wedged worker surfaces as queue
    backpressure (a measurable coverage signal) instead of unbounded memory growth."""
    return [Queue(maxsize=maxsize) for _ in range(n_shards)]
