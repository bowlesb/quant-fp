"""`StrategyState` — the first-class, typed strategy state (REQ-S1) that is the SAME model in backtest
and live (docs/STRATEGY_EXECUTION_ABSTRACTION.md §4).

The prior repo's state was an untyped KV blob split across Postgres+Redis with the dynamic part in the
volatile store (the L3 wall). Here state is ONE typed model: positions, pending orders, realized P&L,
and a typed `counters` map for the strategy-specific carry (streak/persistence/trailing-stop) — all
persisted together to ONE durable store, and the position is RECOMPUTABLE from an append-only fill
ledger (so corruption is detectable). Backtest uses `MemoryStateStore`; live uses a `PgStateStore`
(not built this phase — the interface is what makes them swappable).

`apply_fill` is the ONE transition function both backtest and live call, so positions/P&L evolve
identically regardless of where the fill came from (simulated or real broker).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from quantlib.strategy_core.execution import Fill, OrderIntent


@dataclass
class Position:
    symbol: str
    qty: float  # signed: long +, short −
    avg_entry_price: float


@dataclass
class PendingOrder:
    intent: OrderIntent
    filled_qty: float = 0.0  # cumulative (partials)
    avg_fill_price: float = 0.0


@dataclass
class StrategyState:
    """One typed model, identical backtest + live (REQ-S1)."""

    strategy_id: str
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, PendingOrder] = field(default_factory=dict)  # keyed by client_order_id
    realized_pnl: float = 0.0
    counters: dict[str, float] = field(default_factory=dict)  # typed carry: streak/persistence/stops
    fills: list[Fill] = field(default_factory=list)  # append-only ledger (positions recomputable)

    def record_pending(self, intent: OrderIntent) -> None:
        self.pending[intent.client_order_id] = PendingOrder(intent=intent)

    def apply_fill(self, fill: Fill) -> None:
        """The ONE state transition both paths call. Updates the position (weighted avg on adds,
        realizes P&L on reduces/closes), the pending order's cumulative fill, and the append-only
        ledger. Deterministic; no I/O."""
        self.fills.append(fill)
        pending = self.pending.get(fill.client_order_id)
        if pending is not None:
            pending.filled_qty = fill.filled_qty
            pending.avg_fill_price = fill.avg_price
        signed = fill.filled_qty if fill.side == "buy" else -fill.filled_qty
        existing = self.positions.get(fill.symbol)
        if existing is None:
            if signed != 0.0:
                self.positions[fill.symbol] = Position(fill.symbol, signed, fill.avg_price)
            return
        # same direction -> weighted-average in; opposite -> realize P&L on the closed amount
        if (existing.qty >= 0) == (signed >= 0):
            total = existing.qty + signed
            if total == 0.0:
                del self.positions[fill.symbol]
            else:
                existing.avg_entry_price = (
                    existing.avg_entry_price * existing.qty + fill.avg_price * signed
                ) / total
                existing.qty = total
        else:
            closed = min(abs(existing.qty), abs(signed))
            direction = 1.0 if existing.qty > 0 else -1.0
            self.realized_pnl += direction * (fill.avg_price - existing.avg_entry_price) * closed
            existing.qty += signed
            if abs(existing.qty) < 1e-12:
                del self.positions[fill.symbol]

    def positions_from_ledger(self) -> dict[str, float]:
        """Recompute net qty per symbol from the append-only fill ledger — must equal
        {sym: pos.qty}. An independent check that catches state corruption (REQ-S2)."""
        net: dict[str, float] = {}
        for fill in self.fills:
            signed = fill.filled_qty if fill.side == "buy" else -fill.filled_qty
            net[fill.symbol] = net.get(fill.symbol, 0.0) + signed
        return {sym: qty for sym, qty in net.items() if abs(qty) > 1e-12}


class StateStore(Protocol):
    """Swappable persistence behind one shape: MemoryStateStore (backtest) / PgStateStore (live).

    `append_fill` is the append-only-ledger primitive (REQ-S2): a live store persists each fill durably
    (the ledger is the source from which positions are recomputable), so a crash mid-cycle never loses a
    fill. `save` snapshots the derived state. `load` rebuilds state from the durable store on restart."""

    def load(self, strategy_id: str) -> StrategyState: ...

    def save(self, state: StrategyState) -> None: ...

    def append_fill(self, strategy_id: str, fill: Fill) -> None: ...


class MemoryStateStore:
    """In-process state (backtest). The live `PgStateStore` (durable, atomic, append-only ledger) is
    the same interface."""

    def __init__(self) -> None:
        self._states: dict[str, StrategyState] = {}

    def load(self, strategy_id: str) -> StrategyState:
        return self._states.setdefault(strategy_id, StrategyState(strategy_id=strategy_id))

    def save(self, state: StrategyState) -> None:
        self._states[state.strategy_id] = state

    def append_fill(self, strategy_id: str, fill: Fill) -> None:
        """In-memory: the ledger already lives on `state.fills` (apply_fill appends it). A no-op snapshot
        keeps the protocol uniform so the live durable store is a drop-in."""
        self._states.setdefault(strategy_id, StrategyState(strategy_id=strategy_id))
