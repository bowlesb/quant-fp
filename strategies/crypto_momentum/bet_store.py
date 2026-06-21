"""Postgres-backed bet ledger for the crypto-momentum strategy — its OWN ``strat_cryptomomentum`` schema.

Structurally identical to ``strategies/reversion/bet_store.BetStore`` (same notional-sized,
status-partitioned ``bets`` table, same idempotent open/fill/close lifecycle) — the difference is ONLY the
strategy name, so ``StrategyStore`` derives the isolated ``strat_cryptomomentum`` schema. ``symbol`` here is
the SLASHLESS bus/store form (``BTCUSD``); the strategy maps it to the slash Alpaca form (``BTC/USD``) only
at the order boundary, so this ledger and the coid stay clean (no ``/``).
"""

from __future__ import annotations

import datetime as dt
from typing import cast

from strategies.lib.store import StrategyStore

STRATEGY = "cryptomomentum"

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
    signal         numeric,
    hold_until     timestamptz NOT NULL,
    exit_order_id  text,
    exit_ts        timestamptz,
    exit_price     numeric,
    realized_pnl   numeric,
    status         text        NOT NULL DEFAULT 'open'
)
"""

_OPEN_COLUMNS = (
    "id",
    "symbol",
    "side",
    "entry_notional",
    "qty",
    "entry_order_id",
    "entry_ts",
    "entry_price",
    "hold_until",
    "exit_order_id",
    "status",
)


class BetStore:
    """Durable OPEN/CLOSED bet ledger in the ``strat_cryptomomentum`` Postgres schema (via ``StrategyStore``)."""

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
        signal: float,
    ) -> int:
        """Insert a freshly-submitted open bet (status='open'), recording the model ``signal`` that
        triggered it. Returns its row id. Idempotent on the ``entry_order_id`` UNIQUE constraint."""
        row = self._store.execute_returning(
            f"""INSERT INTO {self._schema}.bets
                    (symbol, side, entry_notional, entry_order_id, entry_ts, hold_until, signal, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')
                ON CONFLICT (entry_order_id) DO NOTHING
                RETURNING id""",
            (symbol, side, entry_notional, entry_order_id, entry_ts, hold_until, signal),
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
        """Record the open's average fill price + filled fractional qty; advance open -> filled."""
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

    def mark_abandoned(self, entry_order_id: str) -> None:
        """Terminate a bet whose entry order never landed at the broker (genuinely not-found): advance it
        to 'closed' with zero realized PnL (no position was ever taken), so the manage loop stops
        re-querying a dead order forever. Backward-readable (existing status column/values)."""
        self._store.execute(
            f"""UPDATE {self._schema}.bets
                   SET status = 'closed', realized_pnl = 0
                 WHERE entry_order_id = %s AND status IN ('open', 'filled', 'closing')""",
            (entry_order_id,),
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

        Filled bets use the real cost ``qty * entry_price``; a still-'open' (unfilled) bet uses its
        ``entry_notional`` so it still counts against the cap and can never escape it."""
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

    def has_open_symbol(self, symbol: str) -> bool:
        """Whether this symbol already has an un-closed bet — crypto-momentum holds at most one bet per
        name so it never stacks multiple longs on the same pair."""
        rows = self._store.query(
            f"""SELECT 1 FROM {self._schema}.bets
                 WHERE symbol = %s AND status IN ('open', 'filled', 'closing') LIMIT 1""",
            (symbol,),
        )
        return bool(rows)
