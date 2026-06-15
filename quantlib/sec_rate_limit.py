"""Token-bucket rate limiter for SEC EDGAR fair-access (~4 rps default).

Ported from the prior project's token_bucket.py, reduced to a SYNCHRONOUS, dependency-free bucket
(the original was async + Prometheus-coupled; this service is sync psycopg/httpx). Shared in quantlib
so the live stream and the backfill path use ONE limiter discipline against the same SEC endpoints.

SEC asks clients to stay under 10 req/s and to send a real User-Agent; we default to 4 req/s for
margin. The bucket fills at `rate` tokens/sec up to `capacity` (default 2x rate, for small bursts);
each request consumes one token, sleeping if the bucket is empty.

    limiter = TokenBucket(rate=4.0)
    with limiter.acquire():
        response = client.get(url)
"""

import threading
import time
from types import TracebackType


class TokenBucket:
    """Thread-safe synchronous token bucket."""

    def __init__(self, rate: float = 4.0, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate * 2
        self.tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last_refill = now

    def acquire(self, tokens: float = 1.0) -> "TokenBucketContext":
        return TokenBucketContext(self, tokens)

    def _acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available, consume them, return seconds waited."""
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            wait_seconds = (tokens - self.tokens) / self.rate
        time.sleep(wait_seconds)
        with self._lock:
            self._refill()
            self.tokens -= tokens
        return wait_seconds


class TokenBucketContext:
    """Context manager returned by TokenBucket.acquire()."""

    def __init__(self, bucket: TokenBucket, tokens: float) -> None:
        self._bucket = bucket
        self._tokens = tokens
        self.waited_seconds = 0.0

    def __enter__(self) -> "TokenBucketContext":
        self.waited_seconds = self._bucket._acquire(self._tokens)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None
