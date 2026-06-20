"""`PgStateStore` (durable, append-only-ledger source of truth) + the restart-recovery driver.

REQ-S2: live state persists to ONE durable store with an append-only fill ledger from which positions
are RECOMPUTABLE — no dynamic state in a volatile side-store (the prior repo's L3 wall). `PgStateStore`
is that store; it persists each fill durably on `append_fill` and rebuilds the full `StrategyState` from
the ledger on `load`, so a restart recovers EXACT state from the ledger alone.

To stay unit-testable without a live Postgres, `PgStateStore` talks to a tiny `LedgerBackend` protocol
(Postgres in production via a thin INSERT/SELECT adapter; an in-memory `DictLedgerBackend` in tests).
The durability CONTRACT — every appended fill is recoverable, positions recompute from the ledger — is
exercised against the fake exactly as it would be against Pg.

`recover_on_restart` is the G3 query-before-resubmit driver (REQ-S4): for each pending coid it asks the
broker `get_order_by_coid` and resolves the four branches (filled→adopt, open→leave, terminal-no-exec→
free to re-decide, absent→safe to submit) — NEVER a blind re-submit, so a crash between submit and ack
can't double-trade.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_executor import ProductionExecutor
from quantlib.strategy_core.state import StrategyState


class LedgerBackend(Protocol):
    """The durable append-only fill ledger behind `PgStateStore` — Postgres in prod, a dict in tests."""

    def append(self, strategy_id: str, fill: Fill) -> None: ...

    def read(self, strategy_id: str) -> Iterable[Fill]: ...


class DictLedgerBackend:
    """In-process ledger (tests). Models the durable contract: appended fills survive and replay in
    order. A `PgLedgerBackend` would INSERT into / SELECT from the strategy's fills table identically."""

    def __init__(self) -> None:
        self._fills: dict[str, list[Fill]] = {}

    def append(self, strategy_id: str, fill: Fill) -> None:
        self._fills.setdefault(strategy_id, []).append(fill)

    def read(self, strategy_id: str) -> list[Fill]:
        return list(self._fills.get(strategy_id, []))


class PgStateStore:
    """Durable state store (REQ-S2): the append-only fill ledger is the source of truth; `load` rebuilds
    the FULL StrategyState by replaying the ledger through `apply_fill`, so a restart recovers exact
    positions/realized-P&L from durable storage alone — no volatile side-store."""

    def __init__(self, backend: LedgerBackend) -> None:
        self._backend = backend

    def append_fill(self, strategy_id: str, fill: Fill) -> None:
        self._backend.append(strategy_id, fill)

    def load(self, strategy_id: str) -> StrategyState:
        state = StrategyState(strategy_id=strategy_id)
        for fill in self._backend.read(strategy_id):
            state.apply_fill(fill)
        return state

    def save(self, state: StrategyState) -> None:
        """The ledger is authoritative, so a snapshot is not required for correctness. Persisting any
        NEW fills not yet in the durable ledger keeps the two consistent (idempotent: the ledger replays
        them on load). A production Pg store would also snapshot derived state for fast warm reads."""
        durable = {id(fill): fill for fill in self._backend.read(state.strategy_id)}
        # nothing to do if the ledger already holds every fill; this guards a save() called after fills
        # were applied in-memory but not yet appended (the Runner appends on each fill, so normally a noop)
        for fill in state.fills:
            if id(fill) not in durable:
                self._backend.append(state.strategy_id, fill)


# the four G3 branches (REQ-G3) — what recovery did with each pending coid.
ADOPTED_FILL = "adopted_fill"  # broker shows it filled/partial -> adopt, do NOT resubmit
LEFT_OPEN = "left_open"  # broker shows it still open (new/accepted) -> leave for the manage loop
FREE_TO_REDECIDE = (
    "free_to_redecide"  # broker shows it rejected/canceled/expired -> no position, may re-decide
)
SAFE_TO_SUBMIT = "safe_to_submit"  # broker never saw it -> the submit never landed, safe to submit now


def recover_on_restart(
    state: StrategyState, executor: ProductionExecutor, pending_coids: Iterable[str]
) -> dict[str, str]:
    """G3 query-before-resubmit (REQ-S4). For each pending coid, query the broker and resolve the branch.
    Returns {coid: RecoveryAction} — never a blind re-submit. A filled/partial coid the state hasn't
    booked is adopted via `apply_fill` (the ledger stays the source of truth)."""
    actions: dict[str, str] = {}
    booked_coids = {fill.client_order_id for fill in state.fills}
    for coid in pending_coids:
        record = executor.get_order_by_coid(coid)
        if record is None:
            actions[coid] = SAFE_TO_SUBMIT
            continue
        if record.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            if coid not in booked_coids and record.filled_qty > 0.0:
                state.apply_fill(record.to_fill())
                booked_coids.add(coid)
            actions[coid] = ADOPTED_FILL
        elif record.state in (OrderState.NEW, OrderState.ACCEPTED):
            actions[coid] = LEFT_OPEN
        else:  # CANCELED / REJECTED / EXPIRED -> the economic order did not execute
            actions[coid] = FREE_TO_REDECIDE
    return actions
