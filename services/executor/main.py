"""Executor — the trade step of the E2E slice.

Reads the latest model predictions and forms a TINY no-flip flat-start long/short paper
basket (top-K non-ETF longs / bottom-K shortable single-name shorts), honoring the
Alpaca foot-guns in docs/EXECUTION.md. Default DRY_RUN: compute + persist the INTENDED
basket to orders_log (status='intended') and log it, WITHOUT submitting. Submission is
enabled only after the live-scoring path is validated at the open.

Safeguards present in dry-run: staleness guard, ETF exclusion, shortable filter,
score-degeneracy guard, hard caps (K + per-name notional + gross), intent persisted
before submit (idempotent client_order_id).

NOT YET BUILT (owned by Execution/Risk — do not believe a docstring over the code):
the live-submit path, a real kill-switch + caps bound from a FRESH broker snapshot, the
diff->close->flip->open sequencer, ETB (not just shortable) enforcement, marketable-limit
pricing, EOD LOC flatten, and the reconciliation loop (the prior recon loop was dropped in
the f4ed85d rewrite — re-adding a read-only recon to dry-run is a tracked Execution/Risk
item). See docs/QA_LEDGER.md / RESPONSIBILITY_MAP.md.
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from quantlib.universe import is_etf_like

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("executor")

MODE = os.environ.get("MODE", "paper")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MODEL_VERSION = os.environ.get("MODEL_VERSION", "lgbm_fwd_30m_v1.0.0")
K_LONG = int(os.environ.get("K_LONG", "5"))
K_SHORT = int(os.environ.get("K_SHORT", "5"))
NOTIONAL_PER_NAME = float(os.environ.get("NOTIONAL_PER_NAME", "300"))
GROSS_CAP = float(os.environ.get("GROSS_CAP", "5000"))
# Staleness must scale with the strategy's HOLD HORIZON: 35m fits a 30-min model; an
# overnight model decides at ~15:55 for a next-open exit, so it needs ~1 trading day.
STALENESS_MAX_MIN = int(os.environ.get("STALENESS_MAX_MIN", "35"))
MIN_SCORE_SEP = float(os.environ.get("MIN_SCORE_SEP", "0.0005"))
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

trading = TradingClient(
    os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=(MODE == "paper")
)

INTENT_SQL = """
INSERT INTO orders_log
    (client_order_id, symbol, side, qty, order_type, mode, intended_at, status,
     prediction, model_version)
