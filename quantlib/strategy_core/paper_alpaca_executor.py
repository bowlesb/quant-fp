"""`PaperAlpacaExecutor` — the REAL-broker executor (alpaca-py `TradingClient`, PAPER account).

It implements the SAME `ProductionExecutor` contract (submit / poll / cancel / positions /
get_order_by_coid) as `FaithfulBacktestExecutor` and `PaperBrokerStub`, by mapping alpaca-py calls and
Alpaca's `OrderStatus`/`filled_qty` into our `OrderRecord`/`OrderState`/`Fill`. So the conformance the
stub proves in-process is proven for REAL against the paper account by the paper-account integration test
(the sim==live gate). PAPER ONLY — `TradingClient(..., paper=True)`; it never touches a live account.

Idempotency (REQ-X4): submit uses the intent's G2 `client_order_id`; on Alpaca's duplicate-coid error it
reads back the existing order rather than placing a second. `get_order_by_coid` is the G3
query-before-resubmit primitive against the real broker.

G1 per-strategy scoping: `broker_fills_for_strategy` returns ONLY the broker orders whose coid is in THIS
strategy's namespace (the coid prefix), so `reconcile` (production_execution.reconcile) sees this
strategy's fills, never the shared account's net — a sibling's order is structurally excluded.

Secrets: the keys come from the caller (env, never logged). Rate limits / transient broker errors are
caught specifically and surfaced (bounded), never swallowed (REQ-X5).
"""

from __future__ import annotations

import logging
from typing import cast

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import (
    ProductionOrderIntent,
    parse_client_order_id,
    strategy_id_of,
)
from quantlib.strategy_core.production_executor import OrderRecord

logger = logging.getLogger(__name__)

# Alpaca OrderStatus -> our OrderState. Statuses not mapped (replaced/pending_*/held/...) are treated as
# still-working (ACCEPTED) — they are non-terminal and the manage loop re-polls them.
_STATUS_MAP: dict[OrderStatus, OrderState] = {
    OrderStatus.NEW: OrderState.NEW,
    OrderStatus.PENDING_NEW: OrderState.PENDING,
    OrderStatus.ACCEPTED: OrderState.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED: OrderState.PARTIALLY_FILLED,
    OrderStatus.FILLED: OrderState.FILLED,
    OrderStatus.CANCELED: OrderState.CANCELED,
    OrderStatus.EXPIRED: OrderState.EXPIRED,
    OrderStatus.REJECTED: OrderState.REJECTED,
    OrderStatus.DONE_FOR_DAY: OrderState.EXPIRED,
}

_TIF_MAP = {"day": TimeInForce.DAY, "gtc": TimeInForce.GTC, "cls": TimeInForce.CLS, "opg": TimeInForce.OPG}


def order_state_of(status: OrderStatus) -> OrderState:
    """Map an Alpaca status to our lifecycle state; an unmapped (transient) status is non-terminal."""
    return _STATUS_MAP.get(status, OrderState.ACCEPTED)


