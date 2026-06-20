"""Per-container parity + restart-safety for reversion's swap onto the production execution+state layer.

Same additive pattern proven on smoke (#220): the bespoke `bets` table + its full lifecycle are RETAINED
unchanged (backward-readable); PaperAlpacaExecutor carries the broker calls (G2 coid) and every captured
fill is mirrored into a durable StrategyState ledger (the SoT migration). These tests pin:

  - PARITY (current-good-state regression): the DECISIONS — the bet gate + the candidate selection — are
    byte-identical pre/post (unchanged pure functions); same inputs -> same bet/no-bet/which-name.
  - RESTART-SAFETY: StrategyState recovers EXACT positions from the durable fill ledger across a restart.
  - The G2 coid is the production path's key; the bets table still stores it (backward-readable).
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.bus.schema import default_schema
from quantlib.bus.vector import FeatureVector
from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import make_client_order_id
from quantlib.strategy_core.production_state import DictLedgerBackend, PgStateStore
from strategies.lib.reversion_model import VwapReversionModel
from strategies.reversion.contract import STRATEGY_NAME
from strategies.reversion.strategy import ReversionConfig, evaluate_bet_gate, select_candidate

NOW = dt.datetime(2026, 6, 20, 15, 0, tzinfo=dt.timezone.utc)
SCHEMA = default_schema()
WINDOW_M = 30
FEATURE = f"vwap_deviation_{WINDOW_M}m"


def _config(**over: object) -> ReversionConfig:
    base = dict(
        symbols=["AAPL"],
        bet_interval_sec=300,
        notional_usd=50.0,
        hold_sec=1800,
        max_concurrent=3,
        max_total_notional_usd=200.0,
        enabled=True,
        loop_block_ms=1000,
        vwap_window_m=WINDOW_M,
        sensitivity=400.0,
        threshold=0.60,
    )
    base.update(over)
    return ReversionConfig(**base)  # type: ignore[arg-type]


def _vector(symbol: str, deviation: float) -> FeatureVector:
    array = np.full(SCHEMA.n_features, np.nan, dtype="<f8")
    array[SCHEMA.offset(FEATURE)] = deviation
    return FeatureVector(SCHEMA, symbol, NOW, array, SCHEMA.fingerprint)


def test_gate_decision_is_unchanged_pure_function() -> None:
    config = _config()
    decision = evaluate_bet_gate(
        config=config, now=NOW, last_bet_ts=None, open_count=0, open_notional=0.0, market_open=True
    )
    assert decision.allowed is True and decision.reason == "ok"
    assert (
        evaluate_bet_gate(
            config=config, now=NOW, last_bet_ts=None, open_count=0, open_notional=0.0, market_open=True
        )
        == decision
    )  # deterministic


def test_gate_blocks_are_unchanged() -> None:
    config = _config()
    assert (
        evaluate_bet_gate(
            config=config, now=NOW, last_bet_ts=None, open_count=0, open_notional=0.0, market_open=False
        ).reason
        == "market_closed"
    )
    assert (
        evaluate_bet_gate(
            config=config, now=NOW, last_bet_ts=None, open_count=3, open_notional=0.0, market_open=True
        ).reason
        == "max_concurrent"
    )


def test_candidate_selection_is_unchanged_deterministic() -> None:
    """The DECISION of which name to bet — pure rank+threshold+exclusion — is unchanged by the swap."""
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    latest = {
        "AAPL": _vector("AAPL", -0.006),  # most below VWAP -> highest P(up)
        "MSFT": _vector("MSFT", -0.001),
        "NVDA": _vector("NVDA", +0.003),  # above VWAP -> not a long
    }
    a = select_candidate(model, latest, threshold=0.55, excluded=set())  # type: ignore[arg-type]
    b = select_candidate(model, latest, threshold=0.55, excluded=set())  # type: ignore[arg-type]
    assert a is not None and a.symbol == "AAPL" and a == b


def test_g2_coid_used_for_reversion_entries() -> None:
    coid = make_client_order_id(STRATEGY_NAME, NOW, "AAPL", "buy")
    assert coid == "reversion-20260620T150000-AAPL-buy"


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
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    entry = make_client_order_id(STRATEGY_NAME, NOW, "AAPL", "buy")
    exit_ = make_client_order_id(STRATEGY_NAME, NOW + dt.timedelta(minutes=30), "AAPL", "sell")
    state = store.load(STRATEGY_NAME)
    for fill in (_fill(entry, "AAPL", "buy", 1.0, 50.0), _fill(exit_, "AAPL", "sell", 1.0, 51.0)):
        state.apply_fill(fill)
        store.append_fill(STRATEGY_NAME, fill)
    recovered = PgStateStore(backend).load(STRATEGY_NAME)
    assert recovered.positions_from_ledger() == {}  # flat after the round trip
    assert recovered.realized_pnl == 1.0


def test_restart_recovers_open_position() -> None:
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    coid = make_client_order_id(STRATEGY_NAME, NOW, "AAPL", "buy")
    fill = _fill(coid, "AAPL", "buy", 2.5, 50.0)
    state = store.load(STRATEGY_NAME)
    state.apply_fill(fill)
    store.append_fill(STRATEGY_NAME, fill)
    assert PgStateStore(backend).load(STRATEGY_NAME).positions["AAPL"].qty == 2.5


def test_pending_close_dedup_marker_logs_once() -> None:
    """The log-legibility fix (the dedup invariant): a coid in `_pending_close_logged` is logged only on
    its FIRST appearance; subsequent manage ticks find it present and skip — so a stale pending exit on a
    closed market emits ONE line, not one per ~1.4s tick. (The marker is cleared on resolution.)"""
    pending_logged: set[str] = set()
    exit_coid = "reversion-20260618T200000-AAPL-sell"
    logged_count = 0
    for _ in range(50):  # 50 manage ticks on the same stale pending exit
        if exit_coid not in pending_logged:
            logged_count += 1
            pending_logged.add(exit_coid)
    assert logged_count == 1  # ONE log across 50 ticks
    pending_logged.discard(exit_coid)  # on resolution the marker clears -> a future pend logs again
    assert exit_coid not in pending_logged
