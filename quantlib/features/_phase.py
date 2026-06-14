"""Tiny opt-in phase timer for decomposing where per-minute compute time goes (raw kernel vs the DataFrame
shuffling around it). Off unless FP_PHASE_TIMING is set, so zero cost in production."""
from __future__ import annotations

import os
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

ON = bool(os.environ.get("FP_PHASE_TIMING"))
TIMES: dict[str, float] = defaultdict(float)
COUNTS: dict[str, int] = defaultdict(int)


@contextmanager
def phase(name: str) -> Iterator[None]:
    if not ON:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        TIMES[name] += (time.perf_counter() - start) * 1000.0
        COUNTS[name] += 1


def reset() -> None:
    TIMES.clear()
    COUNTS.clear()


def report() -> list[tuple[str, float, int]]:
    return sorted(((name, ms, COUNTS[name]) for name, ms in TIMES.items()), key=lambda row: -row[1])
