"""Ingestor entrypoint: sharded Alpaca SIP websocket -> TimescaleDB (M2 topology A).

One READER process owns the single Alpaca market-data websocket (one connection per
account is the hard limit) and routes ticks to N WORKER processes by symbol-hash.
Each worker owns a symbol shard and does the CPU-heavy per-minute quantlib aggregation
+ DB writes for it — the parity cornerstone, identical to the single-process path,
just scoped to a shard. Bars stream for the whole universe (the reader persists them
and forwards minute-close signals to workers); trades/quotes stream for the top-ADV
OFI names (the order-flow tier), partitioned across the workers.

This scales 50 -> >=500 trade/quote names: at 500 a single asyncio process doing
all the aggregation becomes CPU-bound and a slow minute-flush backs up the receive
loop, dropping ticks. Sharding the aggregation across worker processes removes that
bottleneck; the reader stays light (receive + route only).

A live coverage invariant (streamed == subscribed, alarmed) runs per shard from day
one so a capture regression at scale is caught immediately, not in a later backfill
diff. See services/ingestor/coverage.py.
"""
import logging
import os
import signal
import sys
import time
from multiprocessing import Event, Process
from multiprocessing.synchronize import Event as EventType

from app_ingestor.reader import run_reader
from app_ingestor.shard import make_queues
from app_ingestor.subscription import load_subscription
from app_ingestor.worker import run_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingestor")

N_SHARDS = int(os.environ.get("INGESTOR_SHARDS", "4"))
QUEUE_MAXSIZE = int(os.environ.get("INGESTOR_QUEUE_MAXSIZE", "200000"))
SUPERVISE_INTERVAL_S = 5.0

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def spawn_worker(
    shard_id: int,
    queue: object,
    expected_symbols: list[str],
    stop: EventType,
) -> Process:
    proc = Process(
        target=run_worker,
        args=(shard_id, queue, DB_KWARGS, expected_symbols, stop),
        name=f"worker-{shard_id}",
        daemon=False,
    )
    proc.start()
    return proc


def main() -> None:
    bar_symbols, ofi_symbols, shard_symbol_lists = load_subscription(
        DB_KWARGS, N_SHARDS
    )
    logger.info(
        "ingestor (sharded): bars=%d ofi=%d shards=%d (per-shard %s)",
        len(bar_symbols), len(ofi_symbols), N_SHARDS,
        [len(shard) for shard in shard_symbol_lists],
    )

    queues = make_queues(N_SHARDS, QUEUE_MAXSIZE)
    stop: EventType = Event()

    workers: list[Process] = [
        spawn_worker(shard_id, queues[shard_id], shard_symbol_lists[shard_id], stop)
        for shard_id in range(N_SHARDS)
    ]

    reader = Process(
        target=run_reader,
        args=(queues, bar_symbols, ofi_symbols, DB_KWARGS),
        name="reader",
        daemon=False,
    )
    reader.start()

    shutdown_clean = {"value": False}

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("signal %d -> graceful shutdown", signum)
        shutdown_clean["value"] = True
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # A dead worker can't be surgically respawned onto its existing queue: a process
    # killed mid-get() dies holding the mp.Queue read lock, permanently wedging that
    # queue for any replacement consumer (verified in the dry-run — the respawned
    # worker drained 0 messages, silently losing its shard). The only correct recovery
    # with the mp substrate is a FULL restart with fresh queues, so on ANY child death
    # we exit non-zero and let `restart: unless-stopped` rebuild the whole topology.
    # The ~1-minute stream gap is recoverable via the backfill (source='backfill') and
    # visible in the coverage gauges; a silently-wedged shard would not be. (If a
    # future multi-host substrate with consumer-group semantics lands, single-worker
    # respawn becomes safe again — that's the swappable-seam payoff.)
    while not stop.is_set():
        time.sleep(SUPERVISE_INTERVAL_S)
        if not reader.is_alive():
            logger.error("reader died (exit %s) -> full ingestor restart", reader.exitcode)
            stop.set()
            break
        for shard_id, proc in enumerate(workers):
            if not proc.is_alive():
                logger.error(
                    "worker %d died (exit %s) -> full ingestor restart (mp queue can't "
                    "be safely reused after a consumer death)",
                    shard_id, proc.exitcode,
                )
                stop.set()
                break

    if reader.is_alive():
        reader.terminate()
    reader.join(timeout=10)
    for proc in workers:
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()

    if shutdown_clean["value"]:
        logger.info("ingestor stopped (clean shutdown)")
        return
    # Abnormal stop (a child died) — exit non-zero so restart:unless-stopped rebuilds
    # the whole topology with fresh queues. Exiting 0 here would leave the container
    # stopped with a wedged queue.
    logger.error("ingestor exiting non-zero to force a clean full restart")
    sys.exit(1)


if __name__ == "__main__":
    main()
