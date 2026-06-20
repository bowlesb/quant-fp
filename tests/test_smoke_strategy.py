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
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderStatus

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import BusPublisher
from quantlib.bus.schema import default_schema
from quantlib.strategy_core.paper_alpaca_executor import PaperAlpacaExecutor
from strategies.lib.model import MockMLModel
from strategies.lib.stale_entry import StaleEntryTracker
from strategies.smoke.strategy import (
    SmokeConfig,
    SmokeStrategy,
    evaluate_bet_gate,
    exit_coid_for,
    exit_retry_count,
    is_order_not_found,
    order_filled_qty,
    sample_features,
)

NOT_FOUND_BODY = '{"code":40410000,"message":"order not found for %s"}'
SERVER_ERROR_BODY = '{"code":50010000,"message":"internal server error occurred"}'

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
    gate = evaluate_bet_gate(_config(enabled=False), NOW, None, 0, 0.0, market_open=True, last_symbol="AAPL")
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
        NOW,
        None,
        1,
        180.0,
        market_open=True,
        last_symbol="AAPL",
    )
    assert not gate.allowed
    assert gate.reason == "max_total_notional"


def test_gate_bounds_actual_notional_not_share_price() -> None:
    # Regression for the AMD ~$547 bug: with notional sizing, three $50 open bets = $150 ACTUAL
    # exposure (not 3 whole AMD shares ~ $1641). A 4th $50 bet -> 200 == cap, so it is allowed...
    gate_ok = evaluate_bet_gate(
        _config(max_total_notional_usd=200.0, notional_usd=50.0, max_concurrent=10),
        NOW,
        None,
        3,
        150.0,
        market_open=True,
        last_symbol="AMD",
    )
    assert gate_ok.allowed
    # ...but a 5th would push 200 + 50 = 250 > 200 and is skipped.
    gate_blocked = evaluate_bet_gate(
        _config(max_total_notional_usd=200.0, notional_usd=50.0, max_concurrent=10),
        NOW,
        None,
        4,
        200.0,
        market_open=True,
        last_symbol="AMD",
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
    status: object = None  # set to an alpaca OrderStatus in submit_order (Alpaca-shaped)
    filled_at: dt.datetime | None = None
    qty: float | None = None
    side: object = None
    submitted_at: dt.datetime | None = None
    created_at: dt.datetime | None = None


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
    # Coids the broker has "lost": get_order_by_client_id raises order-not-found (40410000) for these,
    # mirroring an exit SELL whose submit never actually landed.
    not_found_coids: set[str] = field(default_factory=set)
    # Substrings of coids whose FIRST submit raises an Alpaca 5xx (then succeeds on re-submit).
    fail_submit_once: set[str] = field(default_factory=set)
    _failed_once: set[str] = field(default_factory=set)

    def get_clock(self) -> FakeClock:
        return FakeClock(is_open=True)

    def submit_order(self, request: object) -> FakeOrder:
        coid = request.client_order_id  # type: ignore[attr-defined]
        notional = request.notional  # type: ignore[attr-defined]
        qty = request.qty  # type: ignore[attr-defined]
        if coid in self.fail_submit_once and coid not in self._failed_once:
            self._failed_once.add(coid)
            raise APIError(SERVER_ERROR_BODY)
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
            status=OrderStatus.FILLED,
            qty=filled_qty,
            submitted_at=NOW,
            created_at=NOW,
        )
        self.submitted.append(order)
        self._by_coid[coid] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> FakeOrder:
        if client_order_id in self.not_found_coids or client_order_id not in self._by_coid:
            raise APIError(NOT_FOUND_BODY % client_order_id)
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
        self,
        symbol: str,
        side: str,
        entry_notional: float,
        entry_order_id: str,
        entry_ts: dt.datetime,
        hold_until: dt.datetime,
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
                "hold_until": hold_until,
                "exit_order_id": None,
                "status": "open",
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

    def update_exit_coid(self, entry_order_id: str, exit_order_id: str) -> None:
        bet = self._bets[entry_order_id]
        if bet["status"] == "closing":
            bet["exit_order_id"] = exit_order_id

    def record_close(
        self, entry_order_id: str, exit_ts: dt.datetime, exit_price: float, realized_pnl: float
    ) -> None:
        bet = self._bets[entry_order_id]
        bet["exit_ts"] = exit_ts
        bet["exit_price"] = exit_price
        bet["realized_pnl"] = realized_pnl
        bet["status"] = "closed"

    def mark_abandoned(self, entry_order_id: str) -> None:
        bet = self._bets[entry_order_id]
        if bet["status"] in ("open", "filled", "closing"):
            bet["status"] = "closed"
            bet["realized_pnl"] = 0

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


