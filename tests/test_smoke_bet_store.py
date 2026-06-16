"""BetStore integration test against the real timescaledb in a THROWAWAY schema (dropped after).

Skips cleanly when no DB is reachable so the unit suite still runs without infra. We monkeypatch the
module-level SCHEMA + DDL to a unique per-run name so this never touches the live ``strat_smoke``
schema, then DROP ... CASCADE in teardown — zero residue.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

import strategies.smoke.bet_store as bet_store_module
from strategies.smoke.bet_store import BetStore

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ.get("DB_PASSWORD", "test"),
}


def _db_up() -> bool:
    try:
        conn = psycopg.connect(**DB_KWARGS, connect_timeout=3)  # type: ignore[arg-type]
        conn.close()
        return True
    except psycopg.Error:
        return False


@pytest.fixture()
def throwaway_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[BetStore]:
    if not _db_up():
        pytest.skip("timescaledb not reachable")
    schema = f"strat_smoke_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(bet_store_module, "SCHEMA", schema)
    monkeypatch.setattr(bet_store_module, "_SCHEMA_DDL", f"CREATE SCHEMA IF NOT EXISTS {schema}")
    monkeypatch.setattr(
        bet_store_module,
        "_TABLE_DDL",
        bet_store_module._TABLE_DDL.replace("strat_smoke.bets", f"{schema}.bets"),
    )
    store = BetStore(DB_KWARGS)
    yield store
    conn = psycopg.connect(**DB_KWARGS, autocommit=True)  # type: ignore[arg-type]
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.close()


def test_open_fill_close_lifecycle(throwaway_store: BetStore) -> None:
    store = throwaway_store
    now = dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)
    hold_until = now + dt.timedelta(seconds=900)

    bet_id = store.record_open("AAPL", "buy", 2, "smoke_AAPL_x", now, hold_until)
    assert bet_id > 0
    assert store.count_open() == 1
    assert store.open_notional() == 0.0  # not filled yet -> no realized notional

    open_bets = store.list_open()
    assert len(open_bets) == 1
    assert open_bets[0]["symbol"] == "AAPL"
    assert open_bets[0]["status"] == "open"
    assert open_bets[0]["entry_price"] is None

    store.mark_filled("smoke_AAPL_x", 190.0)
    assert store.open_notional() == pytest.approx(380.0)  # 2 * 190
    assert store.list_open()[0]["status"] == "filled"

    store.mark_closing("smoke_AAPL_x", "smoke_AAPL_x_exit")
    assert store.list_open()[0]["status"] == "closing"
    assert store.list_open()[0]["exit_order_id"] == "smoke_AAPL_x_exit"

    exit_ts = now + dt.timedelta(seconds=900)
    store.record_close("smoke_AAPL_x", exit_ts, 191.5, (191.5 - 190.0) * 2)
    assert store.count_open() == 0
    assert store.list_open() == []


def test_record_open_idempotent(throwaway_store: BetStore) -> None:
    store = throwaway_store
    now = dt.datetime(2026, 6, 15, 15, 0, tzinfo=dt.timezone.utc)
    hold_until = now + dt.timedelta(seconds=600)
    first = store.record_open("MSFT", "buy", 1, "smoke_MSFT_dup", now, hold_until)
    second = store.record_open("MSFT", "buy", 1, "smoke_MSFT_dup", now, hold_until)
    assert first == second  # same coid -> same row, no duplicate
    assert store.count_open() == 1


def test_list_open_excludes_closed(throwaway_store: BetStore) -> None:
    store = throwaway_store
    now = dt.datetime(2026, 6, 15, 16, 0, tzinfo=dt.timezone.utc)
    hold = now + dt.timedelta(seconds=300)
    store.record_open("NVDA", "buy", 1, "smoke_NVDA_a", now, hold)
    store.record_open("SPY", "buy", 1, "smoke_SPY_b", now, hold)
    store.mark_filled("smoke_NVDA_a", 1000.0)
    store.record_close("smoke_NVDA_a", now, 1001.0, 1.0)
    open_symbols = {bet["symbol"] for bet in store.list_open()}
    assert open_symbols == {"SPY"}
    assert store.count_open() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
