"""Best-effort publish of live feature vectors to the feature-vector bus, OFF the compute critical path.

The capture loop computes per-GROUP frames (one row per symbol). This hook assembles them into one
packed per-symbol vector and XADDs it to Redis — but never on the hot path: ``submit()`` only drops the
already-computed frames onto a bounded queue and returns immediately; a daemon thread does the assemble
+ publish. If the queue is full (slow/absent broker) frames are DROPPED, and Redis errors are swallowed
with a warning — a bus problem must never slow or crash live feature collection. Gated by ``FP_BUS=1``
and real mode only (sim/mock minutes are not published).
"""
from __future__ import annotations

import logging
import os
import queue
import threading

import numpy as np
import polars as pl
import redis

from quantlib.bus.publisher import BusPublisher

logger = logging.getLogger("bus_hook")

BUS_QUEUE_MAX = 256  # bounded backlog; if the publisher can't keep up, drop minutes rather than block
_NON_FEATURE_COLUMNS = frozenset({"symbol", "minute"})


def bus_publish_enabled() -> bool:
    """``FP_BUS=1`` turns on live publishing (default OFF — the live path is unchanged until set)."""
    return os.environ.get("FP_BUS", "0") == "1"


class BusHook:
    """Background assembler+publisher: per-group frames -> per-symbol packed vectors -> Redis streams."""

    def __init__(self, publisher: BusPublisher | None = None) -> None:
        self._publisher = publisher or BusPublisher()  # lazy redis client; no connect until first XADD
        self._queue: queue.Queue[tuple[object, list[tuple[str, pl.DataFrame]]]] = queue.Queue(
            maxsize=BUS_QUEUE_MAX
        )
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name="bus-hook", daemon=True)
        self._thread.start()

    def submit(self, minute: object, outputs: list[tuple[str, pl.DataFrame]]) -> None:
        """Hand this minute's per-group frames to the publisher thread. Never blocks; drops on backlog."""
        try:
            self._queue.put_nowait((minute, outputs))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 50 == 1:
                logger.warning("bus backlog full — dropped %d minute(s) of publishes", self._dropped)

    def _run(self) -> None:
        while True:
            minute, outputs = self._queue.get()
            try:
                self._assemble_and_publish(minute, outputs)
            except redis.exceptions.RedisError as error:
                logger.warning("bus publish failed (minute=%s): %s", minute, error)

    def _assemble_and_publish(self, minute: object, outputs: list[tuple[str, pl.DataFrame]]) -> None:
        schema = self._publisher.schema
        arrays: dict[str, np.ndarray] = {}
        for _group_name, frame in outputs:
            if frame.height == 0:
                continue
            feature_cols = [
                col for col in frame.columns if col not in _NON_FEATURE_COLUMNS and schema.has(col)
            ]
            if not feature_cols:
                continue
            offsets = np.array([schema.offset(col) for col in feature_cols])
            symbols = frame["symbol"].to_list()
            values = frame.select(feature_cols).to_numpy()
            for index, symbol in enumerate(symbols):
                if symbol in arrays:
                    vector = arrays[symbol]
                else:
                    vector = np.full(schema.n_features, np.nan, dtype="<f8")
                    arrays[symbol] = vector
                vector[offsets] = values[index]
        if arrays:
            self._publisher.publish_many((symbol, minute, vector) for symbol, vector in arrays.items())
