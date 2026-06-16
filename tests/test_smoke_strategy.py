"""Smoke-strategy logic tests: the bet/cap/kill-switch GATE (pure), the qty math, the bus SAMPLING
round-trip (real BusPublisher -> BusConsumer), and a full place->manage->finalize flow against a FAKE
broker + in-memory store (no real orders placed).

The gate + qty tests are network-free. The sampling + flow tests use the quant-redis bus and skip
cleanly when it is unreachable.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from dataclasses import dataclass, field

import pytest
import redis

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import BusPublisher
from quantlib.bus.schema import default_schema
from strategies.lib.model import MockMLModel
from strategies.smoke.strategy import (
    SmokeConfig,
    SmokeStrategy,
    evaluate_bet_gate,
    order_filled_qty,
    sample_features,
)

URL = os.environ.get("BUS_REDIS_URL", "redis://quant-redis:6379/0")
NOW = dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def _redis_up() -> bool:
    try:
        redis.Redis.from_url(URL).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


def _config(**overrides: object) -> SmokeConfig:
    base: dict[str, object] = {
        "symbols": ["AAPL"],
        "bet_interval_sec": 300,
        "notional_usd": 50.0,
        "hold_sec": 900,
        "max_concurrent": 3,
        "max_total_notional_usd": 200.0,
        "enabled": True,
        "loop_block_ms": 100,
        "use_model": False,
        "model_threshold": 0.5,
    }
    base.update(overrides)
    return SmokeConfig(**base)  # type: ignore[arg-type]


def test_gate_allows_when_clear() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0, market_open=True, last_symbol="AAPL")
    assert gate.allowed
    assert gate.reason == "ok"


def test_gate_blocks_when_kill_switch_off() -> None:
    gate = evaluate_bet_gate(
        _config(enabled=False), NOW, None, 0, 0.0, market_open=True, last_symbol="AAPL"
    )
    assert not gate.allowed
    assert gate.reason == "kill_switch_off"


def test_gate_blocks_when_market_closed() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0, market_open=False, last_symbol="AAPL")
    assert not gate.allowed
    assert gate.reason == "market_closed"


def test_gate_blocks_within_cadence() -> None:
    last = NOW - dt.timedelta(seconds=100)  # < 300s interval
    gate = evaluate_bet_gate(_config(), NOW, last, 0, 0.0, market_open=True, last_symbol="AAPL")
    assert not gate.allowed
    assert gate.reason == "within_cadence"


def test_gate_blocks_at_max_concurrent() -> None:
    gate = evaluate_bet_gate(
        _config(max_concurrent=3), NOW, None, 3, 0.0, market_open=True, last_symbol="AAPL"
    )
    assert not gate.allowed
    assert gate.reason == "max_concurrent"


def test_gate_blocks_at_max_total_notional() -> None:
    # open 180 + prospective 50 = 230 > 200 cap
    gate = evaluate_bet_gate(
        _config(max_total_notional_usd=200.0, notional_usd=50.0),
        NOW, None, 1, 180.0, market_open=True, last_symbol="AAPL",
    )
    assert not gate.allowed
    assert gate.reason == "max_total_notional"


def test_gate_bounds_actual_notional_not_share_price() -> None:
    # Regression for the AMD ~$547 bug: with notional sizing, three $50 open bets = $150 ACTUAL
    # exposure (not 3 whole AMD shares ~ $1641). A 4th $50 bet -> 200 == cap, so it is allowed...
    gate_ok = evaluate_bet_gate(
        _config(max_total_notional_usd=200.0, notional_usd=50.0, max_concurrent=10),
        NOW, None, 3, 150.0, market_open=True, last_symbol="AMD",
    )
    assert gate_ok.allowed
    # ...but a 5th would push 200 + 50 = 250 > 200 and is skipped.
    gate_blocked = evaluate_bet_gate(
        _config(max_total_notional_usd=200.0, notional_usd=50.0, max_concurrent=10),
        NOW, None, 4, 200.0, market_open=True, last_symbol="AMD",
    )
    assert not gate_blocked.allowed
    assert gate_blocked.reason == "max_total_notional"


def test_gate_blocks_when_no_symbol_seen() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0, market_open=True, last_symbol=None)
    assert not gate.allowed
    assert gate.reason == "no_symbol_seen"


def test_order_filled_qty_reads_fractional_fill() -> None:
    filled = FakeOrder("b", "c", filled_avg_price=190.0, filled_qty=0.263157)
    assert order_filled_qty(filled) == pytest.approx(0.263157)  # ~$50 / $190 share


def test_order_filled_qty_none_when_unfilled() -> None:
    assert order_filled_qty(FakeOrder("b", "c", filled_avg_price=None, filled_qty=None)) is None
    assert order_filled_qty(FakeOrder("b", "c", filled_avg_price=190.0, filled_qty=0.0)) is None


def test_sample_features_reads_real_features() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    schema = default_schema()
    prefix = f"smoketest_{uuid.uuid4().hex[:8]}"
    publisher = BusPublisher(url=URL, schema=schema, prefix=prefix)
    publisher.publish("AAPL", NOW, {"ret_1m": 0.0042, "volume_zscore_5m": 1.25})
    consumer = BusConsumer(["AAPL"], url=URL, schema=schema, prefix=prefix, start="0")
    vectors = consumer.poll(block_ms=500, count=10)
    publisher.close()
    consumer.close()
    assert vectors, "expected the published AAPL vector"
    samples = sample_features(vectors[-1])
    assert samples["ret_1m"] == pytest.approx(0.0042)
    assert samples["volume_zscore_5m"] == pytest.approx(1.25)


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
    """Captures submitted orders; returns canned fills. No network, places nothing real.

    A NOTIONAL buy (``request.notional`` set, ``qty`` None) fills a fractional ``filled_qty`` =
    notional / fill_price, mirroring Alpaca; a ``qty`` order fills exactly that quantity.
    """

    fill_price: float = 190.0
    submitted: list[FakeOrder] = field(default_factory=list)
    _by_coid: dict[str, FakeOrder] = field(default_factory=dict)

    def get_clock(self) -> FakeClock:
        return FakeClock(is_open=True)

    def submit_order(self, request: object) -> FakeOrder:
        coid = request.client_order_id  # type: ignore[attr-defined]
        notional = request.notional  # type: ignore[attr-defined]
        qty = request.qty  # type: ignore[attr-defined]
        if notional is not None:
            filled_qty = float(notional) / self.fill_price
        else:
            filled_qty = float(qty)
        order = FakeOrder(
            id=f"broker-{len(self.submitted)}",
            client_order_id=coid,
            filled_avg_price=self.fill_price,
            filled_qty=filled_qty,
            filled_at=NOW,
        )
        self.submitted.append(order)
        self._by_coid[coid] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> FakeOrder:
        return self._by_coid[client_order_id]


class InMemoryStore:
    """In-memory stand-in for BetStore — same surface the strategy uses, no DB.

    Sizing is by NOTIONAL: ``record_open`` takes ``entry_notional`` and ``qty`` is NULL until
    ``mark_filled`` records the actual fractional fill. ``open_notional`` bounds ACTUAL exposure:
    filled bets at ``qty * entry_price``, unfilled bets at their ``entry_notional``.
    """

    def __init__(self) -> None:
        self._bets: dict[str, dict[str, object]] = {}

    def record_open(
        self, symbol: str, side: str, entry_notional: float, entry_order_id: str,
        entry_ts: dt.datetime, hold_until: dt.datetime,
    ) -> int:
        if entry_order_id not in self._bets:
            self._bets[entry_order_id] = {
                "id": len(self._bets) + 1, "symbol": symbol, "side": side,
                "entry_notional": entry_notional, "qty": None,
                "entry_order_id": entry_order_id, "entry_ts": entry_ts, "entry_price": None,
                "hold_until": hold_until, "exit_order_id": None, "status": "open",
            }
        return int(self._bets[entry_order_id]["id"])  # type: ignore[arg-type]

    def mark_filled(self, entry_order_id: str, entry_price: float, qty: float) -> None:
        bet = self._bets[entry_order_id]
        bet["entry_price"] = entry_price
        bet["qty"] = qty
        bet["status"] = "filled"

    def mark_closing(self, entry_order_id: str, exit_order_id: str) -> None:
        bet = self._bets[entry_order_id]
        bet["exit_order_id"] = exit_order_id
        bet["status"] = "closing"

    def record_close(
        self, entry_order_id: str, exit_ts: dt.datetime, exit_price: float, realized_pnl: float
    ) -> None:
        bet = self._bets[entry_order_id]
        bet["exit_ts"] = exit_ts
        bet["exit_price"] = exit_price
        bet["realized_pnl"] = realized_pnl
        bet["status"] = "closed"

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
    config: SmokeConfig, trading: FakeTradingClient, store: InMemoryStore
) -> SmokeStrategy:
    consumer = BusConsumer(config.symbols, url=URL) if _redis_up() else None
    strategy = SmokeStrategy.__new__(SmokeStrategy)
    strategy._config = config  # type: ignore[attr-defined]
    strategy._consumer = consumer  # type: ignore[attr-defined]
    strategy._trading = trading  # type: ignore[attr-defined]
    strategy._store = store  # type: ignore[attr-defined]
    strategy._model = MockMLModel()  # type: ignore[attr-defined]
    strategy._last_symbol = "AAPL"  # type: ignore[attr-defined]
    strategy._last_vector = None  # type: ignore[attr-defined]
    strategy._last_bet_ts = None  # type: ignore[attr-defined]
    return strategy


def test_place_then_manage_finalizes_bet() -> None:
    config = _config(hold_sec=0)  # hold expires immediately -> closes on next manage
    trading = FakeTradingClient(fill_price=190.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)

    strategy.maybe_place_bet()
    assert store.count_open() == 1
    assert len(trading.submitted) == 1  # the BUY entry
    entry = trading.submitted[0]
    assert entry.client_order_id.startswith("smoke_AAPL_")
    # NOTIONAL buy: $50 / $190 share = ~0.263 fractional shares, not a whole share
    expected_qty = 50.0 / 190.0
    assert entry.filled_qty == pytest.approx(expected_qty)

    trading.fill_price = 191.5  # the close fills at a different price
    strategy.manage_open_bets()

    assert store.count_open() == 0  # finalized
    closed = next(iter(store._bets.values()))
    assert closed["status"] == "closed"
    assert closed["qty"] == pytest.approx(expected_qty)
    assert closed["realized_pnl"] == pytest.approx((191.5 - 190.0) * expected_qty)
    assert len(trading.submitted) == 2  # BUY + SELL
    sell = trading.submitted[1]
    assert sell.filled_qty == pytest.approx(expected_qty)  # sells exactly the filled qty


def test_high_priced_symbol_sized_to_notional_not_whole_share() -> None:
    # Regression for the AMD ~$547 whole-share bug: a $50 bet on a $547 stock must cost ~$50.
    config = _config(notional_usd=50.0)
    trading = FakeTradingClient(fill_price=547.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._last_symbol = "AMD"  # type: ignore[attr-defined]

    strategy.maybe_place_bet()

    entry = trading.submitted[0]
    actual_cost = entry.filled_qty * entry.filled_avg_price  # type: ignore[operator]
    assert actual_cost == pytest.approx(50.0)  # ~$50, NOT ~$547
    assert entry.filled_qty < 1.0  # fractional, not a whole share


def test_kill_switch_places_no_order() -> None:
    config = _config(enabled=False)
    trading = FakeTradingClient()
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy.maybe_place_bet()
    assert store.count_open() == 0
    assert trading.submitted == []


def test_max_concurrent_blocks_new_bet() -> None:
    config = _config(max_concurrent=1)
    trading = FakeTradingClient()
    store = InMemoryStore()
    store.record_open("MSFT", "buy", 50.0, "smoke_pre", NOW, NOW + dt.timedelta(seconds=900))
    strategy = _strategy(config, trading, store)
    strategy.maybe_place_bet()
    assert trading.submitted == []  # already at cap
    assert store.count_open() == 1


def test_total_notional_cap_skips_high_priced_bet() -> None:
    # Three open $50 bets = $150 ACTUAL exposure regardless of share price; a 4th would exceed $150
    # cap (150 + 50 = 200 > 150) and must be skipped — proving the cap bounds real dollars.
    config = _config(max_total_notional_usd=150.0, notional_usd=50.0, max_concurrent=10)
    trading = FakeTradingClient(fill_price=547.0)
    store = InMemoryStore()
    for index in range(3):
        coid = f"smoke_pre_{index}"
        store.record_open("AMD", "buy", 50.0, coid, NOW, NOW + dt.timedelta(seconds=900))
        store.mark_filled(coid, 547.0, 50.0 / 547.0)  # ~0.091 shares each, ~$50 actual
    assert store.open_notional() == pytest.approx(150.0)  # NOT 3 whole AMD shares (~$1641)
    strategy = _strategy(config, trading, store)
    strategy._last_symbol = "AMD"  # type: ignore[attr-defined]

    strategy.maybe_place_bet()

    assert trading.submitted == []  # 4th bet would push to $200 > $150 cap -> skipped
    assert store.count_open() == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
