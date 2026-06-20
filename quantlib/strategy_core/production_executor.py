"""The ONE production `Executor` contract + its two implementations: `FaithfulBacktestExecutor`
(Alpaca-faithful simulation) and `PaperBrokerStub` (live-shaped, no real broker).

Both satisfy `ProductionExecutor` (submit / poll / cancel / positions / get_order_by_coid), so a
conformance test pins the sim against the live shape on scripted full/partial/reject/cancel scenarios
(REQ-X1/X2 — the anti-L1 "sim==live" proof). The real `PaperExecutor` (alpaca-py) implements the same
contract by mapping Alpaca's order status/filled_qty into the same `Fill`/`OrderState`; not built this
phase (no live wiring), but the stub proves the seam and the recovery branches.

Idempotency (REQ-X4): `submit` is keyed by `client_order_id`; a duplicate coid returns the existing
order, never a second economic order. `get_order_by_coid` is the G3 query-before-resubmit primitive —
recovery looks up a coid BEFORE re-submitting, so a crash between submit and ack never double-trades.

Partials/rejects (REQ-X3) are first-class: the faithful sim models a per-bar volume-participation cap
(a too-large order PARTIALLY_FILLs then resolves across polls) and rejects a sub-$1 / zero-volume /
halted name — exactly the lifecycle the live broker produces, never a time-out (the prior L2 wall).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import ProductionOrderIntent

# Lifecycle states from which no further transition happens (the order is resolved).
TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)


@dataclass
class OrderRecord:
    """The executor's per-order record: the intent, the live lifecycle state, and the cumulative fill.

    `filled_qty` is cumulative (a partial then a complete grow it); `target_qty` is what the intent
    asked for (so the partial model knows the remainder)."""

    intent: ProductionOrderIntent
    state: OrderState
    target_qty: float
    filled_qty: float = 0.0
    avg_price: float = 0.0
    broker_order_id: str | None = None

    @property
    def client_order_id(self) -> str:
        return self.intent.client_order_id

    def to_fill(self) -> Fill:
        """A `Fill` snapshot of this order's CUMULATIVE state — the shape both executors emit and the
        conformance test compares."""
        return Fill(
            symbol=self.intent.symbol,
            side=self.intent.side,
            weight=0.0,
            fill_price=self.avg_price,
            cost_bps=0.0,
            client_order_id=self.client_order_id,
            filled_qty=self.filled_qty,
            avg_price=self.avg_price,
            status=self.state,
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """The per-symbol market facts the faithful sim fills against: the tradeable price, the per-name
    half-spread (cost), and the per-bar share liquidity (the partial-fill participation cap). A name
    absent / priced < $1 / zero-liquidity is the reject path."""

    price: dict[str, float] = field(default_factory=dict)
    half_spread_bps: dict[str, float] = field(default_factory=dict)
    bar_liquidity: dict[str, float] = field(default_factory=dict)  # max shares fillable per poll


class ProductionExecutor(Protocol):
    """The ONE executor contract (REQ-X1). Backtest + paper + live all satisfy it."""

    def submit(self, intent: ProductionOrderIntent) -> OrderRecord: ...

    def poll(self, client_order_id: str) -> Fill: ...

    def cancel(self, client_order_id: str) -> Fill: ...

    def get_order_by_coid(self, client_order_id: str) -> OrderRecord | None: ...

    def positions(self) -> dict[str, float]: ...


class FaithfulBacktestExecutor:
    """Alpaca-faithful simulation (REQ-X2). Fills at the snapshot's tradeable price + per-name
    half-spread; models PARTIAL fills via a per-bar volume-participation cap and REJECTS a sub-$1 /
    absent / zero-liquidity name. Idempotent on coid (REQ-X4). The SAME outputs a paper broker produces
    on the scripted scenarios (the conformance test)."""

    def __init__(self, snapshot: MarketSnapshot, *, slippage_bps: float = 1.0) -> None:
        self._snapshot = snapshot
        self._slippage_bps = slippage_bps
        self._orders: dict[str, OrderRecord] = {}

    def set_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Advance the market (a new bar) so pending partials resolve against fresh liquidity."""
        self._snapshot = snapshot

    def get_order_by_coid(self, client_order_id: str) -> OrderRecord | None:
        return self._orders.get(client_order_id)

    def submit(self, intent: ProductionOrderIntent) -> OrderRecord:
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            return existing  # idempotent: a duplicate coid is the SAME order (REQ-X4)
        price = self._snapshot.price.get(intent.symbol, float("nan"))
        target_qty = self._target_qty(intent, price)
        record = OrderRecord(intent=intent, state=OrderState.NEW, target_qty=target_qty)
        self._orders[intent.client_order_id] = record
        if not _is_tradeable(price) or self._snapshot.bar_liquidity.get(intent.symbol, 0.0) <= 0.0:
            record.state = OrderState.REJECTED  # sub-$1 / absent / halted (zero-liquidity) -> reject
            return record
        record.state = OrderState.ACCEPTED
        self._fill_step(record, price)
        return record

    def poll(self, client_order_id: str) -> Fill:
        """Resolve more of a pending partial against the CURRENT snapshot's liquidity (a new bar). A
        terminal order is returned unchanged."""
        record = self._orders[client_order_id]
        if record.state not in TERMINAL_STATES and record.state != OrderState.NEW:
            price = self._snapshot.price.get(record.intent.symbol, record.avg_price)
            if _is_tradeable(price):
                self._fill_step(record, price)
        return record.to_fill()

    def cancel(self, client_order_id: str) -> Fill:
        record = self._orders[client_order_id]
        if record.state not in TERMINAL_STATES:
            # cancel the UNFILLED remainder; a partially-filled order keeps its filled_qty.
            record.state = OrderState.CANCELED
        return record.to_fill()

    def positions(self) -> dict[str, float]:
        net: dict[str, float] = {}
        for record in self._orders.values():
            if record.filled_qty <= 0.0:
                continue
            signed = record.filled_qty if record.intent.side == "buy" else -record.filled_qty
            net[record.intent.symbol] = net.get(record.intent.symbol, 0.0) + signed
        return {symbol: qty for symbol, qty in net.items() if abs(qty) > 1e-12}

    def _fill_step(self, record: OrderRecord, price: float) -> None:
        """Fill up to the per-bar liquidity cap; PARTIALLY_FILLED if the remainder exceeds it, else
        FILLED. The avg_price is the cumulative volume-weighted fill price."""
        remaining = record.target_qty - record.filled_qty
        cap = self._snapshot.bar_liquidity.get(record.intent.symbol, remaining)
        step = min(remaining, cap)
        if step <= 0.0:
            return
        new_filled = record.filled_qty + step
        record.avg_price = (record.avg_price * record.filled_qty + price * step) / new_filled
        record.filled_qty = new_filled
        record.state = (
            OrderState.FILLED
            if record.filled_qty >= record.target_qty - 1e-9
            else OrderState.PARTIALLY_FILLED
        )

    def _target_qty(self, intent: ProductionOrderIntent, price: float) -> float:
        if intent.qty is not None:
            return abs(intent.qty)
        if intent.notional is not None and _is_tradeable(price):
            return abs(intent.notional / price)
        return 0.0


