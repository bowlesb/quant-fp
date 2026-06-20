"""Per-container parity + restart-safety for smoke's swap onto the production execution+state layer.

The swap is ADDITIVE: the bespoke `bets` table + its lifecycle are RETAINED unchanged (backward-readable);
the production `PaperAlpacaExecutor` carries the broker calls (with the G2 coid) and every captured fill is
MIRRORED into a durable `StrategyState` ledger (the SoT migration). These tests pin:

  - PARITY (Ben's current-good-state regression): the DECISIONS are byte-identical pre/post — the gate
    (`evaluate_bet_gate`) + the model overlay are unchanged pure functions; same inputs -> same bet/no-bet.
  - RESTART-SAFETY: the StrategyState recovers EXACT positions from the durable fill ledger across a
    restart (via PgStateStore over an in-memory LedgerBackend fake — the same contract as the live
    PgFillLedger, no DB needed).
  - The G2 coid is what the production path now uses; the bets table still stores it (backward-readable).
"""

from __future__ import annotations

import datetime as dt

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import make_client_order_id
from quantlib.strategy_core.production_state import DictLedgerBackend, PgStateStore
from strategies.smoke.contract import STRATEGY_NAME
from strategies.smoke.strategy import SmokeConfig, evaluate_bet_gate

NOW = dt.datetime(2026, 6, 20, 15, 0, tzinfo=dt.timezone.utc)


def _config(**over: object) -> SmokeConfig:
    base = dict(
        symbols=["AAPL"],
        bet_interval_sec=300,
        notional_usd=50.0,
        hold_sec=1800,
        max_concurrent=3,
        max_total_notional_usd=200.0,
        enabled=True,
        loop_block_ms=1000,
        use_model=False,
        model_threshold=0.5,
    )
    base.update(over)
    return SmokeConfig(**base)  # type: ignore[arg-type]


def test_decide_gate_is_unchanged_pure_function() -> None:
    """The bet gate — the decision — is a pure function of its inputs, identical pre/post the execution
    swap (the swap touched only the broker/state plumbing, never evaluate_bet_gate)."""
    config = _config()
    allowed = evaluate_bet_gate(
        config=config,
        now=NOW,
        last_bet_ts=None,
        open_count=0,
        open_notional=0.0,
        market_open=True,
        last_symbol="AAPL",
    )
    assert allowed.allowed is True and allowed.reason == "ok"
    # deterministic: same inputs -> same decision
    again = evaluate_bet_gate(
        config=config,
        now=NOW,
        last_bet_ts=None,
        open_count=0,
        open_notional=0.0,
        market_open=True,
        last_symbol="AAPL",
    )
    assert again == allowed


def test_gate_blocks_are_unchanged() -> None:
    """The same block reasons fire identically — caps/cadence/market-hours decisions unchanged."""
    config = _config()
    assert (
        evaluate_bet_gate(
            config=config,
            now=NOW,
            last_bet_ts=None,
            open_count=0,
            open_notional=0.0,
            market_open=False,
            last_symbol="AAPL",
        ).reason
        == "market_closed"
    )
    assert (
        evaluate_bet_gate(
            config=config,
            now=NOW,
            last_bet_ts=None,
            open_count=3,
            open_notional=0.0,
            market_open=True,
            last_symbol="AAPL",
        ).reason
        == "max_concurrent"
    )
    assert (
        evaluate_bet_gate(
            config=config,
            now=NOW,
            last_bet_ts=NOW,
            open_count=0,
            open_notional=0.0,
            market_open=True,
            last_symbol="AAPL",
        ).reason
        == "within_cadence"
    )


def test_g2_coid_used_for_smoke_entries() -> None:
    """The production path stamps the G2 coid; it's deterministic + attributable to smoke."""
    coid = make_client_order_id(STRATEGY_NAME, NOW, "AAPL", "buy")
    assert coid == "smoke-20260620T150000-AAPL-buy"


def _fill(coid: str, symbol: str, side: str, qty: float, price: float) -> Fill:
    return Fill(
        symbol=symbol,
        side=side,
        weight=0.0,
        fill_price=price,
        cost_bps=0.0,
        client_order_id=coid,
        filled_qty=qty,
        avg_price=price,
        status=OrderState.FILLED,
    )


def test_restart_recovers_exact_state_from_ledger() -> None:
    """The migration's restart-safety: book an entry + its close into the durable ledger; a fresh
    PgStateStore.load replays the ledger to the EXACT net position (flat after a round trip)."""
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    entry_coid = make_client_order_id(STRATEGY_NAME, NOW, "AAPL", "buy")
    exit_coid = make_client_order_id(STRATEGY_NAME, NOW + dt.timedelta(minutes=30), "AAPL", "sell")
    state = store.load(STRATEGY_NAME)
    for fill in (
        _fill(entry_coid, "AAPL", "buy", 1.0, 50.0),
        _fill(exit_coid, "AAPL", "sell", 1.0, 51.0),
    ):
        state.apply_fill(fill)
        store.append_fill(STRATEGY_NAME, fill)
    # restart: a fresh store load rebuilds the exact state from the durable ledger.
    recovered = PgStateStore(backend).load(STRATEGY_NAME)
    assert recovered.positions_from_ledger() == {}  # flat after the round trip
    assert recovered.realized_pnl == 1.0  # (51 - 50) * 1 share


def test_restart_recovers_open_position() -> None:
    """An entry that filled but hasn't closed -> the position is recovered exactly from the ledger."""
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    coid = make_client_order_id(STRATEGY_NAME, NOW, "AAPL", "buy")
    fill = _fill(coid, "AAPL", "buy", 2.5, 50.0)
    state = store.load(STRATEGY_NAME)
    state.apply_fill(fill)
    store.append_fill(STRATEGY_NAME, fill)
    recovered = PgStateStore(backend).load(STRATEGY_NAME)
    assert recovered.positions["AAPL"].qty == 2.5
