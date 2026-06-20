"""The two `DataFeed`s — `PanelFeed` (replays the historical panel) and `BusFeed` (the live bus).

Same event shape out (`FeedEvent(cross_section, ts)`), different source — so the SAME `Runner` +
`strategy.decide` run over either. `PanelFeed` yields one `PanelCrossSection` per timestamp from the
battery's column-major `Panel`; `BusFeed` yields a `BusCrossSection` from the latest-by-symbol bus
vectors each cycle. See docs/STRATEGY_BATTERY_PORTABILITY.md §2.5.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import numpy as np

from quantlib.strategy_core.adapters import BusCrossSection, PanelCrossSection
from quantlib.strategy_core.execution import FeedEvent

# execution columns the BacktestExecutor reads by name off each panel cross-section.
_EXEC_COLUMNS = ("entry_close", "half_spread_bps")


class PanelFeed:
    """Replays a battery `Panel` as one `PanelCrossSection` event per distinct timestamp, in time
    order. Each event exposes the panel's features PLUS the execution columns (entry_close,
    half_spread_bps) by name, so the BacktestExecutor reads fill price + cost through the same by-name
    interface the live feed uses."""

    def __init__(self, panel: object) -> None:
        # duck-typed on the battery Panel (symbol_code, symbol_names, minute_epoch, feature_matrix,
        # feature_names, entry_close, half_spread_bps) to avoid importing battery into the pure core.
        self._panel = panel

    def events(self) -> Iterator[FeedEvent]:
        panel = self._panel
        minute_epoch = panel.minute_epoch  # type: ignore[attr-defined]
        feature_names = list(panel.feature_names)  # type: ignore[attr-defined]
        feature_columns = {name: i for i, name in enumerate(feature_names)}
        unique_minutes = np.unique(minute_epoch)
        symbol_names = panel.symbol_names  # type: ignore[attr-defined]
        for ns in unique_minutes:
            rows = np.where(minute_epoch == ns)[0]
            symbols = [symbol_names[panel.symbol_code[r]] for r in rows]  # type: ignore[attr-defined]
            matrix = panel.feature_matrix[rows]  # type: ignore[attr-defined]
            extra = {
                "entry_close": panel.entry_close[rows],  # type: ignore[attr-defined]
                "half_spread_bps": panel.half_spread_bps[rows],  # type: ignore[attr-defined]
            }
            ts = dt.datetime.fromtimestamp(int(ns) / 1e9, tz=dt.timezone.utc)
            yield FeedEvent(
                cross_section=PanelCrossSection(symbols, ts, matrix, feature_columns, extra),
                ts=ts,
            )


class BusFeed:
    """The live feed: one `BusCrossSection` event per cycle from the latest-by-symbol bus vectors. The
    container's existing poll loop (e.g. `ReversionStrategy.consume`) maintains `latest_by_symbol`; this
    wraps that snapshot so the SAME `decide` runs on it. `poll()` returns the snapshot for one cycle."""

    def __init__(self, consumer: object, *, block_ms: int = 1000, count: int = 200) -> None:
        # duck-typed on quantlib.bus.consumer.BusConsumer (.poll) — not imported, so the pure core
        # never drags redis into the battery.
        self._consumer = consumer
        self._block_ms = block_ms
        self._count = count
        self._latest: dict[str, object] = {}

    def poll_once(self) -> FeedEvent | None:
        vectors = self._consumer.poll(block_ms=self._block_ms, count=self._count)  # type: ignore[attr-defined]
        if not vectors:
            return None
        for vector in vectors:
            self._latest[vector.symbol] = vector
        cross_section = BusCrossSection(self._latest)
        return FeedEvent(cross_section=cross_section, ts=cross_section.minute)

    def events(self) -> Iterator[FeedEvent]:
        """Unbounded live stream: each non-empty poll is one decision event."""
        while True:
            event = self.poll_once()
            if event is not None:
                yield event
