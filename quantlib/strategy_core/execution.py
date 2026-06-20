"""The HOT-SWAP execution seams (the event-driven-backtester == live-trader pattern).

The strategy's `decide` is written ONCE; the `Executor`, `DataFeed`, and `Clock` are swapped under it:

    battery_backtest = Runner(strategy, PanelFeed(panel),  BacktestExecutor(cost_model), SimClock())
    live_container   = Runner(strategy, BusFeed(consumer), PaperExecutor(broker),        RealClock())

SAME `Runner`, SAME `strategy.decide`, swapped components. The live container is a thin harness around
the same shared decision logic — no duplicated code. See docs/STRATEGY_BATTERY_PORTABILITY.md §2.5.

PERFORMANCE (§2.6): the live `PaperExecutor` runs `decide` per-event; the `BacktestExecutor` over the
panel must NOT loop per-event in slow Python (Ben's <30-60s budget). For cross-sectional archetypes the
backtest path is VECTORIZED — `BacktestExecutor.run_vectorized` applies the strategy's columnar
`score` across ALL timestamps at once and books via the per-timestamp cost model — while the SAME
`decide` serves the live per-minute path. Inherently-sequential archetypes (triple-barrier first-touch,
streak) cannot be one columnar pass and are the Phase-1 Rust kernel; `Executor` accommodates both.

This module deliberately imports NOTHING from the bus/broker — the live `PaperExecutor`/`BusFeed` are
duck-typed on the broker/consumer handles so the pure core never drags redis/alpaca into the battery.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from quantlib.strategy_core import CrossSection, TargetPosition


@dataclass(frozen=True)
class OrderIntent:
    """What the strategy WANTS to transact this step — broker-agnostic. The Executor turns it into a
    simulated fill (backtest) or a real broker order (live). Same intent in; only the fill differs."""

    symbol: str
    side: str  # "buy" | "sell"
    target_weight: float  # desired dollar-neutral book weight (basket) ...
    notional: float | None = None  # ... or an absolute notional (single-name)
    reason: str = ""

    @classmethod
    def from_target(cls, target: TargetPosition) -> "OrderIntent":
        return cls(
            symbol=target.symbol,
            side="buy" if target.target_weight >= 0 else "sell",
            target_weight=target.target_weight,
            reason=f"score={target.score:.5f}",
        )


@dataclass(frozen=True)
class Fill:
    """A realized fill. In backtest the price/cost come from the panel's tradeable entry + half-spread;
    live they come from the broker's fill report."""

    symbol: str
    side: str
    weight: float
    fill_price: float
    cost_bps: float


@dataclass
class BookState:
    """The current book the strategy reads (read-only to `decide`) — symbol -> weight, plus the last
    step's targets so the executor can diff for turnover. Carried explicitly (no hidden cross-step
    state) so the backtest path stays vectorizable."""

    weights: dict[str, float] = field(default_factory=dict)

    def weight_of(self, symbol: str) -> float:
        return self.weights.get(symbol, 0.0)


class Clock(Protocol):
    def now(self) -> dt.datetime:
        ...


class SimClock:
    """Panel-driven time: advances to each event's timestamp (no wall-clock — safe on a feature-time
    path, reproducible)."""

    def __init__(self) -> None:
        self._now = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

    def set(self, ts: dt.datetime) -> None:
        self._now = ts

    def now(self) -> dt.datetime:
        return self._now


class RealClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class FeedEvent:
    cross_section: CrossSection
    ts: dt.datetime


class DataFeed(Protocol):
    """Replays decision events. Same event shape out (a CrossSection + ts), different source."""

    def events(self) -> Iterator[FeedEvent]:
        ...


class Strategy(Protocol):
    """`decide` is PURE: given the as-of-t cross-section + the current book, return the orders to
    transact. No bus, no broker, no wall-clock. Called once per step by the Runner — identical live +
    backtest. MUST be columnar-friendly so the BacktestExecutor can batch it (see module docstring)."""

    def decide(self, cross_section: CrossSection, state: BookState) -> list[OrderIntent]:
        ...


class Executor(Protocol):
    """THE pretend-trade vs actual-trade swap. `execute` turns intents into fills (simulated or real)
    and updates the book."""

    def execute(self, intents: list[OrderIntent], cross_section: CrossSection, clock: Clock) -> list[Fill]:
        ...

    def book(self) -> BookState:
        ...


class TargetBookCore(Protocol):
    """A declarative decision core that returns the desired target BOOK (the `CrossSectionalLS` shape).
    `TargetBookStrategy` adapts it to the per-step `Strategy` (`decide -> [OrderIntent]`) so the same
    core drives the per-event Runner. The target book is also what the vectorized backtest path books —
    one representation, both paths."""

    def decide(self, cross_section: CrossSection) -> list[TargetPosition]:
        ...


class TargetBookStrategy:
    """Adapts a `TargetBookCore` (returns target weights) into the execution `Strategy` (`decide(cs,
    state) -> [OrderIntent]`). The intents are the orders to REACH the target book from the current
    book — the same diff-to-target a live container does (e.g. reversion's select+place). Pure."""

    def __init__(self, core: TargetBookCore) -> None:
        self._core = core

    def decide(self, cross_section: CrossSection, state: BookState) -> list[OrderIntent]:
        targets = self._core.decide(cross_section)
        target_symbols = {t.symbol for t in targets}
        intents = [OrderIntent.from_target(t) for t in targets]
        # flatten anything held but no longer targeted (exit to zero weight)
        for symbol, weight in state.weights.items():
            if symbol not in target_symbols and weight != 0.0:
                intents.append(
                    OrderIntent(
                        symbol=symbol,
                        side="sell" if weight > 0 else "buy",
                        target_weight=0.0,
                        reason="exit_untargeted",
                    )
                )
        return intents


class Runner:
    """Ties {strategy, feed, executor, clock} — the ONE loop both the battery backtest and the live
    container share. The per-event path: faithful to live, used for the parity proof + small backtests.
    For the FAST battery, cross-sectional archetypes use `BacktestExecutor.run_vectorized` instead (the
    same `decide` logic applied columnar) — see §2.6 — but the per-event `run` here is the reference
    that the vectorized path must match."""

    def __init__(self, strategy: Strategy, feed: DataFeed, executor: Executor, clock: Clock) -> None:
        self._strategy = strategy
        self._feed = feed
        self._executor = executor
        self._clock = clock

    def run(self) -> None:
        for event in self._feed.events():
            if isinstance(self._clock, SimClock):
                self._clock.set(event.ts)
            intents = self._strategy.decide(event.cross_section, self._executor.book())
            self._executor.execute(intents, event.cross_section, self._clock)
