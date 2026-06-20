"""Postgres ledger for the overnight-beta strategy — its OWN ``strat_overnightbeta`` schema.

Two tables (via the reusable ``StrategyStore``):
  - ``positions``: each overnight leg (one row per symbol per overnight), status-partitioned
    (entered → flattened), with the submit (close-auction) + flatten (open-auction) order ids + fills.
  - ``slippage_log``: the PRIMARY deliverable — for every auction fill, the model-EXPECTED reference price
    (the official close/open print the certify assumed) vs the REALIZED fill price → realized slippage in
    bps. This is the measurement that gates the edge (real vs the backtest's 5 bps model).

All access is via ``StrategyStore``'s parameterized helpers (short-lived autocommit connections); the
container author never touches global DB config.
"""

from __future__ import annotations

import datetime as dt
from typing import cast

from strategies.lib.store import StrategyStore

STRATEGY = "overnightbeta"

POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.positions (
    id              bigserial PRIMARY KEY,
    rebalance_date  date        NOT NULL,
    symbol          text        NOT NULL,
    leg             text        NOT NULL,              -- 'long' (high-beta) | 'short' (low-beta)
    beta            numeric,
    target_notional numeric     NOT NULL,              -- signed target $ for this overnight leg
    enter_order_id  text        NOT NULL UNIQUE,       -- the close-auction (CLS) client_order_id
    enter_ts        timestamptz NOT NULL,
    enter_ref_price numeric,                           -- model-expected close (for slippage)
    enter_fill_price numeric,                          -- realized close-auction fill
    enter_qty       numeric,
    exit_order_id   text,                              -- the open-auction (OPG) client_order_id
    exit_ts         timestamptz,
    exit_ref_price  numeric,                           -- model-expected open
    exit_fill_price numeric,                           -- realized open-auction fill
    realized_pnl    numeric,
    status          text        NOT NULL DEFAULT 'entered'  -- 'entered' | 'flattened'
)
"""

SLIPPAGE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.slippage_log (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    symbol          text        NOT NULL,
    auction         text        NOT NULL,              -- 'close' (MOC/CLS) | 'open' (MOO/OPG)
    side            text        NOT NULL,              -- 'buy' | 'sell'
    ref_price       numeric     NOT NULL,              -- the model-expected auction reference price
    fill_price      numeric     NOT NULL,              -- the realized fill
    slippage_bps    numeric     NOT NULL,              -- signed adverse slippage in bps (the deliverable)
    order_id        text
)
"""

_OPEN_COLUMNS = (
    "id",
    "rebalance_date",
    "symbol",
    "leg",
    "beta",
    "target_notional",
    "enter_order_id",
    "enter_ts",
    "enter_ref_price",
    "enter_fill_price",
    "enter_qty",
    "exit_order_id",
    "status",
)


def slippage_bps(side: str, ref_price: float, fill_price: float) -> float:
    """Signed ADVERSE slippage in bps. For a BUY, paying ABOVE the reference is adverse (positive);
    for a SELL, filling BELOW the reference is adverse (positive). So adverse slippage is always the
    cost you paid vs the expected auction print."""
    if ref_price <= 0:
        return 0.0
    raw = (fill_price - ref_price) / ref_price * 1e4
    return raw if side == "buy" else -raw


