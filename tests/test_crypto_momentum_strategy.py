"""Crypto-momentum-strategy logic tests: the CryptoMomentumModel (sign, monotonicity, NaN-safety,
determinism), the slashless<->slash symbol mapping, the pure bet GATE (no market-hours gate — crypto is
24/7), the pure candidate selection (rank + threshold + exclusion), and a full place -> manage -> finalize
flow against a FAKE broker + in-memory store (no real orders placed).

All tests are network-free: vectors are built directly from ``default_schema()`` (no bus round-trip
needed), so the model + selection logic is exercised deterministically. The flow test asserts the crypto
specifics: the coid + ledger keep the SLASHLESS symbol (BTCUSD) while the Alpaca order carries the SLASH
pair (BTC/USD), and the order uses TimeInForce.GTC (crypto rejects DAY).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderStatus, TimeInForce

from quantlib.bus.schema import default_schema
from quantlib.bus.vector import FeatureVector
from strategies.crypto_momentum.strategy import (
    CryptoMomentumConfig,
    CryptoMomentumStrategy,
    evaluate_bet_gate,
    order_filled_qty,
    select_candidate,
    to_alpaca_symbol,
)
from strategies.lib.crypto_momentum_model import CryptoMomentumModel
from strategies.lib.stale_entry import StaleEntryTracker

NOT_FOUND_BODY = '{"code":40410000,"message":"order not found for: %s"}'
NOW = dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)
SCHEMA = default_schema()
WINDOW_M = 5
FEATURE = f"ret_{WINDOW_M}m"


def _vector(symbol: str, ret: float) -> FeatureVector:
    """A decoded vector for ``symbol`` whose ``ret_{WINDOW_M}m`` cell = ``ret`` (all other cells NaN — the
    model reads only the one feature it depends on)."""
    array = np.full(SCHEMA.n_features, np.nan, dtype=np.float64)
    array[SCHEMA.offset(FEATURE)] = ret
    return FeatureVector(SCHEMA, symbol, NOW, array, SCHEMA.fingerprint)


def _config(**overrides: object) -> CryptoMomentumConfig:
    base: dict[str, object] = {
        "symbols": ["BTCUSD"],
        "bet_interval_sec": 300,
        "notional_usd": 50.0,
        "hold_sec": 1800,
        "max_concurrent": 3,
        "max_total_notional_usd": 200.0,
        "enabled": True,
        "loop_block_ms": 100,
        "ret_window_m": WINDOW_M,
        "sensitivity": 200.0,
        "threshold": 0.55,
    }
    base.update(overrides)
    return CryptoMomentumConfig(**base)  # type: ignore[arg-type]


def test_to_alpaca_symbol_maps_slash() -> None:
    assert to_alpaca_symbol("BTCUSD") == "BTC/USD"
    assert to_alpaca_symbol("ETHUSD") == "ETH/USD"
    assert to_alpaca_symbol("DOGEUSD") == "DOGE/USD"
    # a non-USD / already-mapped symbol is returned unchanged (defensive)
    assert to_alpaca_symbol("USD") == "USD"


def test_model_positive_momentum_is_bullish() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    assert model.predict(_vector("BTCUSD", 0.005)).probability > 0.5  # up momentum -> long
    assert model.predict(_vector("BTCUSD", -0.005)).probability < 0.5  # down -> no long
    assert model.predict(_vector("BTCUSD", 0.0)).probability == pytest.approx(0.5)


def test_model_more_momentum_is_more_bullish() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    mild = model.predict(_vector("BTCUSD", 0.001)).probability
    strong = model.predict(_vector("BTCUSD", 0.005)).probability
    assert strong > mild > 0.5  # monotone in the trailing return


def test_model_nan_feature_is_no_signal() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    assert model.predict(_vector("BTCUSD", float("nan"))).probability == pytest.approx(0.5)


def test_model_is_deterministic() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    a = model.predict(_vector("BTCUSD", 0.003)).probability
    b = model.predict(_vector("BTCUSD", 0.003)).probability
    assert a == b


def test_select_candidate_picks_strongest_momentum() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    latest = {
        "BTCUSD": _vector("BTCUSD", 0.001),  # mild up
        "ETHUSD": _vector("ETHUSD", 0.006),  # strongest up -> should win
        "SOLUSD": _vector("SOLUSD", -0.004),  # down -> not a long
    }
    candidate = select_candidate(model, latest, threshold=0.52, excluded=set())
    assert candidate is not None
    assert candidate.symbol == "ETHUSD"
    assert candidate.probability > 0.52


def test_select_candidate_respects_threshold() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    latest = {"BTCUSD": _vector("BTCUSD", 0.00001)}  # barely up -> p just above 0.5
    assert select_candidate(model, latest, threshold=0.60, excluded=set()) is None


def test_select_candidate_excludes_held_symbols() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    latest = {"ETHUSD": _vector("ETHUSD", 0.006)}
    assert select_candidate(model, latest, threshold=0.52, excluded={"ETHUSD"}) is None


def test_select_candidate_skips_nan() -> None:
    model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)
    latest = {"BTCUSD": _vector("BTCUSD", float("nan"))}
    assert select_candidate(model, latest, threshold=0.52, excluded=set()) is None


def test_gate_allows_when_clear() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0)
    assert gate.allowed and gate.reason == "ok"


def test_gate_blocks_kill_switch() -> None:
    gate = evaluate_bet_gate(_config(enabled=False), NOW, None, 0, 0.0)
    assert not gate.allowed and gate.reason == "kill_switch_off"


def test_gate_blocks_within_cadence() -> None:
    last = NOW - dt.timedelta(seconds=100)
    gate = evaluate_bet_gate(_config(), NOW, last, 0, 0.0)
    assert not gate.allowed and gate.reason == "within_cadence"


def test_gate_blocks_max_concurrent() -> None:
    gate = evaluate_bet_gate(_config(max_concurrent=3), NOW, None, 3, 0.0)
    assert not gate.allowed and gate.reason == "max_concurrent"


def test_gate_blocks_max_total_notional() -> None:
    gate = evaluate_bet_gate(_config(max_total_notional_usd=200.0, notional_usd=50.0), NOW, None, 1, 180.0)
    assert not gate.allowed and gate.reason == "max_total_notional"


def test_gate_has_no_market_hours_block() -> None:
    """Crypto is 24/7: the gate never blocks on market hours (there is no such parameter)."""
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0)
    assert gate.allowed  # would have been blocked by a market-closed check on the equity strategies


def test_order_filled_qty() -> None:
    assert order_filled_qty(FakeOrder("b", "c", 65000.0, 0.000769)) == pytest.approx(0.000769)
    assert order_filled_qty(FakeOrder("b", "c", None, None)) is None
    assert order_filled_qty(FakeOrder("b", "c", 65000.0, 0.0)) is None


@dataclass
class FakeOrder:
    id: str
    client_order_id: str
    filled_avg_price: float | None
    filled_qty: float | None = None
    status: object = None
    filled_at: dt.datetime | None = None
    qty: float | None = None
    side: object = None
    symbol: str | None = None
    time_in_force: object = None


@dataclass
class FakeTradingClient:
    """Captures submitted orders; returns canned fills. No network, places nothing real. A NOTIONAL buy
    fills a fractional ``filled_qty`` = notional / fill_price (mirroring Alpaca crypto); a ``qty`` order
    fills exactly that quantity. Records the request symbol + TIF so the crypto specifics are assertable."""

    fill_price: float = 65000.0
    submitted: list[FakeOrder] = field(default_factory=list)
    _by_coid: dict[str, FakeOrder] = field(default_factory=dict)

    def submit_order(self, request: object) -> FakeOrder:
        coid = request.client_order_id  # type: ignore[attr-defined]
        notional = getattr(request, "notional", None)
        qty = getattr(request, "qty", None)
        filled_qty = float(notional) / self.fill_price if notional is not None else float(qty)
        order = FakeOrder(
            id=f"broker-{len(self.submitted)}",
            client_order_id=coid,
            filled_avg_price=self.fill_price,
            filled_qty=filled_qty,
            filled_at=NOW,
            status=OrderStatus.FILLED,
            qty=filled_qty,
            symbol=request.symbol,  # type: ignore[attr-defined]
            time_in_force=request.time_in_force,  # type: ignore[attr-defined]
        )
        self.submitted.append(order)
        self._by_coid[coid] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> FakeOrder:
        if client_order_id not in self._by_coid:
            raise APIError(NOT_FOUND_BODY % client_order_id)
        return self._by_coid[client_order_id]


class InMemoryStore:
    """In-memory stand-in for the crypto-momentum BetStore — same surface the strategy uses, no DB."""

    def __init__(self) -> None:
        self._bets: dict[str, dict[str, object]] = {}

    def record_open(
        self,
        symbol: str,
        side: str,
        entry_notional: float,
        entry_order_id: str,
        entry_ts: dt.datetime,
        hold_until: dt.datetime,
        signal: float,
    ) -> int:
        if entry_order_id not in self._bets:
            self._bets[entry_order_id] = {
                "id": len(self._bets) + 1,
                "symbol": symbol,
                "side": side,
                "entry_notional": entry_notional,
                "qty": None,
                "entry_order_id": entry_order_id,
                "entry_ts": entry_ts,
                "entry_price": None,
                "signal": signal,
                "hold_until": hold_until,
                "exit_order_id": None,
                "status": "open",
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
            exit_ts,
            exit_price,
            realized_pnl,
            "closed",
        )

    def mark_abandoned(self, entry_order_id: str) -> None:
        bet = self._bets[entry_order_id]
        if bet["status"] in ("open", "filled", "closing"):
            bet["status"], bet["realized_pnl"] = "closed", 0

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
    config: CryptoMomentumConfig, trading: FakeTradingClient, store: InMemoryStore
) -> CryptoMomentumStrategy:
    strategy = CryptoMomentumStrategy.__new__(CryptoMomentumStrategy)
    strategy._config = config  # type: ignore[attr-defined]
    strategy._consumer = None  # type: ignore[attr-defined]
    strategy._trading = trading  # type: ignore[attr-defined]
    strategy._store = store  # type: ignore[attr-defined]
    strategy._state = None  # type: ignore[attr-defined]
    strategy._state_store = None  # type: ignore[attr-defined]
    strategy._pending_close_logged = set()  # type: ignore[attr-defined]
    strategy._stale_entries = StaleEntryTracker()  # type: ignore[attr-defined]
    strategy._model = CryptoMomentumModel(window_m=WINDOW_M, sensitivity=200.0)  # type: ignore[attr-defined]
    strategy._latest_by_symbol = {}  # type: ignore[attr-defined]
    strategy._last_bet_ts = None  # type: ignore[attr-defined]
    return strategy


def test_place_then_manage_finalizes_bet() -> None:
    config = _config(hold_sec=0)  # hold expires immediately -> closes on next manage
    trading = FakeTradingClient(fill_price=65000.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._latest_by_symbol = {"BTCUSD": _vector("BTCUSD", 0.006)}  # type: ignore[attr-defined]

    strategy.maybe_place_bet()
    assert store.count_open() == 1
    entry = trading.submitted[0]
    # the coid keeps the SLASHLESS symbol (so make_client_order_id parses), the order carries the SLASH pair
    assert entry.client_order_id.startswith("cryptomomentum-")
    assert entry.client_order_id.endswith("-BTCUSD-buy")
    assert entry.symbol == "BTC/USD"
    assert entry.time_in_force == TimeInForce.GTC  # crypto requires GTC, never DAY
    expected_qty = 50.0 / 65000.0  # NOTIONAL buy -> fractional coins
    assert entry.filled_qty == pytest.approx(expected_qty)
    # the ledger keeps the slashless symbol
    assert next(iter(store._bets.values()))["symbol"] == "BTCUSD"

    trading.fill_price = 65500.0
    strategy.manage_open_bets()
    assert store.count_open() == 0
    closed = next(iter(store._bets.values()))
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] == pytest.approx((65500.0 - 65000.0) * expected_qty)
    assert len(trading.submitted) == 2  # BUY + SELL
    exit_order = trading.submitted[1]
    assert exit_order.symbol == "BTC/USD"
    assert exit_order.time_in_force == TimeInForce.GTC


def test_no_bet_when_nothing_clears_threshold() -> None:
    config = _config()
    trading = FakeTradingClient()
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._latest_by_symbol = {"BTCUSD": _vector("BTCUSD", -0.005)}  # type: ignore[attr-defined] # down momentum
    strategy.maybe_place_bet()
    assert trading.submitted == [] and store.count_open() == 0


def test_kill_switch_places_no_order() -> None:
    config = _config(enabled=False)
    trading = FakeTradingClient()
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._latest_by_symbol = {"BTCUSD": _vector("BTCUSD", 0.006)}  # type: ignore[attr-defined]
    strategy.maybe_place_bet()
    assert trading.submitted == [] and store.count_open() == 0


def test_does_not_stack_on_held_symbol() -> None:
    config = _config(max_concurrent=10)
    trading = FakeTradingClient()
    store = InMemoryStore()
    store.record_open("ETHUSD", "buy", 50.0, "cmom_pre", NOW, NOW + dt.timedelta(seconds=1800), 0.9)
    strategy = _strategy(config, trading, store)
    # ETHUSD is the strongest but already held; BTCUSD mild -> below threshold -> no new bet.
    strategy._latest_by_symbol = {  # type: ignore[attr-defined]
        "ETHUSD": _vector("ETHUSD", 0.006),
        "BTCUSD": _vector("BTCUSD", 0.00001),
    }
    strategy.maybe_place_bet()
    assert trading.submitted == []
    assert store.count_open() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
