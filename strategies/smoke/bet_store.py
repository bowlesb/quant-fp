"""Postgres-backed bet ledger for the smoke strategy — built on the reusable ``StrategyStore``.

The smoke strategy owns its OWN schema, ``strat_smoke`` (``StrategyStore`` derives it from the strategy
name "smoke"), so it never collides table names with the executor or any future strategy. The store is
self-service: it declares one ``CREATE TABLE`` statement and ``StrategyStore`` creates the schema + table
idempotently — the author never touches global DB config or schema-management internals.

The store is the durable book the strategy reconciles against on restart — OPEN bets are resumed, CLOSED
bets are the realized record. All access goes through ``StrategyStore``'s parameterized helpers (short-
lived autocommit connections, so a transient DB blip never holds a stale handle).

Sizing is by NOTIONAL, not whole shares: the entry is a notional market BUY for ``entry_notional``
dollars (``SMOKE_NOTIONAL_USD``), so a $50 bet costs ~$50 regardless of share price. The filled
fractional ``qty`` is unknown until the open fills, so ``qty`` is NULL until then; the close sells
exactly that filled ``qty``.

Schema (one table, ``bets``, status-partitioned by the ``status`` column):
    id             bigserial primary key
    symbol         text       the traded ticker
    side           text       'buy' (smoke only goes long for now)
    entry_notional numeric    the target dollar size of the open (SMOKE_NOTIONAL_USD)
    qty            numeric    filled (fractional) shares of the open (NULL until filled)
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
    entry_notional numeric     NOT NULL,
    qty            numeric,
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
    "id", "symbol", "side", "entry_notional", "qty", "entry_order_id", "entry_ts",
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
        entry_notional: float,
        entry_order_id: str,
        entry_ts: dt.datetime,
        hold_until: dt.datetime,
    ) -> int:
        """Insert a freshly-submitted open bet (status='open'). Returns its row id.

        The bet is sized by NOTIONAL (``entry_notional`` dollars); the filled fractional ``qty`` is
        unknown until the open fills, so it stays NULL here and is set by ``mark_filled``.

        The ``entry_order_id`` UNIQUE constraint makes this idempotent: a duplicate submit (e.g. a
        restart mid-place) hits ON CONFLICT DO NOTHING and we recover the existing id.
        """
        row = self._store.execute_returning(
            f"""INSERT INTO {self._schema}.bets
                    (symbol, side, entry_notional, entry_order_id, entry_ts, hold_until, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'open')
                ON CONFLICT (entry_order_id) DO NOTHING
                RETURNING id""",
            (symbol, side, entry_notional, entry_order_id, entry_ts, hold_until),
        )
        if row is not None:
            return int(cast(int, row[0]))
        existing = self._store.query(
            f"SELECT id FROM {self._schema}.bets WHERE entry_order_id = %s", (entry_order_id,)
        )
        if not existing:
            raise RuntimeError(f"record_open: no row for entry_order_id {entry_order_id!r}")
        return int(cast(int, existing[0][0]))

    def mark_filled(self, entry_order_id: str, entry_price: float, qty: float) -> None:
        """Record the open's average fill price and filled (fractional) qty; advance open -> filled.

        The filled ``qty`` is the actual fractional share count Alpaca filled the notional buy with;
        the close sells exactly this quantity.
        """
        self._store.execute(
            f"""UPDATE {self._schema}.bets
                   SET entry_price = %s, qty = %s, status = 'filled'
                 WHERE entry_order_id = %s AND status = 'open'""",
            (entry_price, qty, entry_order_id),
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
        """Sum of ACTUAL open dollar exposure over un-closed bets — the gross-notional-cap input.

        For a filled bet, exposure is the real cost ``qty * entry_price``. For a bet still 'open'
        (entry not yet filled, qty/entry_price NULL) we use its ``entry_notional`` — the dollar size
        we submitted the notional buy for — so an unfilled bet still counts against the cap and can
        never escape it. This makes the cap bound real exposure regardless of share price.
        """
        rows = self._store.query(
            f"""SELECT COALESCE(sum(
                    CASE WHEN entry_price IS NOT NULL AND qty IS NOT NULL
                         THEN qty * entry_price
                         ELSE entry_notional
                    END
                ), 0)
                  FROM {self._schema}.bets
                 WHERE status IN ('open', 'filled', 'closing')"""
        )
        return float(cast(float, rows[0][0])) if rows else 0.0
