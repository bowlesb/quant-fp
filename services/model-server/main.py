"""Model-server — the 'deploy/predict' step of the E2E slice.

Each rebalance cadence during RTH: compute live feature vectors for the current
universe via the SAME quantlib.featurestore code (source='live'), score them with
the saved LightGBM booster, and write rank/decile to the predictions table for the
executor to act on. Reusing the feature builder keeps live scoring identical to
training (the parity guarantee, end to end).
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import lightgbm as lgb
import numpy as np
import psycopg

from quantlib.featurestore import build_feature_store, load_membership

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("model-server")

MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
HORIZON = os.environ.get("MODEL_HORIZON", "fwd_30m")
CADENCE_MIN = int(os.environ.get("FEATURE_CADENCE_MIN", "30"))
SETTLE_MIN = int(os.environ.get("PREDICT_SETTLE_MIN", "2"))
LOOP_SECONDS = int(os.environ.get("PREDICT_LOOP_SECONDS", "30"))

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

booster = lgb.Booster(model_file=os.path.join(MODELS_DIR, f"model_{HORIZON}.txt"))
with open(os.path.join(MODELS_DIR, f"model_{HORIZON}.meta.json")) as f:
    META = json.load(f)
MODEL_VERSION = f"lgbm_{HORIZON}_{META['set_version']}"

PRED_SQL = """
INSERT INTO predictions (symbol, ts, model_version, horizon, score, rank, decile, created_at)
VALUES (%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (symbol, ts, model_version, horizon) DO UPDATE
SET score=EXCLUDED.score, rank=EXCLUDED.rank, decile=EXCLUDED.decile, created_at=now()
"""


def target_minute() -> datetime:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(minutes=SETTLE_MIN)


def score_minute(conn: psycopg.Connection, minute: datetime) -> int:
    membership = load_membership(conn)
    members = sorted(membership.get(datetime.now(timezone.utc).date(), set())) or \
        sorted({s for m in membership.values() for s in m})
    # Compute live feature vectors for this minute (source='live') via shared code.
    build_feature_store(conn, members, minute, minute, "stream", "live", membership, CADENCE_MIN)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT symbol, vector FROM feature_vectors
               WHERE ts=%s AND source='live' AND set_version=%s""",
            (minute, META["set_version"]),
        )
        rows = cur.fetchall()
    if not rows:
        return 0
    symbols = [r[0] for r in rows]
    X = np.array([[float(v) if v is not None else math.nan for v in r[1]] for r in rows], dtype=float)
    scores = booster.predict(X)
    order = np.argsort(-scores)                       # best score = rank 0
    n = len(scores)
    with conn.cursor() as cur:
        for rank, idx in enumerate(order):
            decile = min(9, int(rank * 10 / n))
            cur.execute(PRED_SQL, (symbols[idx], minute, MODEL_VERSION, HORIZON,
                                   float(scores[idx]), rank, decile))
    return n


def main() -> None:
    logger.info("model-server starting: model=%s cadence=%dm", MODEL_VERSION, CADENCE_MIN)
    last_scored: datetime | None = None
    while True:
        try:
            minute = target_minute()
            if minute != last_scored and minute.astimezone(timezone.utc).minute % CADENCE_MIN == 0:
                with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
                    n = score_minute(conn, minute)
                if n:
                    logger.info("scored %d symbols for %s", n, minute.isoformat())
                last_scored = minute
        except (psycopg.Error, KeyError, ValueError) as exc:
            logger.error("cycle error: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
