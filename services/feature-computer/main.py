"""Live feature-computer: each minute, compute feature_vectors (source='stream')
for the configured symbols from the just-closed minute's stored bars/aggregates,
via the shared quantlib.featurestore (the same code the historical builder uses).

Because it reads from the DB at minute close — not from any in-process live state
— the live vectors are reproducible by a later historical rebuild, which the
validate-features replay-equivalence check confirms.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg

from quantlib.featurestore import build_feature_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("feature-computer")

# Compute features for the symbols that have full trades/quotes (so all features
# are populated). Defaults to the live trade/quote subset.
SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get(
        "FEATURE_SYMBOLS", "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,SPY,QQQ,JPM"
    ).split(",")
    if s.strip()
]
LOOP_SECONDS = int(os.environ.get("FC_LOOP_SECONDS", "20"))
# Lag behind real time so bars + minute aggregates have settled before we compute.
SETTLE_MINUTES = int(os.environ.get("FC_SETTLE_MINUTES", "2"))

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def target_minute() -> datetime:
    now = datetime.now(timezone.utc)
    floored = now.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=SETTLE_MINUTES)


def main() -> None:
    logger.info("feature-computer starting: %d symbols, settle=%dm", len(SYMBOLS), SETTLE_MINUTES)
    last_done: datetime | None = None
    while True:
        try:
            minute = target_minute()
            if minute != last_done:
                with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
                    count = build_feature_store(
                        conn, SYMBOLS, minute, minute, "stream", "stream"
                    )
                if count:
                    logger.info("computed %d feature vectors for %s", count, minute.isoformat())
                last_done = minute
        except psycopg.Error as exc:
            logger.error("cycle error: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
