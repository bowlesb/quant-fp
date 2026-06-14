"""Real-Alpaca capture adapter — connects via alpaca-py's StockDataStream and feeds the SHARED
``process_bars`` core (the same compute/store code as the mock; only the connection differs).

Alpaca delivers 1-minute bars one-at-a-time per symbol shortly after each minute closes; we batch by
minute and flush a completed minute to the core when the next minute's bars start arriving.
``STREAM_URL_OVERRIDE`` (env) can point at a protocol-faithful mock; unset = the real feed.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from quantlib.features.backfill_bars import backfill_daily
from quantlib.features.capture import DEFAULT_BUFFER_MINUTES, CaptureState, process_bars
from quantlib.features.loaders import load_reference
from quantlib.features.sharded_capture import process_reduce, route_minute, worker_main


def build_stream() -> StockDataStream:
    feed = DataFeed.SIP if os.environ.get("ALPACA_DATA_FEED", "sip").lower() == "sip" else DataFeed.IEX
    return StockDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        feed=feed,
        url_override=os.environ.get("STREAM_URL_OVERRIDE"),
    )


def run_capture(symbols: list[str], root: str, mode: str, window: int = DEFAULT_BUFFER_MINUTES, day: str | None = None) -> None:
    state = CaptureState()
    pending: dict = {"minute": None, "bars": []}
    # Load the slowly-changing snapshots ONCE at startup; held for the session so the sector/asset-flag
    # groups (reference) and the multi-day/prior-day groups (daily history) serve live off the SAME
    # frames the backfill + parity paths use. Daily needs the session date to anchor the prior close.
    snapshots = {"reference": load_reference()}
    if day is not None:
        snapshots["daily"] = backfill_daily(day, symbols)
    stream = build_stream()

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            process_bars(state, pending["bars"], root, mode, day, window, snapshots)
            pending["bars"] = []
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    stream.subscribe_bars(on_bar, *symbols)
    stream.run()


def run_sharded_capture(  # pragma: no cover (live multiprocess loop; logic is unit-tested in sharded_capture)
    symbols: list[str], root: str, mode: str, n_shards: int | None = None,
    window: int = DEFAULT_BUFFER_MINUTES, day: str | None = None,
) -> None:
    """Production scale-out: ONE reader owns the websocket and routes each completed minute to N
    persistent worker processes by hash(symbol); the reader runs the universe-wide reduce itself. Each
    worker computes the SAME group code on its shard (byte-identical to single-process — proven in
    tests/test_fp_sharding.py), and writes only its own symbols (partition-disjoint, no DB contention)."""
    n_shards = n_shards or max(1, (os.cpu_count() or 8) - 2)
    snapshots = {"reference": load_reference()}
    if day is not None:
        snapshots["daily"] = backfill_daily(day, symbols)
    queues = [mp.Queue() for _ in range(n_shards)]
    workers = [
        mp.Process(target=worker_main, args=(i, n_shards, queues[i], root, mode, window, day, snapshots), daemon=True)
        for i in range(n_shards)
    ]
    for worker in workers:
        worker.start()

    reduce_state = CaptureState()
    pending: dict = {"minute": None, "bars": []}
    stream = build_stream()

    def dispatch(bars: list[dict]) -> None:
        for shard_id, shard_bars in enumerate(route_minute(bars, n_shards)):
            if shard_bars:
                queues[shard_id].put(shard_bars)  # map: per-shard workers
        process_reduce(reduce_state, bars, root, mode, day, window)  # gather: universe-wide rank

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            dispatch(pending["bars"])
            pending["bars"] = []
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    stream.subscribe_bars(on_bar, *symbols)
    stream.run()


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.real_capture <sym,sym> <root> <real|mock> [day] [--sharded]")
    symbols, root, mode = sys.argv[1].split(","), sys.argv[2], sys.argv[3]
    day = sys.argv[4] if len(sys.argv) > 4 and not sys.argv[4].startswith("--") else None
    if "--sharded" in sys.argv:
        run_sharded_capture(symbols, root, mode, day=day)
    else:
        run_capture(symbols, root, mode, day=day)


if __name__ == "__main__":
    main()
