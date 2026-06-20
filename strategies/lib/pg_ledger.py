"""`PgFillLedger` — a durable append-only fill ledger in a strategy's ``strat_<name>`` Postgres schema,
satisfying the `LedgerBackend` protocol so it backs the production `PgStateStore`.

This is the StrategyState side of the bet-store -> StrategyState migration (docs/
STRATEGY_EXECUTION_ABSTRACTION.md §5). It is ADDITIVE: a new ``fills`` table in the SAME ``strat_<name>``
schema, ALONGSIDE the container's existing bespoke table (e.g. smoke's ``bets``), which is left untouched.
So the migration is BACKWARD-READABLE by construction — a rolled-back container on the OLD path reads its
unchanged ``bets`` table exactly as before and never sees / never needs the ``fills`` table. The fills
ledger is the source from which `StrategyState` positions are recomputable on restart (REQ-S2).

One row per fill EVENT (append-only, never updated): the cumulative-vs-delta distinction is the writer's
job (the ProductionRunner books deltas), so the ledger just stores what it is handed and replays it in
insertion order through `apply_fill` on load.
"""

from __future__ import annotations

from collections.abc import Sequence

from quantlib.strategy_core.execution import Fill, OrderState
from strategies.lib.store import StrategyStore

FILLS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.fills (
    id              bigserial PRIMARY KEY,
    client_order_id text        NOT NULL,
    symbol          text        NOT NULL,
    side            text        NOT NULL,
    filled_qty      numeric     NOT NULL,
    avg_price       numeric     NOT NULL,
    status          text        NOT NULL,
    appended_at     timestamptz NOT NULL DEFAULT now()
)
"""

_FILL_COLUMNS = ("client_order_id", "symbol", "side", "filled_qty", "avg_price", "status")


class PgFillLedger:
    """The append-only fill ledger for a strategy, in its ``strat_<name>`` schema. A `LedgerBackend`."""

    def __init__(self, store: StrategyStore) -> None:
        self._store = store
        self._schema = store.schema

    def append(self, strategy_id: str, fill: Fill) -> None:
        """Append one fill event (never updated). ``strategy_id`` is implicit in the schema ownership, so
        it is not stored per-row (the table IS this strategy's ledger)."""
        self._store.execute(
            f"""INSERT INTO {self._schema}.fills
                    (client_order_id, symbol, side, filled_qty, avg_price, status)
                VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                fill.client_order_id,
                fill.symbol,
                fill.side,
                fill.filled_qty,
                fill.avg_price,
                fill.status.value,
            ),
        )

    def read(self, strategy_id: str) -> Sequence[Fill]:
        """Replay the ledger in insertion order — the rows from which `StrategyState` is recomputed."""
        rows = self._store.query_dicts(
            f"""SELECT {", ".join(_FILL_COLUMNS)}
                  FROM {self._schema}.fills
                 ORDER BY id ASC""",
            _FILL_COLUMNS,
        )
        return [
            Fill(
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                weight=0.0,
                fill_price=float(row["avg_price"]),  # type: ignore[arg-type]
                cost_bps=0.0,
                client_order_id=str(row["client_order_id"]),
                filled_qty=float(row["filled_qty"]),  # type: ignore[arg-type]
                avg_price=float(row["avg_price"]),  # type: ignore[arg-type]
                status=OrderState(str(row["status"])),
            )
            for row in rows
        ]
