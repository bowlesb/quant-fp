"""The PRODUCTION execution layer — the realtime harness that makes a strategy actually trade with real,
reconciled, restart-safe state (docs/STRATEGY_EXECUTION_ABSTRACTION.md, the G1-G7 fixes).

This sits ON TOP of the shared primitives:
  - decision logic stays in `DecisionCore.decide` (REQ-D1, unchanged);
  - `StrategyState` / `apply_fill` (state.py) is the ONE typed transition both paths call — it already
    books the ACTUAL filled_qty (REQ-G5: partials change the weight, not the cost).

What this module adds for production fidelity (the portability `execution.py` is the battery-seam demo;
THIS is the broker-faithful layer):
  - `ProductionOrderIntent` with the G2 fully-qualifying `client_order_id`
    (`{strategy}-{YYYYMMDDTHHMMSS}-{symbol}-{side}`) — idempotent + per-strategy attributable (G1).
  - the full Alpaca `OrderState` lifecycle (NEW…ACCEPTED…PARTIALLY_FILLED…FILLED/CANCELED/REJECTED/
    EXPIRED) — partials and rejects are first-class (REQ-X3), never time-outs (the prior repo's L2 wall).
  - ONE `ProductionExecutor` protocol: `submit / poll / cancel / positions / account / pre_trade_check /
    reconcile`. `FaithfulBacktestExecutor` (Alpaca-faithful sim) and `PaperBrokerStub` (live-shaped, no
    real broker) both satisfy it — so a conformance test pins the sim against the live shape (REQ-X1/X2,
    the anti-L1 proof).
  - `pre_trade_check` (G4): a buying-power / shortable / PDT gate BEFORE a basket submits, so a basket
    never lands lopsided.
  - per-strategy `reconcile` (G1): the broker truth for THIS strategy = the net of broker fills whose
    coid is in this strategy's namespace; a broker position with no coid in the namespace (a sibling's)
    is NEVER adopted.
  - corporate-actions mid-hold (G6) applied in reconcile before the broker-vs-state compare.

No bus/redis/alpaca import here — the live `PaperExecutor` will subclass/duck-type the broker handle;
this module is pure and unit-testable (the broker is a small in-process fake in tests).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from quantlib.strategy_core.execution import Fill
from quantlib.strategy_core.state import StrategyState


def make_client_order_id(strategy_id: str, decision_ts: dt.datetime, symbol: str, side: str) -> str:
    """The G2 fully-qualifying coid: ``{strategy}-{YYYYMMDDTHHMMSS}-{symbol}-{side}``. Encodes the
    strategy (the G1 namespace), the full date+second timestamp (no cross-day collision; second
    resolution distinguishes a same-minute re-decision), the symbol and the side. Deterministic from
    (strategy_id, decision_ts, symbol, side) -> idempotent re-submit (REQ-X4) AND per-strategy
    attributable (G1). Requires a tz-aware UTC ``decision_ts`` (the UTC-everywhere rule)."""
    if decision_ts.tzinfo is None:
        raise ValueError("decision_ts must be tz-aware UTC (the UTC-everywhere rule)")
    if "-" in strategy_id:
        # the coid is '-'-delimited and strategy_id is its FIRST segment (the G1 namespace key); a hyphen
        # in the id would make strategy_id_of un-attributable.
        raise ValueError(f"strategy_id must not contain '-' (got {strategy_id!r})")
    stamp = decision_ts.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{strategy_id}-{stamp}-{symbol}-{side}"


def strategy_id_of(client_order_id: str) -> str:
    """The strategy namespace a coid belongs to — the first ``-``-delimited segment (G1 attribution)."""
    return client_order_id.split("-", 1)[0]


@dataclass(frozen=True)
class ProductionOrderIntent:
    """What the strategy wants to transact — broker-agnostic, qty/notional oriented (not book-weight).

    ``client_order_id`` is the G2 deterministic key. ``qty`` is signed-by-``side`` magnitude (shares);
    ``notional`` an alternative absolute dollar size (one of qty/notional is set). The closed TIF/type
    sets mirror Alpaca (no open-ended action)."""

    strategy_id: str
    symbol: str
    side: str  # "buy" | "sell"
    decision_ts: dt.datetime
    qty: float | None = None
    notional: float | None = None
    order_type: str = "market"  # market | limit | stop | stop_limit
    tif: str = "day"  # day | cls | opg | gtc
    limit_price: float | None = None
    stop_price: float | None = None
    reason: str = ""

    @property
    def client_order_id(self) -> str:
        return make_client_order_id(self.strategy_id, self.decision_ts, self.symbol, self.side)


@dataclass(frozen=True)
class Account:
    """The broker account snapshot the pre-trade gate reads (REQ-G4)."""

    buying_power: float
    daytrade_count: int = 0
    pattern_day_trader: bool = False


@dataclass(frozen=True)
class PreTradeResult:
    """The G4 gate's verdict: which intents are admitted and which are rejected (with a reason each)."""

    admitted: list[ProductionOrderIntent]
    rejected: list[tuple[ProductionOrderIntent, str]]


@dataclass(frozen=True)
class CorporateAction:
    """A split/dividend on a held symbol during a multi-day hold (REQ-G6). ``split_ratio`` 2.0 = a 2:1
    split (shares double, avg entry halves); ``cash_dividend`` is per-share cash booked to realized."""

    symbol: str
    effective: dt.datetime
    split_ratio: float = 1.0
    cash_dividend: float = 0.0