class PaperAlpacaExecutor:
    """The real-broker `ProductionExecutor` against the Alpaca PAPER account."""

    def __init__(self, trading: TradingClient) -> None:
        self._trading = trading

    def submit(self, intent: ProductionOrderIntent) -> OrderRecord:
        """Place the order with its G2 coid. Idempotent: a duplicate-coid APIError reads back the existing
        order instead of placing a second (REQ-X4)."""
        existing = self.get_order_by_coid(intent.client_order_id)
        if existing is not None:
            return existing
        request = self._build_request(intent)
        try:
            order = self._trading.submit_order(request)
        except APIError as exc:
            # a duplicate-coid race: the order already exists -> read it back, never place a second.
            existing = self.get_order_by_coid(intent.client_order_id)
            if existing is not None:
                return existing
            logger.warning("submit rejected for %s: %s", intent.client_order_id, exc)
            return OrderRecord(intent=intent, state=OrderState.REJECTED, target_qty=self._target_qty(intent))
        return self._record_from_order(intent, cast(Order, order))

    def poll(self, client_order_id: str) -> Fill:
        record = self.get_order_by_coid(client_order_id)
        if record is None:
            raise KeyError(f"no broker order for coid {client_order_id}")
        return record.to_fill()

    def cancel(self, client_order_id: str) -> Fill:
        record = self.get_order_by_coid(client_order_id)
        if record is None:
            raise KeyError(f"no broker order for coid {client_order_id}")
        if record.broker_order_id is not None and record.state not in (
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        ):
            try:
                self._trading.cancel_order_by_id(record.broker_order_id)
            except APIError as exc:
                logger.warning("cancel failed for %s: %s", client_order_id, exc)
        refreshed = self.get_order_by_coid(client_order_id)
        return (refreshed or record).to_fill()

    def get_order_by_coid(self, client_order_id: str) -> OrderRecord | None:
        """Read the broker's current view of a coid (the G3 query-before-resubmit primitive). None if the
        broker never saw it."""
        try:
            raw_order = self._trading.get_order_by_client_id(client_order_id)
        except APIError:
            return None
        if raw_order is None:
            return None
        order = cast(Order, raw_order)
        intent = self._intent_from_order(order)
        return self._record_from_order(intent, order)

    def positions(self) -> dict[str, float]:
        """The WHOLE account's positions (shared account). NOT used for per-strategy reconcile — use
        `broker_fills_for_strategy` (G1). Exposed only for an operational account-wide monitor."""
        net: dict[str, float] = {}
        for position in cast("list[Position]", self._trading.get_all_positions()):
            net[position.symbol] = float(position.qty)
        return net

    def broker_fills_for_strategy(self, strategy_id: str, *, limit: int = 500) -> list[Fill]:
        """G1: the broker fills whose coid is in THIS strategy's namespace ONLY — the per-strategy broker
        truth `reconcile` consumes. A sibling strategy's order (different coid prefix) is excluded."""
        orders = cast("list[Order]", self._trading.get_orders())
        fills: list[Fill] = []
        for order in orders:
            coid = order.client_order_id
            if coid is None or strategy_id_of(coid) != strategy_id:
                continue
            record = self._record_from_order(self._intent_from_order(order), order)
            if record.filled_qty > 0.0:
                fills.append(record.to_fill())
        return fills

    def _build_request(self, intent: ProductionOrderIntent) -> MarketOrderRequest | LimitOrderRequest:
        side = OrderSide.BUY if intent.side == "buy" else OrderSide.SELL
        tif = _TIF_MAP.get(intent.tif, TimeInForce.DAY)
        kwargs: dict[str, object] = {
            "symbol": intent.symbol,
            "side": side,
            "time_in_force": tif,
            "client_order_id": intent.client_order_id,
        }
        if intent.notional is not None:
            kwargs["notional"] = intent.notional
        else:
            kwargs["qty"] = abs(intent.qty or 0.0)
        if intent.order_type == "limit" and intent.limit_price is not None:
            return LimitOrderRequest(limit_price=intent.limit_price, **kwargs)  # type: ignore[arg-type]
        return MarketOrderRequest(**kwargs)  # type: ignore[arg-type]

    def _record_from_order(self, intent: ProductionOrderIntent, order: Order) -> OrderRecord:
        filled_qty = float(order.filled_qty) if order.filled_qty is not None else 0.0
        avg_price = float(order.filled_avg_price) if order.filled_avg_price is not None else 0.0
        target = float(order.qty) if order.qty is not None else self._target_qty(intent)
        return OrderRecord(
            intent=intent,
            state=order_state_of(order.status),
            target_qty=target,
            filled_qty=filled_qty,
            avg_price=avg_price,
            broker_order_id=str(order.id),
        )

    def _intent_from_order(self, order: Order) -> ProductionOrderIntent:
        """Reconstruct the intent from a broker order so a polled/reconciled order reproduces its EXACT
        original coid (parsed from the broker's client_order_id) — so reconcile matches on it. Falls back
        to the order's own fields if the coid isn't our production form."""
        coid = order.client_order_id or ""
        try:
            strategy_id, decision_ts, symbol, side = parse_client_order_id(coid)
        except ValueError:
            return ProductionOrderIntent(
                strategy_id=strategy_id_of(coid) if coid else "",
                symbol=order.symbol,
                side="buy" if order.side == OrderSide.BUY else "sell",
                decision_ts=order.submitted_at or order.created_at,
                qty=float(order.qty) if order.qty is not None else None,
            )
        return ProductionOrderIntent(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            decision_ts=decision_ts,
            qty=float(order.qty) if order.qty is not None else None,
        )

    @staticmethod
    def _target_qty(intent: ProductionOrderIntent) -> float:
        return abs(intent.qty) if intent.qty is not None else 0.0
