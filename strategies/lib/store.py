"""``StrategyStore`` — a reusable, self-service Postgres home for a strategy container.

A strategy author should NEVER touch global DB configuration or know how schemas are managed. They
declare a strategy name and a list of ``CREATE TABLE`` statements (using the ``{schema}`` placeholder)
and get back an object that:

  * owns an isolated ``strat_<name>`` schema (created idempotently on construction),
  * creates the declared tables idempotently (safe on every startup),
  * hands out short-lived autocommit connections and a safe parameterized ``execute`` / ``query`` so a
    transient DB blip never holds a stale handle.

The per-strategy schema is the isolation boundary: each container owns its namespace, can be dropped
or migrated independently, and can never collide table names with the executor or another strategy.

Usage (a new container, knowing nothing about DB internals):

    BETS_TABLE = '''
        CREATE TABLE IF NOT EXISTS {schema}.bets (
            id bigserial PRIMARY KEY,
            symbol text NOT NULL,
            ...
        )
    '''
    store = StrategyStore.from_env("smoke", [BETS_TABLE])
    store.execute(f"INSERT INTO {store.schema}.bets (symbol) VALUES (%s)", ("AAPL",))
    rows = store.query(f"SELECT symbol FROM {store.schema}.bets WHERE symbol = %s", ("AAPL",))

``{schema}`` in each DDL string is substituted with the strategy's real schema name, so the same DDL
text works for the live schema AND for a throwaway test schema with no edits.
"""
from __future__ import annotations

import os
from collections.abc import Sequence

import psycopg

SCHEMA_PREFIX = "strat_"


def schema_name(strategy: str) -> str:
    """The Postgres schema a strategy owns: ``strat_<name>``. Validates the name is a safe identifier
    (letters/digits/underscore) so it can be interpolated into DDL without injection risk."""
    if not strategy or not strategy.replace("_", "").isalnum():
        raise ValueError(f"invalid strategy name {strategy!r} (use letters, digits, underscore)")
    return f"{SCHEMA_PREFIX}{strategy.lower()}"


def db_kwargs_from_env() -> dict[str, str | int]:
    """Standard DB connection kwargs from the container environment (DB_PASSWORD is required)."""
    return {
        "host": os.environ.get("DB_HOST", "timescaledb"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ.get("DB_NAME", "quant"),
        "user": os.environ.get("DB_USER", "quant"),
        "password": os.environ["DB_PASSWORD"],
    }


class StrategyStore:
    """An isolated ``strat_<name>`` schema plus its tables, with safe parameterized access.

    Construction is the registration step: it creates the schema and every declared table (idempotent),
    so a container author never calls any DB-management code directly.
    """

    def __init__(
        self,
        strategy: str,
        table_ddls: Sequence[str],
        db_kwargs: dict[str, str | int],
    ) -> None:
        self.strategy = strategy
        self.schema = schema_name(strategy)
        self._table_ddls = list(table_ddls)
        self._db_kwargs = db_kwargs
        self.register()

    @classmethod
    def from_env(cls, strategy: str, table_ddls: Sequence[str]) -> StrategyStore:
        """Build a store using DB connection kwargs read from the environment."""
        return cls(strategy, table_ddls, db_kwargs_from_env())

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(**self._db_kwargs, autocommit=True)  # type: ignore[arg-type]

    def register(self) -> None:
        """Create the schema + every declared table. Idempotent — safe to call on every startup.

        Each DDL string's ``{schema}`` placeholder is substituted with this strategy's real schema name
        before execution, so the author writes schema-agnostic DDL.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            for ddl in self._table_ddls:
                cur.execute(ddl.format(schema=self.schema))

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        """Run a parameterized write (INSERT/UPDATE/DELETE/DDL). No result is returned."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))

    def execute_returning(self, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
        """Run a parameterized write with a RETURNING clause; return the first row (or None)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return tuple(row) if row is not None else None

    def query(self, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
        """Run a parameterized SELECT; return all rows as tuples."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [tuple(row) for row in cur.fetchall()]

    def query_dicts(self, sql: str, columns: Sequence[str], params: Sequence[object] = ()) -> list[dict[str, object]]:
        """Run a parameterized SELECT whose columns are ``columns`` (in order); return list of dicts."""
        return [dict(zip(columns, row)) for row in self.query(sql, params)]

    def drop(self) -> None:
        """DROP this strategy's schema CASCADE. For tests / teardown — never call on a live schema."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
