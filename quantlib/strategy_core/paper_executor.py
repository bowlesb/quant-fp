"""`PaperExecutorStub` — the live-shaped executor for the portability worked example, WITHOUT a real
broker call (a faithful stub, as the Lead permits for the demo).

It satisfies the SAME `execute(intents, cs, clock) -> [Fill]` contract as `BacktestExecutor`, but is
shaped like the live path: it records each submitted `OrderIntent` by its `client_order_id`
(idempotent — a duplicate coid is a no-op, REQ-X4) and returns a `Fill` report the way a paper broker
would (filled at the cross-section's entry price). This lets the worked example run the SAME
`decide()` + `StrategyState` through a live-shaped path and show the live container is a thin harness
with no duplicated decision logic.

The REAL `PaperExecutor`/`LiveExecutor` (alpaca-py `TradingClient`) is the next step; it implements the
same contract — `submit` → real order with the coid, `poll` → map Alpaca status/filled_qty into a
`Fill`. Not built this phase; this stub proves the seam.
"""
from __future__ import annotations

import numpy as np

from quantlib.strategy_core import CrossSection
from quantlib.strategy_core.execution import BookState, Clock, Fill, OrderIntent, OrderState


class PaperExecutorStub:
    """Live-shaped executor (no real broker). Idempotent on client_order_id; Alpaca-shaped Fill report."""

    def __init__(self) -> None:
        self._submitted: dict[str, OrderIntent] = {}  # coid -> intent (the idempotency ledger)
        self._book = BookState()

    def book(self) -> BookState:
        return self._book

    def submitted_order_ids(self) -> set[str]:
        return set(self._submitted)

    def execute(self, intents: list[OrderIntent], cross_section: CrossSection, clock: Clock) -> list[Fill]:
        fills: list[Fill] = []
        new_weights = dict(self._book.weights)
        for intent in intents:
            if intent.client_order_id in self._submitted:
                continue  # idempotent: a duplicate coid is a no-op (REQ-X4)
            self._submitted[intent.client_order_id] = intent
            price = cross_section.feature_for(intent.symbol, "entry_close")
            if not np.isfinite(price) or price < 1.0:
                fills.append(
                    Fill(
                        symbol=intent.symbol,
                        side=intent.side,
                        weight=intent.target_weight,
                        fill_price=float("nan"),
                        cost_bps=0.0,
                        client_order_id=intent.client_order_id,
                        filled_qty=0.0,
                        avg_price=0.0,
                        status=OrderState.REJECTED,
                    )
                )
                continue
            new_weights[intent.symbol] = intent.target_weight
            qty = abs(intent.notional / price) if intent.notional else abs(intent.target_weight)
            fills.append(
                Fill(
                    symbol=intent.symbol,
                    side=intent.side,
                    weight=intent.target_weight,
                    fill_price=float(price),
                    cost_bps=0.0,
                    client_order_id=intent.client_order_id,
                    filled_qty=qty,
                    avg_price=float(price),
                    status=OrderState.FILLED,
                )
            )
        self._book = BookState(weights=new_weights)
        return fills