class PositionStore:
    """Durable overnight-leg ledger + the auction-slippage log in ``strat_overnightbeta`` (via StrategyStore)."""

    def __init__(self, db_kwargs: dict[str, str | int]) -> None:
        self._store = StrategyStore(STRATEGY, [POSITIONS_DDL, SLIPPAGE_DDL], db_kwargs)
        self._schema = self._store.schema

    @property
    def schema(self) -> str:
        return self._schema

    def record_enter(
        self,
        rebalance_date: dt.date,
        symbol: str,
        leg: str,
        beta: float,
        target_notional: float,
        enter_order_id: str,
        enter_ts: dt.datetime,
        enter_ref_price: float,
    ) -> int:
        """Insert a freshly-submitted overnight leg (status='entered'). Idempotent on enter_order_id."""
        row = self._store.execute_returning(
            f"""INSERT INTO {self._schema}.positions
                    (rebalance_date, symbol, leg, beta, target_notional, enter_order_id, enter_ts,
                     enter_ref_price, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'entered')
                ON CONFLICT (enter_order_id) DO NOTHING
                RETURNING id""",
            (rebalance_date, symbol, leg, beta, target_notional, enter_order_id, enter_ts, enter_ref_price),
        )
        if row is not None:
            return int(cast(int, row[0]))
        existing = self._store.query(
            f"SELECT id FROM {self._schema}.positions WHERE enter_order_id = %s", (enter_order_id,)
        )
        if not existing:
            raise RuntimeError(f"record_enter: no row for {enter_order_id!r}")
        return int(cast(int, existing[0][0]))

    def mark_entered_fill(self, enter_order_id: str, fill_price: float, qty: float) -> None:
        self._store.execute(
            f"""UPDATE {self._schema}.positions SET enter_fill_price = %s, enter_qty = %s
                 WHERE enter_order_id = %s""",
            (fill_price, qty, enter_order_id),
        )

    def mark_exit_submitted(self, enter_order_id: str, exit_order_id: str, exit_ref_price: float) -> None:
        self._store.execute(
            f"""UPDATE {self._schema}.positions SET exit_order_id = %s, exit_ref_price = %s
                 WHERE enter_order_id = %s AND status = 'entered'""",
            (exit_order_id, exit_ref_price, enter_order_id),
        )

    def record_flatten(
        self, enter_order_id: str, exit_ts: dt.datetime, exit_fill_price: float, realized_pnl: float
    ) -> None:
        self._store.execute(
            f"""UPDATE {self._schema}.positions
                   SET exit_ts = %s, exit_fill_price = %s, realized_pnl = %s, status = 'flattened'
                 WHERE enter_order_id = %s""",
            (exit_ts, exit_fill_price, realized_pnl, enter_order_id),
        )

    def mark_abandoned(self, enter_order_id: str) -> None:
        """Terminate a leg whose close-auction entry never landed at the broker (genuinely not-found):
        advance it to 'flattened' with zero realized PnL (no position was ever taken), so manage stops
        re-querying a dead order forever. Backward-readable (existing status column/values)."""
        self._store.execute(
            f"""UPDATE {self._schema}.positions
                   SET status = 'flattened', realized_pnl = 0
                 WHERE enter_order_id = %s AND status = 'entered'""",
            (enter_order_id,),
        )

    def log_slippage(
        self, symbol: str, auction: str, side: str, ref_price: float, fill_price: float, order_id: str
    ) -> None:
        """The PRIMARY deliverable: log the realized auction slippage (the number that gates the edge)."""
        self._store.execute(
            f"""INSERT INTO {self._schema}.slippage_log
                    (symbol, auction, side, ref_price, fill_price, slippage_bps, order_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                symbol,
                auction,
                side,
                ref_price,
                fill_price,
                slippage_bps(side, ref_price, fill_price),
                order_id,
            ),
        )

    def list_entered(self) -> list[dict[str, object]]:
        """All legs still 'entered' (awaiting the open-auction flatten)."""
        return self._store.query_dicts(
            f"""SELECT {", ".join(_OPEN_COLUMNS)} FROM {self._schema}.positions
                 WHERE status = 'entered' ORDER BY enter_ts ASC""",
            _OPEN_COLUMNS,
        )

    def count_entered(self) -> int:
        rows = self._store.query(f"SELECT count(*) FROM {self._schema}.positions WHERE status = 'entered'")
        return int(cast(int, rows[0][0])) if rows else 0

    def mean_slippage_bps(self) -> dict[str, float]:
        """The running answer: mean realized auction slippage by auction side — the deliverable summary."""
        rows = self._store.query(
            f"SELECT auction, avg(slippage_bps), count(*) FROM {self._schema}.slippage_log GROUP BY auction"
        )
        return {str(r[0]): float(cast(float, r[1])) for r in rows} if rows else {}