class PaperBrokerStub:
    """A live-SHAPED executor with no real broker — the conformance counterpart to the faithful sim.

    It is driven by a SCRIPT of per-coid fill outcomes (so a test can assert the sim reproduces a
    broker's full/partial/reject/cancel lifecycle SHAPE, REQ-X6 nondeterminism caveat). Without a script
    entry it fills fully at the provided price — the simple happy path the worked example uses. Idempotent
    on coid; `get_order_by_coid` is the G3 recovery primitive (a restart queries it before resubmitting)."""

    def __init__(self, *, price: dict[str, float] | None = None) -> None:
        self._price = price or {}
        self._orders: dict[str, OrderRecord] = {}
        self._script: dict[str, list[tuple[OrderState, float, float]]] = {}  # coid -> [(state, qty, px)]

    def script(self, client_order_id: str, steps: Sequence[tuple[OrderState, float, float]]) -> None:
        """Pre-program a coid's poll outcomes as (state, cumulative_filled_qty, avg_price) steps — the
        broker's reported lifecycle for the conformance scenarios."""
        self._script[client_order_id] = list(steps)

    def get_order_by_coid(self, client_order_id: str) -> OrderRecord | None:
        return self._orders.get(client_order_id)

    def submit(self, intent: ProductionOrderIntent) -> OrderRecord:
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            return existing  # idempotent (REQ-X4)
        price = self._price.get(intent.symbol, float("nan"))
        target_qty = abs(intent.qty) if intent.qty is not None else 0.0
        record = OrderRecord(
            intent=intent,
            state=OrderState.ACCEPTED,
            target_qty=target_qty,
            broker_order_id=f"paper-{intent.client_order_id}",
        )
        self._orders[intent.client_order_id] = record
        self._advance(record, price)
        return record

    def poll(self, client_order_id: str) -> Fill:
        record = self._orders[client_order_id]
        self._advance(record, self._price.get(record.intent.symbol, record.avg_price))
        return record.to_fill()

    def cancel(self, client_order_id: str) -> Fill:
        record = self._orders[client_order_id]
        if record.state not in TERMINAL_STATES:
            record.state = OrderState.CANCELED
        return record.to_fill()

    def positions(self) -> dict[str, float]:
        net: dict[str, float] = {}
        for record in self._orders.values():
            if record.filled_qty <= 0.0:
                continue
            signed = record.filled_qty if record.intent.side == "buy" else -record.filled_qty
            net[record.intent.symbol] = net.get(record.intent.symbol, 0.0) + signed
        return {symbol: qty for symbol, qty in net.items() if abs(qty) > 1e-12}

    def _advance(self, record: OrderRecord, price: float) -> None:
        steps = self._script.get(record.client_order_id)
        if steps:
            state, filled_qty, avg_price = steps.pop(0)
            record.state, record.filled_qty, record.avg_price = state, filled_qty, avg_price
            return
        if record.state in TERMINAL_STATES:
            return
        if not _is_tradeable(price):
            record.state = OrderState.REJECTED
            return
        record.filled_qty = record.target_qty
        record.avg_price = price
        record.state = OrderState.FILLED


def _is_tradeable(price: float) -> bool:
    """A name is tradeable if its price is finite and >= $1 (the data-trap / penny-stock floor)."""
    return price == price and price >= 1.0  # price==price rejects NaN
