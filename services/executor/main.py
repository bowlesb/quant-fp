"""Executor + reconciliation (Phase 0 hello-world).

Two responsibilities, on a 5-minute loop:
1. Once per trading day, while the market is open, submit one tiny **paper**
   market order, record it in orders_log, poll for the fill, record it in
   fills_log. This exercises the order -> fill -> persist path.
2. Every cycle, reconcile our DB's position view (net signed fills) against
   Alpaca's /positions and log the result to reconciliation_log.

Real strategy order flow replaces step 1 in Phase 4; the reconciliation loop is
permanent.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import psycopg
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("executor")

MODE = os.environ.get("MODE", "paper")
HELLO_SYMBOL = os.environ.get("HELLO_SYMBOL", "SPY")
HELLO_QTY = int(os.environ.get("HELLO_QTY", "1"))
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "300"))

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

trading = TradingClient(
    os.environ["ALPACA_KEY_ID"],
    os.environ["ALPACA_SECRET_KEY"],
    paper=(MODE == "paper"),
)


def db() -> psycopg.Connection:
    return psycopg.connect(**DB_KWARGS, autocommit=True)


def already_ordered_today(conn: psycopg.Connection, day: str) -> bool:
    """True if today's hello order exists in our DB or already at the broker.

    Checking the broker too keeps us idempotent across DB resets: Alpaca enforces
    unique client_order_id, so a date-keyed id must not be resubmitted.
    """
    client_order_id = f"hello-{day}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM orders_log WHERE client_order_id = %s", (client_order_id,)
        )
        if cur.fetchone() is not None:
            return True
    try:
        trading.get_order_by_client_id(client_order_id)
        return True
    except APIError:
        return False


def place_hello_order(conn: psycopg.Connection, day: str) -> None:
    client_order_id = f"hello-{day}"
    intended_at = datetime.now(timezone.utc)
    request = MarketOrderRequest(
        symbol=HELLO_SYMBOL,
        qty=HELLO_QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )
    order = trading.submit_order(request)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_log
                (client_order_id, symbol, side, qty, order_type, mode,
                 intended_at, submitted_at, alpaca_order_id, status)
            VALUES (%s,%s,'buy',%s,'market',%s,%s,%s,%s,%s)
            ON CONFLICT (client_order_id) DO NOTHING
            """,
            (
                client_order_id,
                HELLO_SYMBOL,
                HELLO_QTY,
                MODE,
                intended_at,
                order.submitted_at,
                str(order.id),
                str(order.status),
            ),
        )
    logger.info("placed hello order %s (alpaca id %s)", client_order_id, order.id)
    poll_fill(conn, str(order.id))


def poll_fill(conn: psycopg.Connection, alpaca_order_id: str) -> None:
    for _ in range(20):
        order = trading.get_order_by_id(alpaca_order_id)
        if order.filled_qty and float(order.filled_qty) > 0:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO fills_log (alpaca_order_id, fill_ts, qty, price)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (alpaca_order_id, fill_ts) DO NOTHING
                    """,
                    (
                        alpaca_order_id,
                        order.filled_at or datetime.now(timezone.utc),
                        float(order.filled_qty),
                        float(order.filled_avg_price),
                    ),
                )
                cur.execute(
                    "UPDATE orders_log SET status=%s WHERE alpaca_order_id=%s",
                    (str(order.status), alpaca_order_id),
                )
            logger.info(
                "fill: %s qty=%s @ %s",
                alpaca_order_id,
                order.filled_qty,
                order.filled_avg_price,
            )
            return
        time.sleep(3)
    logger.warning("order %s not filled after polling", alpaca_order_id)


def db_positions(conn: psycopg.Connection) -> dict[str, float]:
    """Net signed quantity per symbol from our recorded fills."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.symbol,
                   sum(f.qty * CASE WHEN o.side='buy' THEN 1 ELSE -1 END)
            FROM fills_log f
            JOIN orders_log o ON o.alpaca_order_id = f.alpaca_order_id
            GROUP BY o.symbol
            """
        )
        return {symbol: float(qty) for symbol, qty in cur.fetchall()}


def reconcile(conn: psycopg.Connection) -> None:
    alpaca_positions = {
        position.symbol: float(position.qty)
        for position in trading.get_all_positions()
    }
    ours = db_positions(conn)
    symbols = set(alpaca_positions) | set(ours)
    mismatches = [
        {
            "symbol": symbol,
            "db": ours.get(symbol, 0.0),
            "alpaca": alpaca_positions.get(symbol, 0.0),
        }
        for symbol in symbols
        if abs(ours.get(symbol, 0.0) - alpaca_positions.get(symbol, 0.0)) > 1e-6
    ]
    ok = not mismatches
    detail = {"db": ours, "alpaca": alpaca_positions, "mismatches": mismatches}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reconciliation_log (ts, ok, detail)
            VALUES (%s,%s,%s)
            ON CONFLICT (ts) DO NOTHING
            """,
            (datetime.now(timezone.utc), ok, json.dumps(detail)),
        )
    if ok:
        logger.info("reconciliation OK (%d symbols)", len(symbols))
    else:
        logger.warning("reconciliation MISMATCH: %s", mismatches)


def main() -> None:
    logger.info(
        "executor starting: mode=%s hello=%dx%s loop=%ds",
        MODE,
        HELLO_QTY,
        HELLO_SYMBOL,
        LOOP_SECONDS,
    )
    while True:
        try:
            with db() as conn:
                clock = trading.get_clock()
                day = clock.timestamp.strftime("%Y-%m-%d")
                if clock.is_open and not already_ordered_today(conn, day):
                    place_hello_order(conn, day)
                reconcile(conn)
        except (psycopg.Error, APIError, ValueError, KeyError) as exc:
            logger.error("cycle error: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