def _strategy(config: SmokeConfig, trading: FakeTradingClient, store: InMemoryStore) -> SmokeStrategy:
    consumer = BusConsumer(config.symbols, url=URL) if _redis_up() else None
    strategy = SmokeStrategy.__new__(SmokeStrategy)
    strategy._config = config  # type: ignore[attr-defined]
    strategy._consumer = consumer  # type: ignore[attr-defined]
    strategy._trading = trading  # type: ignore[attr-defined]
    strategy._store = store  # type: ignore[attr-defined]
    strategy._executor = PaperAlpacaExecutor(trading)  # type: ignore[attr-defined,arg-type]
    strategy._state = None  # type: ignore[attr-defined]
    strategy._state_store = None  # type: ignore[attr-defined]
    strategy._model = MockMLModel()  # type: ignore[attr-defined]
    strategy._last_symbol = "AAPL"  # type: ignore[attr-defined]
    strategy._last_vector = None  # type: ignore[attr-defined]
    strategy._last_bet_ts = None  # type: ignore[attr-defined]
    strategy._stale_entries = StaleEntryTracker()  # type: ignore[attr-defined]
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
    # the G2 fully-qualifying coid {strategy}-{stamp}-{symbol}-{side} (the migration's coid scheme).
    assert entry.client_order_id.startswith("smoke-") and entry.client_order_id.endswith("-AAPL-buy")
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


class _CountingFakeTradingClient(FakeTradingClient):
    """A FakeTradingClient that counts get_order_by_client_id calls — to prove the broker GET-rate drops
    once a dead entry is abandoned (the spin stops)."""

    get_calls: int = 0

    def get_order_by_client_id(self, client_order_id: str) -> FakeOrder:
        self.get_calls += 1
        return super().get_order_by_client_id(client_order_id)


