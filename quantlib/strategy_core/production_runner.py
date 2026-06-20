"""`ProductionRunner` — the ONE loop that ties a strategy's pure decision to the production execution+state
layer (docs/STRATEGY_EXECUTION_ABSTRACTION.md §4/§5). A live container is a thin wrapper around this.

Each cycle:
  1. the strategy's PURE decision produces `ProductionOrderIntent`s (decide() UNCHANGED — it reads features
     by name and returns intents; no executor/store/clock leaks into it);
  2. `pre_trade_check` (G4) admits the basket against the account (BP/PDT/shortable);
  3. each admitted intent is `submit`ted to the `ProductionExecutor` (idempotent on the G2 coid);
  4. a fill (full or partial) is booked via `StrategyState.apply_fill` (the ONE transition; G5 books the
     ACTUAL filled weight) AND durably appended to the `StateStore` ledger (REQ-S2);
  5. on startup, `recover_on_restart` (G3) resolves any pending coids and `reconcile` (G1, per-strategy)
     adopts the broker truth for THIS strategy only.

The runner imports the executor/state CONTRACTS, not a specific broker — a backtest passes a
`FaithfulBacktestExecutor` + `MemoryStateStore`; live passes a `PaperAlpacaExecutor` + `PgStateStore`.
SAME runner, swapped components — no second decision implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import (
    Account,
    ProductionOrderIntent,
    pre_trade_check,
    reconcile,
)
from quantlib.strategy_core.production_executor import ProductionExecutor
from quantlib.strategy_core.production_state import recover_on_restart
from quantlib.strategy_core.state import StateStore, StrategyState

logger = logging.getLogger(__name__)


class DecisionSource(Protocol):
    """A strategy's per-cycle decision — pure, returns the intents to transact (decide() UNCHANGED)."""

    def intents(self) -> list[ProductionOrderIntent]: ...


class ProductionRunner:
    """Ties {decision, executor, state, store} — the one loop both backtest and live share."""

    def __init__(
        self,
        strategy_id: str,
        executor: ProductionExecutor,
        store: StateStore,
        *,
        account_provider: "AccountProvider | None" = None,
    ) -> None:
        self._strategy_id = strategy_id
        self._executor = executor
        self._store = store
        self._account_provider = account_provider
        self._state: StrategyState = store.load(strategy_id)

    @property
    def state(self) -> StrategyState:
        return self._state

    def recover(self, pending_coids: Sequence[str], broker_fills: Sequence[object]) -> None:
        """Startup recovery (REQ-S4): G3 query-before-resubmit for pending coids, then G1 per-strategy
        reconcile against the broker fills (broker wins). `broker_fills` are this strategy's namespace
        fills (e.g. `PaperAlpacaExecutor.broker_fills_for_strategy`)."""
        recover_on_restart(self._state, self._executor, pending_coids)
        reconcile(self._state, broker_fills)  # type: ignore[arg-type]
        self._store.save(self._state)

    def submit_intents(self, intents: list[ProductionOrderIntent]) -> list[str]:
        """Run the G4 gate then submit the admitted intents, booking each fill into state + the durable
        ledger. Returns the coids that were submitted (for the manage loop). PURE of the decision —
        `intents` come from the strategy's unchanged decide()."""
        admitted = self._gate(intents)
        submitted: list[str] = []
        for intent in admitted:
            record = self._executor.submit(intent)
            # production tracking is by the durable ledger + the broker (queried by coid), not the battery
            # `pending` dict — the manage loop polls each returned coid; the ledger is the position SoT.
            submitted.append(intent.client_order_id)
            if record.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED) and record.filled_qty > 0:
                fill = record.to_fill()
                self._state.apply_fill(fill)
                self._store.append_fill(self._strategy_id, fill)
        self._store.save(self._state)
        return submitted

    def manage(self, coids: Sequence[str]) -> None:
        """Poll open coids; book any newly-filled qty into state + the ledger (cumulative-safe: apply_fill
        books the delta via the cumulative fill the broker reports)."""
        for coid in coids:
            fill = self._executor.poll(coid)
            already = self._filled_so_far(coid)
            if fill.filled_qty > already and fill.status in (
                OrderState.FILLED,
                OrderState.PARTIALLY_FILLED,
            ):
                # book the INCREMENT (the broker reports cumulative; apply_fill expects the new qty as a
                # signed add, so emit a delta fill).
                delta = fill.filled_qty - already
                delta_fill = Fill(
                    symbol=fill.symbol,
                    side=fill.side,
                    weight=fill.weight,
                    fill_price=fill.fill_price,
                    cost_bps=fill.cost_bps,
                    client_order_id=fill.client_order_id,
                    filled_qty=delta,
                    avg_price=fill.avg_price,
                    status=fill.status,
                )
                self._state.apply_fill(delta_fill)
                self._store.append_fill(self._strategy_id, delta_fill)
        self._store.save(self._state)

    def _filled_so_far(self, coid: str) -> float:
        """Cumulative filled qty already booked for a coid (sum of its ledger fills)."""
        return sum(fill.filled_qty for fill in self._state.fills if fill.client_order_id == coid)

    def _gate(self, intents: list[ProductionOrderIntent]) -> list[ProductionOrderIntent]:
        if self._account_provider is None:
            return intents  # no account context (e.g. backtest) -> no pre-trade gate
        account, price_of, shortable = self._account_provider.snapshot(intents)
        result = pre_trade_check(intents, account, price_of=price_of, shortable=shortable)
        for intent, reason in result.rejected:
            logger.info("pre_trade_check rejected %s: %s", intent.client_order_id, reason)
        return result.admitted


class AccountProvider(Protocol):
    """Supplies the pre-trade gate's inputs (the account snapshot + per-symbol price/shortable) for a
    basket. A live container reads these from the broker; a backtest from the panel."""

    def snapshot(
        self, intents: Sequence[ProductionOrderIntent]
    ) -> tuple[Account, dict[str, float], dict[str, bool]]: ...
