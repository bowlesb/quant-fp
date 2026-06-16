"""Reversion-strategy logic tests: the VwapReversionModel (sign, monotonicity, NaN-safety,
determinism), the pure bet GATE, the pure candidate selection (rank + threshold + exclusion), and a full
place -> manage -> finalize flow against a FAKE broker + in-memory store (no real orders placed).

All tests are network-free: vectors are built directly from ``default_schema()`` (no bus round-trip
needed), so the model + selection logic is exercised deterministically.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pytest

from quantlib.bus.schema import default_schema
from quantlib.bus.vector import FeatureVector
from strategies.lib.reversion_model import VwapReversionModel
from strategies.reversion.strategy import (
    ReversionConfig,
    ReversionStrategy,
    evaluate_bet_gate,
    order_filled_qty,
    select_candidate,
)

NOW = dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)
SCHEMA = default_schema()
WINDOW_M = 30
FEATURE = f"vwap_deviation_{WINDOW_M}m"


def _vector(symbol: str, deviation: float) -> FeatureVector:
    """A decoded vector for ``symbol`` whose ``vwap_deviation_{WINDOW_M}m`` cell = ``deviation`` (all
    other cells NaN — the model reads only the one feature it depends on)."""
    array = np.full(SCHEMA.n_features, np.nan, dtype=np.float64)
    array[SCHEMA.offset(FEATURE)] = deviation
    return FeatureVector(SCHEMA, symbol, NOW, array, SCHEMA.fingerprint)


def _config(**overrides: object) -> ReversionConfig:
    base: dict[str, object] = {
        "symbols": ["AAPL"],
        "bet_interval_sec": 300,
        "notional_usd": 50.0,
        "hold_sec": 1800,
        "max_concurrent": 3,
        "max_total_notional_usd": 200.0,
        "enabled": True,
        "loop_block_ms": 100,
        "vwap_window_m": WINDOW_M,
        "sensitivity": 400.0,
        "threshold": 0.60,
    }
    base.update(overrides)
    return ReversionConfig(**base)  # type: ignore[arg-type]


def test_model_below_vwap_is_bullish() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    # Stretched BELOW VWAP (negative deviation) -> reversion view UP -> probability > 0.5.
    assert model.predict(_vector("AAPL", -0.005)).probability > 0.5
    # Stretched ABOVE VWAP -> probability < 0.5 (no long).
    assert model.predict(_vector("AAPL", 0.005)).probability < 0.5
    # At VWAP -> exactly 0.5 (no view).
    assert model.predict(_vector("AAPL", 0.0)).probability == pytest.approx(0.5)


def test_model_more_stretched_is_more_bullish() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    mild = model.predict(_vector("AAPL", -0.001)).probability
    strong = model.predict(_vector("AAPL", -0.005)).probability
    assert strong > mild > 0.5  # monotone in how far below VWAP


def test_model_nan_feature_is_no_signal() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    prediction = model.predict(_vector("AAPL", float("nan")))
    assert prediction.probability == pytest.approx(0.5)  # warmup/sparse -> below any threshold


def test_model_is_deterministic() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    a = model.predict(_vector("AAPL", -0.003)).probability
    b = model.predict(_vector("AAPL", -0.003)).probability
    assert a == b


def test_select_candidate_picks_most_stretched() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    latest = {
        "AAPL": _vector("AAPL", -0.001),  # mildly below VWAP
        "MSFT": _vector("MSFT", -0.006),  # most below VWAP -> should win
        "NVDA": _vector("NVDA", +0.004),  # above VWAP -> not a long
    }
    candidate = select_candidate(model, latest, threshold=0.55, excluded=set())
    assert candidate is not None
    assert candidate.symbol == "MSFT"
    assert candidate.probability > 0.55


def test_select_candidate_respects_threshold() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    latest = {"AAPL": _vector("AAPL", -0.0001)}  # barely below VWAP -> p just above 0.5
    assert select_candidate(model, latest, threshold=0.60, excluded=set()) is None


def test_select_candidate_excludes_held_symbols() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    latest = {"MSFT": _vector("MSFT", -0.006)}
    assert select_candidate(model, latest, threshold=0.55, excluded={"MSFT"}) is None


def test_select_candidate_skips_nan() -> None:
    model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)
    latest = {"AAPL": _vector("AAPL", float("nan"))}
    assert select_candidate(model, latest, threshold=0.55, excluded=set()) is None


def test_gate_allows_when_clear() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0, market_open=True)
    assert gate.allowed and gate.reason == "ok"


def test_gate_blocks_kill_switch() -> None:
    gate = evaluate_bet_gate(_config(enabled=False), NOW, None, 0, 0.0, market_open=True)
    assert not gate.allowed and gate.reason == "kill_switch_off"


def test_gate_blocks_market_closed() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0, market_open=False)
    assert not gate.allowed and gate.reason == "market_closed"


def test_gate_blocks_within_cadence() -> None:
    last = NOW - dt.timedelta(seconds=100)
    gate = evaluate_bet_gate(_config(), NOW, last, 0, 0.0, market_open=True)
    assert not gate.allowed and gate.reason == "within_cadence"


def test_gate_blocks_max_concurrent() -> None:
    gate = evaluate_bet_gate(_config(max_concurrent=3), NOW, None, 3, 0.0, market_open=True)
    assert not gate.allowed and gate.reason == "max_concurrent"


def test_gate_blocks_max_total_notional() -> None:
    gate = evaluate_bet_gate(
        _config(max_total_notional_usd=200.0, notional_usd=50.0), NOW, None, 1, 180.0, market_open=True
    )
    assert not gate.allowed and gate.reason == "max_total_notional"


def test_order_filled_qty() -> None:
    assert order_filled_qty(FakeOrder("b", "c", 190.0, 0.2631)) == pytest.approx(0.2631)
    assert order_filled_qty(FakeOrder("b", "c", None, None)) is None
    assert order_filled_qty(FakeOrder("b", "c", 190.0, 0.0)) is None


@dataclass
class FakeOrder:
    id: str
    client_order_id: str
    filled_avg_price: float | None
    filled_qty: float | None = None
    status: str = "filled"
    filled_at: dt.datetime | None = None


@dataclass
class FakeClock:
    is_open: bool = True


@dataclass
class FakeTradingClient:
    """Captures submitted orders; returns canned fills. No network, places nothing real. A NOTIONAL buy
    fills a fractional ``filled_qty`` = notional / fill_price (mirroring Alpaca); a ``qty`` order fills
    exactly that quantity."""

    fill_price: float = 190.0
    submitted: list[FakeOrder] = field(default_factory=list)
    _by_coid: dict[str, FakeOrder] = field(default_factory=dict)

    def get_clock(self) -> FakeClock:
        return FakeClock(is_open=True)

    def submit_order(self, request: object) -> FakeOrder:
        coid = request.client_order_id  # type: ignore[attr-defined]
        notional = request.notional  # type: ignore[attr-defined]
        qty = request.qty  # type: ignore[attr-defined]
        filled_qty = float(notional) / self.fill_price if notional is not None else float(qty)
        order = FakeOrder(
            id=f"broker-{len(self.submitted)}", client_order_id=coid,
            filled_avg_price=self.fill_price, filled_qty=filled_qty, filled_at=NOW,
        )
        self.submitted.append(order)
        self._by_coid[coid] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> FakeOrder:
        return self._by_coid[client_order_id]


class InMemoryStore:
    """In-memory stand-in for the reversion BetStore — same surface the strategy uses, no DB."""

    def __init__(self) -> None:
        self._bets: dict[str, dict[str, object]] = {}

    def record_open(
        self, symbol: str, side: str, entry_notional: float, entry_order_id: str,
        entry_ts: dt.datetime, hold_until: dt.datetime, signal: float,
    ) -> int:
        if entry_order_id not in self._bets:
            self._bets[entry_order_id] = {
                "id": len(self._bets) + 1, "symbol": symbol, "side": side,
                "entry_notional": entry_notional, "qty": None, "entry_order_id": entry_order_id,
                "entry_ts": entry_ts, "entry_price": None, "signal": signal, "hold_until": hold_until,
                "exit_order_id": None, "status": "open",
            }
        return int(self._bets[entry_order_id]["id"])  # type: ignore[arg-type]

    def mark_filled(self, entry_order_id: str, entry_price: float, qty: float) -> None:
        bet = self._bets[entry_order_id]
        bet["entry_price"], bet["qty"], bet["status"] = entry_price, qty, "filled"

    def mark_closing(self, entry_order_id: str, exit_order_id: str) -> None:
        bet = self._bets[entry_order_id]
        bet["exit_order_id"], bet["status"] = exit_order_id, "closing"

    def record_close(
        self, entry_order_id: str, exit_ts: dt.datetime, exit_price: float, realized_pnl: float
    ) -> None:
        bet = self._bets[entry_order_id]
        bet["exit_ts"], bet["exit_price"], bet["realized_pnl"], bet["status"] = (
            exit_ts, exit_price, realized_pnl, "closed"
        )

    def list_open(self) -> list[dict[str, object]]:
        return [b for b in self._bets.values() if b["status"] in ("open", "filled", "closing")]

    def count_open(self) -> int:
        return len(self.list_open())

    def open_notional(self) -> float:
        total = 0.0
        for bet in self._bets.values():
            if bet["status"] not in ("open", "filled", "closing"):
                continue
            if bet["entry_price"] is not None and bet["qty"] is not None:
                total += float(bet["qty"]) * float(bet["entry_price"])  # type: ignore[arg-type]
            else:
                total += float(bet["entry_notional"])  # type: ignore[arg-type]
        return total


def _strategy(
    config: ReversionConfig, trading: FakeTradingClient, store: InMemoryStore
) -> ReversionStrategy:
    strategy = ReversionStrategy.__new__(ReversionStrategy)
    strategy._config = config  # type: ignore[attr-defined]
    strategy._consumer = None  # type: ignore[attr-defined]
    strategy._trading = trading  # type: ignore[attr-defined]
    strategy._store = store  # type: ignore[attr-defined]
    strategy._model = VwapReversionModel(window_m=WINDOW_M, sensitivity=400.0)  # type: ignore[attr-defined]
    strategy._latest_by_symbol = {}  # type: ignore[attr-defined]
    strategy._last_bet_ts = None  # type: ignore[attr-defined]
    return strategy


def test_place_then_manage_finalizes_bet() -> None:
    config = _config(hold_sec=0)  # hold expires immediately -> closes on next manage
    trading = FakeTradingClient(fill_price=190.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._latest_by_symbol = {"AAPL": _vector("AAPL", -0.006)}  # type: ignore[attr-defined]

    strategy.maybe_place_bet()
    assert store.count_open() == 1
    entry = trading.submitted[0]
    assert entry.client_order_id.startswith("rev_AAPL_")
    expected_qty = 50.0 / 190.0  # NOTIONAL buy -> fractional shares
    assert entry.filled_qty == pytest.approx(expected_qty)

    trading.fill_price = 191.5
    strategy.manage_open_bets()
    assert store.count_open() == 0
    closed = next(iter(store._bets.values()))
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] == pytest.approx((191.5 - 190.0) * expected_qty)
    assert len(trading.submitted) == 2  # BUY + SELL


def test_no_bet_when_nothing_clears_threshold() -> None:
    config = _config()
    trading = FakeTradingClient()
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._latest_by_symbol = {"AAPL": _vector("AAPL", +0.005)}  # type: ignore[attr-defined] # ABOVE VWAP
    strategy.maybe_place_bet()
    assert trading.submitted == [] and store.count_open() == 0


def test_kill_switch_places_no_order() -> None:
    config = _config(enabled=False)
    trading = FakeTradingClient()
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._latest_by_symbol = {"AAPL": _vector("AAPL", -0.006)}  # type: ignore[attr-defined]
    strategy.maybe_place_bet()
    assert trading.submitted == [] and store.count_open() == 0


def test_does_not_stack_on_held_symbol() -> None:
    config = _config(max_concurrent=10)
    trading = FakeTradingClient()
    store = InMemoryStore()
    store.record_open("AAPL", "buy", 50.0, "rev_pre", NOW, NOW + dt.timedelta(seconds=1800), 0.9)
    strategy = _strategy(config, trading, store)
    # AAPL is the most stretched but already held; MSFT mild -> below threshold -> no new bet.
    strategy._latest_by_symbol = {  # type: ignore[attr-defined]
        "AAPL": _vector("AAPL", -0.006), "MSFT": _vector("MSFT", -0.0001),
    }
    strategy.maybe_place_bet()
    assert trading.submitted == []
    assert store.count_open() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