def test_stale_not_found_entry_is_abandoned_and_stops_spinning() -> None:
    """The reconcile-spin fix: a bet whose entry order is GENUINELY not-found is re-checked a bounded few
    times, then abandoned (marked closed) — after which the manage loop never queries the broker for it
    again (the GET-rate drops to zero), instead of spinning ~4 GETs/sec forever."""
    config = _config(hold_sec=900)
    trading = _CountingFakeTradingClient(fill_price=190.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    # a low threshold so the test resolves quickly (2 checks over >=0s); the live default is 5 over 30s.
    strategy._stale_entries = StaleEntryTracker(min_checks=2, min_seconds=0.0)  # type: ignore[attr-defined]

    # inject an OPEN bet whose entry order the broker will report not-found (it never landed).
    dead_coid = "smoke-20260616T150000-AAPL-buy"
    trading.not_found_coids.add(dead_coid)
    store.record_open("AAPL", "buy", 50.0, dead_coid, NOW, NOW + dt.timedelta(seconds=900))
    assert store.count_open() == 1

    # tick 1: 1st not-found (streak=1) -> not yet terminal, still open.
    strategy.manage_open_bets()
    assert store.count_open() == 1
    # tick 2: 2nd consecutive not-found -> terminal -> abandoned (closed), no position taken.
    strategy.manage_open_bets()
    assert store.count_open() == 0
    closed = store._bets[dead_coid]
    assert closed["status"] == "closed" and closed["realized_pnl"] == 0

    # THE SPIN STOPS: further manage cycles must NOT query the broker for the (now-closed) dead bet.
    calls_after_abandon = trading.get_calls
    for _ in range(10):
        strategy.manage_open_bets()
    assert trading.get_calls == calls_after_abandon  # zero additional GETs -> the spin is gone


def test_transient_then_found_entry_is_not_abandoned() -> None:
    """A live order that momentarily 404s on a race (then reappears) is NEVER wrongly expired: a reset
    breaks the not-found streak so only a CONSECUTIVE genuine-not-found run can terminate a bet."""
    config = _config(hold_sec=900)
    trading = FakeTradingClient(fill_price=190.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._stale_entries = StaleEntryTracker(min_checks=2, min_seconds=0.0)  # type: ignore[attr-defined]

    coid = "smoke-20260616T150000-AAPL-buy"
    trading.not_found_coids.add(coid)
    # a far-future hold (relative to the real wall clock the manage loop uses) so the bet stays open after
    # the entry fills, isolating the abandon-vs-fill behavior from the time-based close.
    far_future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3650)
    store.record_open("AAPL", "buy", 50.0, coid, NOW, far_future)

    strategy.manage_open_bets()  # 1st not-found (streak=1)
    assert store.count_open() == 1
    # the order "appears" now (the race resolved) -> the entry fills, the streak resets.
    trading.not_found_coids.discard(coid)
    trading._by_coid[coid] = FakeOrder(
        id="broker-x",
        client_order_id=coid,
        filled_avg_price=190.0,
        filled_qty=50.0 / 190.0,
        status=OrderStatus.FILLED,
        qty=50.0 / 190.0,
        filled_at=NOW,
    )
    strategy.manage_open_bets()  # the entry now fills -> NOT abandoned, the bet is live
    bet = store._bets[coid]
    assert bet["status"] == "filled"  # a real filled position, never expired (abandon -> 'closed')
    assert bet.get("qty") is not None  # the entry actually filled a real quantity
    assert strategy._stale_entries.streak_count(coid) == 0  # type: ignore[attr-defined]  # streak cleared


def test_is_order_not_found_detects_code() -> None:
    assert is_order_not_found(APIError(NOT_FOUND_BODY % "smoke_X_exit"))
    assert not is_order_not_found(APIError(SERVER_ERROR_BODY))
    assert not is_order_not_found(APIError("not even json"))
    # Falls back to message text when the body has no parseable code.
    assert is_order_not_found(APIError("order not found for smoke_X_exit"))


def test_exit_coid_helpers_roundtrip() -> None:
    base = exit_coid_for("smoke_SPY_T1")
    assert base == "smoke_SPY_T1_exit"
    assert exit_retry_count(base) == 0
    retry1 = exit_coid_for("smoke_SPY_T1", retry=1)
    assert retry1 == "smoke_SPY_T1_exit_r1"
    assert exit_retry_count(retry1) == 1
    assert exit_retry_count(exit_coid_for("smoke_SPY_T1", retry=2)) == 2


def test_close_resubmits_when_recorded_exit_not_found_at_broker() -> None:
    # The live SPY bug: bet is 'closing' with an exit_order_id the broker has NEVER heard of (the SELL
    # never landed — 5xx after mark_closing). Old code looped "CLOSE pending fill" forever; now we must
    # re-submit a FRESH exit and finalize.
    config = _config(hold_sec=900)
    trading = FakeTradingClient(fill_price=751.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._last_symbol = "SPY"  # type: ignore[attr-defined]

    entry_coid = "smoke_SPY_stuck"
    qty = 50.0 / 751.0
    store.record_open("SPY", "buy", 50.0, entry_coid, NOW, NOW - dt.timedelta(seconds=1))
    store.mark_filled(entry_coid, 751.0, qty)
    # Simulate the stuck state: marked closing with a base exit coid that does NOT exist at the broker.
    lost_exit = exit_coid_for(entry_coid)
    store.mark_closing(entry_coid, lost_exit)
    trading.not_found_coids.add(lost_exit)

    trading.fill_price = 752.0  # the re-submitted close fills here
    bet = store.list_open()[0]
    strategy._close_bet(bet)  # type: ignore[attr-defined]

    assert store.count_open() == 0  # finalized, not looping
    closed = store._bets[entry_coid]
    assert closed["status"] == "closed"
    assert closed["exit_order_id"] == exit_coid_for(entry_coid, retry=1)  # fresh retry coid
    assert closed["realized_pnl"] == pytest.approx((752.0 - 751.0) * qty)
    # Exactly one SELL was actually placed (the re-submit); the lost one never landed.
    sells = [order for order in trading.submitted if order.client_order_id.endswith("_r1")]
    assert len(sells) == 1


def test_close_propagates_then_resubmits_on_server_error() -> None:
    # End-to-end of the real failure mode: the FIRST exit submit hits a 5xx (propagates), the bet is
    # left 'closing' with an order the broker lacks; the NEXT close cycle re-submits and finalizes.
    config = _config(hold_sec=900)
    trading = FakeTradingClient(fill_price=751.0)
    store = InMemoryStore()
    strategy = _strategy(config, trading, store)
    strategy._last_symbol = "SPY"  # type: ignore[attr-defined]

    entry_coid = "smoke_SPY_5xx"
    qty = 50.0 / 751.0
    store.record_open("SPY", "buy", 50.0, entry_coid, NOW, NOW - dt.timedelta(seconds=1))
    store.mark_filled(entry_coid, 751.0, qty)
    base_exit = exit_coid_for(entry_coid)
    trading.fail_submit_once.add(base_exit)  # first SELL submit 5xx-es

    bet = store.list_open()[0]
    with pytest.raises(APIError):  # the 5xx propagates to the loop's handler
        strategy._close_bet(bet)  # type: ignore[attr-defined]
    assert store.list_open()[0]["status"] == "closing"  # left mid-close, exit recorded

    strategy._close_bet(store.list_open()[0])  # type: ignore[attr-defined]  # next cycle resolves it
    assert store.count_open() == 0
    assert store._bets[entry_coid]["status"] == "closed"


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
