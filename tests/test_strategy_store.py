"""StrategyStore integration test against the real timescaledb in a THROWAWAY schema (dropped after).

Proves the self-service contract: given a strategy name + a list of CREATE TABLE statements (using the
``{schema}`` placeholder), the store creates ``strat_<name>`` + its tables idempotently and offers safe
parameterized read/write — the author never touches global DB config. Skips when no DB is reachable.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from strategies.lib.store import StrategyStore, schema_name

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ.get("DB_PASSWORD", "test"),
}

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.widgets (
    id    bigserial PRIMARY KEY,
    label text NOT NULL UNIQUE,
    n     integer NOT NULL
)
"""


def _db_up() -> bool:
    try:
        conn = psycopg.connect(**DB_KWARGS, connect_timeout=3)  # type: ignore[arg-type]
        conn.close()
        return True
    except psycopg.Error:
        return False


def test_schema_name_valid() -> None:
    assert schema_name("smoke") == "strat_smoke"
    assert schema_name("My_Strat2") == "strat_my_strat2"


def test_schema_name_rejects_injection() -> None:
    with pytest.raises(ValueError):
        schema_name("bad; drop table x")
    with pytest.raises(ValueError):
        schema_name("")


@pytest.fixture()
def throwaway_store() -> Iterator[StrategyStore]:
    if not _db_up():
        pytest.skip("timescaledb not reachable")
    strategy = f"audit_{uuid.uuid4().hex[:8]}"
    store = StrategyStore(strategy, [TABLE_DDL], DB_KWARGS)
    yield store
    store.drop()


def test_register_is_idempotent(throwaway_store: StrategyStore) -> None:
    # A second register() on the same store must not error (CREATE ... IF NOT EXISTS).
    throwaway_store.register()
    throwaway_store.register()
    rows = throwaway_store.query(f"SELECT count(*) FROM {throwaway_store.schema}.widgets")
    assert rows[0][0] == 0


def test_write_read_roundtrip(throwaway_store: StrategyStore) -> None:
    store = throwaway_store
    row = store.execute_returning(
        f"INSERT INTO {store.schema}.widgets (label, n) VALUES (%s, %s) RETURNING id",
        ("alpha", 7),
    )
    assert row is not None and int(row[0]) > 0  # type: ignore[arg-type]
    store.execute(f"INSERT INTO {store.schema}.widgets (label, n) VALUES (%s, %s)", ("beta", 3))
    dicts = store.query_dicts(
        f"SELECT label, n FROM {store.schema}.widgets ORDER BY label", ("label", "n")
    )
    assert dicts == [{"label": "alpha", "n": 7}, {"label": "beta", "n": 3}]


def test_drop_removes_schema(throwaway_store: StrategyStore) -> None:
    store = throwaway_store
    store.drop()
    rows = store.query(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s", (store.schema,)
    )
    assert rows[0][0] == 0
    store.register()  # re-create so the fixture teardown's drop is a clean no-op


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
