"""Postgres-backed bet ledger for the smoke strategy — built on the reusable ``StrategyStore``.

The smoke strategy owns its OWN schema, ``strat_smoke`` (``StrategyStore`` derives it from the strategy
name "smoke"), so it never collides table names with the executor or any future strategy. The store is
self-service: it declares one ``CREATE TABLE`` statement and ``StrategyStore`` creates the schema + table
idempotently — the author never touches global DB config or schema-management internals.

The store is the durable book the strategy reconciles against on restart — OPEN bets are resumed, CLOSED
bets are the realized record. All access goes through ``StrategyStore``'s parameterized helpers (short-
lived autocommit connections, so a transient DB blip never holds a stale handle).

Schema (one table, ``bets``, status-partitioned by the ``status`` column):
    id             bigserial primary key
    symbol         text       the traded ticker
    side           text       'buy' (smoke only goes long for now)
    qty            numeric    whole shares
    entry_order_id text       Alpaca client_order_id (our idempotency key, prefix ``smoke_``)
    entry_ts       timestamptz when we submitted the open
    entry_price    numeric    avg fill price of the open (NULL until filled)
    hold_until     timestamptz when the time-based exit fires
    exit_order_id  text       Alpaca client_order_id of the close
    exit_ts        timestamptz when the close filled
    exit_price     numeric    avg fill price of the close
    realized_pnl   numeric    (exit_price - entry_price) * qty  (long)
    status         text       'open' | 'filled' | 'closing' | 'closed'
"""
from __future__ import annotations

import datetime as dt
from typing import cast

from strategies.lib.store import StrategyStore

STRATEGY = "smoke"

BETS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.bets (
    id             bigserial PRIMARY KEY,
    symbol         text        NOT NULL,
    side           text        NOT NULL,
    qty            numeric     NOT NULL,
    entry_order_id text        NOT NULL UNIQUE,
    entry_ts       timestamptz NOT NULL,
    entry_price    numeric,
    hold_until     timestamptz NOT NULL,
    exit_order_id  text,
    exit_ts        timestamptz,
    exit_price     numeric,
    realized_pnl   numeric,
    status         text        NOT NULL DEFAULT 'open'
)
"""

_OPEN_COLUMNS = (
    "id", "symbol", "side", "qty", "entry_order_id", "entry_ts",
    "entry_price", "hold_until", "exit_order_id", "status",
)
_OPEN_STATUSES = ("open", "filled", "closing")


class BetStore:
    """Durable OPEN/CLOSED bet ledger in the ``strat_smoke`` Postgres schema (via ``StrategyStore``)."""

    def __init__(self, db_kwargs: dict[str, str | int]) -> None:
        self._store = StrategyStore(STRATEGY, [BETS_TABLE_DDL], db_kwargs)
        self._schema = self._store.schema

    def record_open(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_order_id: str,
        entry_ts: dt.datetime,
        hold_until: dt.datetime,
    ) -> int:
        """Insert a freshly-submitted open bet (status='open'). Returns its row id.

        The ``entry_order_id`` UNIQUE constraint makes this idempotent: a duplicate submit (e.g. a
        restart mid-place) hits ON CONFLICT DO NOTHING and we recover the existing id.
        """
        row = self._store.execute_returning(
            f"""INSERT INTO {self._schema}.bets
                    (symbol, side, qty, entry_order_id, entry_ts, hold_until, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'open')
                ON CONFLICT (entry_order_id) DO NOTHING
                RETURNING id""",
            (symbol, side, qty, entry_order_id, entry_ts, hold_until),
        )
        if row is not None:
            return int(cast(int, row[0]))
        existing = self._store.query(
            f"SELECT id FROM {self._schema}.bets WHERE entry_order_id = %s", (entry_order_id,)
        )
        if not existing:
            raise RuntimeError(f"record_open: no row for entry_order_id {entry_order_id!r}")
        return int(cast(int, existing[0][0]))

    def mark_filled(self, entry_order_id: str, entry_price: float) -> None:
        """Record the open's average fill price and advance status open -> filled."""
        self._store.execute(
            f"""UPDATE {self._schema}.bets
                   SET entry_price = %s, status = 'filled'
                 WHERE entry_order_id = %s AND status = 'open'""",
            (entry_price, entry_order_id),
        )

    def mark_closing(self, entry_order_id: str, exit_order_id: str) -> None:
        """Record that a closing order was submitted; advance status -> closing (idempotency guard)."""
        self._store.execute(
            f"""UPDATE {self._schema}.bets
                   SET exit_order_id = %s, status = 'closing'
                 WHERE entry_order_id = %s AND status IN ('open', 'filled')""",
            (exit_order_id, entry_order_id),
        )

    def record_close(
        self,
        entry_order_id: str,
        exit_ts: dt.datetime,
        exit_price: float,
        realized_pnl: float,
    ) -> None:
        """Finalize a bet: record exit fill + realized PnL and advance status -> closed."""
        self._store.execute(
            f"""UPDATE {self._schema}.bets
                   SET exit_ts = %s, exit_price = %s, realized_pnl = %s, status = 'closed'
                 WHERE entry_order_id = %s""",
            (exit_ts, exit_price, realized_pnl, entry_order_id),
        )

    def list_open(self) -> list[dict[str, object]]:
        """All bets not yet closed (status in open/filled/closing) — the set to manage/reconcile."""
        return self._store.query_dicts(
            f"""SELECT {", ".join(_OPEN_COLUMNS)}
                  FROM {self._schema}.bets
                 WHERE status IN ('open', 'filled', 'closing')
                 ORDER BY entry_ts ASC""",
            _OPEN_COLUMNS,
        )

    def count_open(self) -> int:
        """Number of un-closed bets — the concurrency-cap input."""
        rows = self._store.query(
            f"SELECT count(*) FROM {self._schema}.bets WHERE status IN ('open', 'filled', 'closing')"
        )
        return int(cast(int, rows[0][0])) if rows else 0

    def open_notional(self) -> float:
        """Sum of (qty * entry_price) over un-closed bets — the gross-notional-cap input.

        Bets still 'open' (entry not yet filled) have NULL entry_price; they're excluded here because
        their realized notional is unknown until fill — the caller adds the prospective new-bet
        notional to this when checking the cap, which is conservative enough for a smoke test.
        """
        rows = self._store.query(
            f"""SELECT COALESCE(sum(qty * entry_price), 0)
                  FROM {self._schema}.bets
                 WHERE status IN ('filled', 'closing') AND entry_price IS NOT NULL"""
        )
        return float(cast(float, rows[0][0])) if rows else 0.0