VALUES (%s,%s,%s,%s,'limit',%s,%s,'intended',%s,%s)
ON CONFLICT (client_order_id) DO UPDATE
SET qty=EXCLUDED.qty, prediction=EXCLUDED.prediction, intended_at=EXCLUDED.intended_at
"""


def latest_prediction_ts(conn: psycopg.Connection) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(ts) FROM predictions WHERE model_version=%s", (MODEL_VERSION,))
        row = cur.fetchone()
        return row[0] if row else None


def candidate_pool(conn: psycopg.Connection, ts: datetime) -> list[dict]:
    """Predictions at ts joined with shortability + name + latest close; ETFs dropped."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.symbol, p.score, am.easy_to_borrow, am.name,
                   (SELECT close FROM bars_1m b WHERE b.symbol=p.symbol AND b.ts<=%s
                    ORDER BY b.ts DESC LIMIT 1) AS last_close
            FROM predictions p JOIN asset_metadata am ON am.symbol=p.symbol
            WHERE p.model_version=%s AND p.ts=%s
            """,
            (ts, MODEL_VERSION, ts),
        )
        rows = cur.fetchall()
    pool = []
    for symbol, score, easy_to_borrow, name, last_close in rows:
        if is_etf_like(name) or last_close is None or last_close < 5:
            continue                              # single-stock, >=$5, real price only
        # ETB (not just shortable): HTB short opens get rejected by Alpaca (EXECUTION.md §2)
        pool.append({"symbol": symbol, "score": float(score), "etb": bool(easy_to_borrow),
                     "price": float(last_close)})
    return pool


def build_basket(pool: list[dict]) -> tuple[list[dict], list[dict]]:
    """Top-K longs / bottom-K shortable shorts (filter-then-select), whole shares."""
    ranked = sorted(pool, key=lambda r: (-r["score"], r["symbol"]))
    longs = ranked[:K_LONG]
    shorts = [r for r in reversed(ranked) if r["etb"]][:K_SHORT]   # ETB-only shorts
    short_syms = {r["symbol"] for r in shorts}
    longs = [r for r in longs if r["symbol"] not in short_syms]   # no long+short same name
    for leg in longs + shorts:
        leg["qty"] = max(1, int(NOTIONAL_PER_NAME // leg["price"]))
    return longs, shorts


def rebalance(conn: psycopg.Connection, ts: datetime) -> None:
    pool = candidate_pool(conn, ts)
    if len(pool) < K_LONG + K_SHORT:
        logger.warning("candidate pool too small (%d); skipping rebalance", len(pool))
        return
    longs, shorts = build_basket(pool)
    # score-degeneracy guard (QA): a thin-panel model can output near-constant scores,
    # so the decile cut is decided by alphabetical tie-break, not signal. Refuse to
    # form a basket unless the long/short tails are actually separated.
    sep = (min(leg["score"] for leg in longs) - max(leg["score"] for leg in shorts)) if (longs and shorts) else 0.0
    if sep < MIN_SCORE_SEP:
        logger.warning("scores degenerate (long/short separation %.6f < %.6f); basket would be "
                       "tie-break noise — skipping rebalance", sep, MIN_SCORE_SEP)
        return
    gross = sum(leg["qty"] * leg["price"] for leg in longs + shorts)
    if gross > GROSS_CAP:
        logger.error("intended gross %.0f exceeds cap %.0f; skipping", gross, GROSS_CAP)
        return
    rb = ts.strftime("%Y%m%dT%H%M")
    rows = []
    for leg, side in [(x, "buy") for x in longs] + [(x, "sell") for x in shorts]:
        coid = f"{MODEL_VERSION}-{rb}-{leg['symbol']}-{side}"
        rows.append((coid, leg["symbol"], side, leg["qty"], MODE if not DRY_RUN else "dry_run",
                     datetime.now(timezone.utc), leg["score"], MODEL_VERSION))
    with conn.cursor() as cur:
        cur.executemany(INTENT_SQL, rows)        # persist intent BEFORE any submit
    net = sum(leg["qty"] * leg["price"] for leg in longs) - sum(leg["qty"] * leg["price"] for leg in shorts)
    logger.info("REBALANCE %s | longs=%s shorts=%s | gross=%.0f net=%.0f | %s",
                ts.isoformat(), [leg["symbol"] for leg in longs], [leg["symbol"] for leg in shorts],
                gross, net, "DRY-RUN (logged, not submitted)" if DRY_RUN else "LIVE")
    if DRY_RUN:
        return
    # LIVE submit path (kill-switch, caps from fresh broker snapshot, marketable-limit,
    # reconcile) is gated until the live-scoring path is validated — not enabled yet.
    logger.warning("LIVE submit not yet enabled; intent logged only")


def reconcile(conn: psycopg.Connection) -> None:
    """Read-only broker-truth probe (re-added — the prior recon loop was dropped in the
    f4ed85d rewrite). Fetches live broker positions; in dry-run we submit nothing, so the
    expected book is FLAT and any broker position is drift. Writes to reconciliation_log
    so a silent desync surfaces. This is our only broker-truth signal until the live path."""
    positions = trading.get_all_positions()
    broker = {p.symbol: float(p.qty) for p in positions}
    expected: dict[str, float] = {}               # dry-run: nothing submitted -> flat
    ok = (broker == expected)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reconciliation_log (ts, ok, detail) VALUES (now(), %s, %s)",
            (ok, json.dumps({"mode": "dry_run" if DRY_RUN else MODE,
                             "broker_positions": broker, "expected": expected})),
        )
    if not ok:
        logger.warning("reconcile DRIFT: broker holds %s (expected flat in dry-run)", broker)


def main() -> None:
    logger.info("executor starting: mode=%s dry_run=%s model=%s K=%d/%d notional=%.0f",
                MODE, DRY_RUN, MODEL_VERSION, K_LONG, K_SHORT, NOTIONAL_PER_NAME)
    last_rebalanced: datetime | None = None
    while True:
        try:
            with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
                reconcile(conn)                   # broker-truth probe every cycle
                ts = latest_prediction_ts(conn)
                if ts is None:
                    pass
                elif ts == last_rebalanced:
                    pass
                elif datetime.now(timezone.utc) - ts > timedelta(minutes=STALENESS_MAX_MIN):
                    logger.info("latest prediction %s is stale (> %dm); not trading",
                                ts.isoformat(), STALENESS_MAX_MIN)
                    last_rebalanced = ts          # don't spam; wait for fresh preds
                else:
                    rebalance(conn, ts)
                    last_rebalanced = ts
        except (psycopg.Error, ValueError, KeyError, APIError) as exc:
            logger.error("cycle error: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
