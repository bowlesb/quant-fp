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
from strategies.smoke.strategy import (
    SmokeConfig,
    SmokeStrategy,
    compute_qty,
    evaluate_bet_gate,
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


def test_gate_blocks_when_no_symbol_seen() -> None:
    gate = evaluate_bet_gate(_config(), NOW, None, 0, 0.0, market_open=True, last_symbol=None)
    assert not gate.allowed
    assert gate.reason == "no_symbol_seen"


def test_compute_qty_whole_shares() -> None:
    assert compute_qty(50.0, 190.0) == 1   # floor(50/190)=0 -> clamped to 1
    assert compute_qty(500.0, 100.0) == 5
    assert compute_qty(50.0, 10.0) == 5


def test_compute_qty_rejects_bad_price() -> None:
    with pytest.raises(ValueError):
        compute_qty(50.0, 0.0)


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
    status: str = "filled"
    filled_at: dt.datetime | None = None


@dataclass
class FakeClock:
    is_open: bool = True


@dataclass
class FakeTradingClient:
    """Captures submitted orders; returns canned fills. No network, places nothing real."""

    fill_price: float = 190.0
    submitted: list[FakeOrder] = field(default_factory=list)
    _by_coid: dict[str, FakeOrder] = field(default_factory=dict)

    def get_clock(self) -> FakeClock:
        return FakeClock(is_open=True)

    def submit_order(self, request: object) -> FakeOrder:
        coid = request.client_order_id  # type: ignore[attr-defined]
        order = FakeOrder(
            id=f"broker-{len(self.submitted)}",
            client_order_id=coid,
            filled_avg_price=self.fill_price,
            filled_at=NOW,
        )
        self.submitted.append(order)
        self._by_coid[coid] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> FakeOrder:
        return self._by_coid[client_order_id]


@dataclass
class FakeTrade:
    price: float


@dataclass
class FakeDataClient:
    price: float = 190.0

    def get_stock_latest_trade(self, request: object) -> dict[str, FakeTrade]:
        symbol = request.symbol_or_symbols  # type: ignore[attr-defined]
        return {symbol: FakeTrade(price=self.price)}


class InMemoryStore:
    """In-memory stand-in for BetStore — same surface the strategy uses, no DB."""

    def __init__(self) -> None:
        self._bets: dict[str, dict[str, object]] = {}

    def record_open(
        self, symbol: str, side: str, qty: float, entry_order_id: str,
        entry_ts: dt.datetime, hold_until: dt.datetime,
    ) -> int:
        if entry_order_id not in self._bets:
            self._bets[entry_order_id] = {
                "id": len(self._bets) + 1, "symbol": symbol, "side": side, "qty": qty,
                "entry_order_id": entry_order_id, "entry_ts": entry_ts, "entry_price": None,
                "hold_until": hold_until, "exit_order_id": None, "status": "open",
            }
        return int(self._bets[entry_order_id]["id"])  # type: ignore[arg-type]

    def mark_filled(self, entry_order_id: str, entry_price: float) -> None:
        bet = self._bets[entry_order_id]
        bet["entry_price"] = entry_price
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
        return sum(
            float(b["qty"]) * float(b["entry_price"])  # type: ignore[arg-type]
            for b in self._bets.values()
            if b["status"] in ("filled", "closing") and b["entry_price"] is not None
        )


def _strategy(
    config: SmokeConfig, trading: FakeTradingClient, data: FakeDataClient, store: InMemoryStore
) -> SmokeStrategy:
    consumer = BusConsumer(config.symbols, url=URL) if _redis_up() else None
    strategy = SmokeStrategy.__new__(SmokeStrategy)
    strategy._config = config  # type: ignore[attr-defined]
    strategy._consumer = consumer  # type: ignore[attr-defined]
    strategy._trading = trading  # type: ignore[attr-defined]
    strategy._data = data  # type: ignore[attr-defined]
    strategy._store = store  # type: ignore[attr-defined]
    strategy._last_symbol = "AAPL"  # type: ignore[attr-defined]
    strategy._last_bet_ts = None  # type: ignore[attr-defined]
    return strategy


def test_place_then_manage_finalizes_bet() -> None:
    config = _config(hold_sec=0)  # hold expires immediately -> closes on next manage
    trading = FakeTradingClient(fill_price=190.0)
    data = FakeDataClient(price=190.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, data, store)

    strategy.maybe_place_bet()
    assert store.count_open() == 1
    assert len(trading.submitted) == 1  # the BUY entry
    entry = trading.submitted[0]
    assert entry.client_order_id.startswith("smoke_AAPL_")

    trading.fill_price = 191.5  # the close fills at a different price
    strategy.manage_open_bets()

    assert store.count_open() == 0  # finalized
    closed = next(iter(store._bets.values()))
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] == pytest.approx((191.5 - 190.0) * 1)
    assert len(trading.submitted) == 2  # BUY + SELL


def test_kill_switch_places_no_order() -> None:
    config = _config(enabled=False)
    trading = FakeTradingClient()
    store = InMemoryStore()
    strategy = _strategy(config, trading, FakeDataClient(), store)
    strategy.maybe_place_bet()
    assert store.count_open() == 0
    assert trading.submitted == []


def test_max_concurrent_blocks_new_bet() -> None:
    config = _config(max_concurrent=1)
    trading = FakeTradingClient()
    store = InMemoryStore()
    store.record_open("MSFT", "buy", 1, "smoke_pre", NOW, NOW + dt.timedelta(seconds=900))
    strategy = _strategy(config, trading, FakeDataClient(), store)
    strategy.maybe_place_bet()
    assert trading.submitted == []  # already at cap
    assert store.count_open() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