@dataclass
class ReconcileReport:
    """The outcome of a per-strategy reconcile (REQ-S3/G1): what was adopted from the broker, what drift
    was found, whether it alerts, and which sibling positions were (correctly) ignored."""

    adopted: dict[str, float] = field(default_factory=dict)  # symbol -> broker qty adopted into state
    drift: dict[str, float] = field(default_factory=dict)  # symbol -> (broker - state) qty mismatch
    ignored_siblings: list[str] = field(default_factory=list)  # broker positions not in our namespace
    corporate_actions_applied: list[str] = field(default_factory=list)
    alert: bool = False


# Drift beyond this (abs qty, summed) raises the ReconcileReport.alert flag (never silent auto-fix, R3).
DRIFT_ALERT_QTY = 1.0


def pre_trade_check(
    intents: Sequence[ProductionOrderIntent],
    account: Account,
    *,
    price_of: Mapping[str, float],
    shortable: Mapping[str, bool],
    pdt_limit: int = 3,
) -> PreTradeResult:
    """G4: admit a basket only if the account can support it. Sums the basket's required buying power
    (qty * price, or notional), rejects a short leg whose symbol isn't shortable, and rejects the whole
    basket if it would breach the PDT day-trade limit. Returns admitted + rejected-with-reason so the
    strategy's sizing policy decides (scale pro-rata / skip) rather than submitting a lopsided basket.

    This is execution policy (lives here, NOT in `decide`) — REQ-G4."""
    admitted: list[ProductionOrderIntent] = []
    rejected: list[tuple[ProductionOrderIntent, str]] = []
    if account.pattern_day_trader and account.daytrade_count >= pdt_limit:
        return PreTradeResult(admitted=[], rejected=[(intent, "pdt_limit") for intent in intents])
    required_bp = 0.0
    for intent in intents:
        if intent.side == "sell" and not shortable.get(intent.symbol, False):
            rejected.append((intent, "not_shortable"))
            continue
        price = price_of.get(intent.symbol)
        if price is None or price <= 0.0:
            rejected.append((intent, "no_price"))
            continue
        cost = intent.notional if intent.notional is not None else abs(intent.qty or 0.0) * price
        required_bp += cost
        admitted.append(intent)
    if required_bp > account.buying_power:
        # the whole admitted basket can't be funded -> reject it (the caller scales pro-rata or skips,
        # preserving neutrality), never submit a knowingly-lopsided subset.
        return PreTradeResult(
            admitted=[], rejected=[(intent, "insufficient_buying_power") for intent in admitted] + rejected
        )
    return PreTradeResult(admitted=admitted, rejected=rejected)


def apply_corporate_action(state: StrategyState, action: CorporateAction) -> None:
    """G6: apply a split/dividend to a held position BEFORE the broker-vs-state compare, so the broker's
    post-action qty matches the adjusted state qty (a reconciled adjustment, not spurious drift)."""
    position = state.positions.get(action.symbol)
    if position is None:
        return
    if action.split_ratio != 1.0:
        position.qty *= action.split_ratio
        position.avg_entry_price /= action.split_ratio
    if action.cash_dividend != 0.0:
        state.realized_pnl += action.cash_dividend * position.qty


def reconcile(
    state: StrategyState,
    broker_fills: Sequence[Fill],
    *,
    corporate_actions: Sequence[CorporateAction] = (),
) -> ReconcileReport:
    """Per-strategy reconcile (REQ-S3/G1). The broker truth for THIS strategy is the net of ``broker_fills``
    whose coid is in THIS strategy's namespace; a fill outside the namespace (a sibling's) is IGNORED,
    never adopted. Corporate actions (G6) are applied to held positions first. On a coid-attributed
    broker fill the state never saw, we adopt it via `apply_fill` (the ledger stays authoritative). Drift
    (broker net != state net for an in-namespace symbol) is recorded; large drift alerts (R3, no silent
    auto-fix)."""
    report = ReconcileReport()
    for action in corporate_actions:
        apply_corporate_action(state, action)
        report.corporate_actions_applied.append(action.symbol)

    seen_coids = {fill.client_order_id for fill in state.fills}
    broker_net: dict[str, float] = {}
    for fill in broker_fills:
        if strategy_id_of(fill.client_order_id) != state.strategy_id:
            report.ignored_siblings.append(fill.symbol)  # a sibling strategy's fill — NOT ours (G1)
            continue
        signed = fill.filled_qty if fill.side == "buy" else -fill.filled_qty
        broker_net[fill.symbol] = broker_net.get(fill.symbol, 0.0) + signed
        if fill.client_order_id not in seen_coids:
            state.apply_fill(fill)  # a coid-attributed fill the state missed -> adopt it
            seen_coids.add(fill.client_order_id)
            report.adopted[fill.symbol] = report.adopted.get(fill.symbol, 0.0) + signed

    state_net = {symbol: position.qty for symbol, position in state.positions.items()}
    total_drift = 0.0
    for symbol in set(broker_net) | set(state_net):
        diff = broker_net.get(symbol, 0.0) - state_net.get(symbol, 0.0)
        if abs(diff) > 1e-9:
            report.drift[symbol] = diff
            total_drift += abs(diff)
    report.alert = total_drift >= DRIFT_ALERT_QTY
    return report
