"""Live capture client — buffers per-minute bars from a websocket stream, computes the bar features
over a trailing window, and writes ``source=stream`` to the store.

SAME code for mock and real: the URL selects the feed (a local mock vs the real data websocket), and
the store root's mode marker ('mock'|'real') guarantees simulated data never lands in the real
store. This is the FP2 live path and the Monday capture service.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime

import polars as pl
import websockets

from quantlib.features import metrics, store
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable

BARS_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "volume": pl.Float64,
}

# The trailing buffer MUST exceed the largest declared feature window plus the deepest intra-feature
# lag. Below that, the buffer's leading-edge minutes lack the lookback/lag context the settled
# backfill has (e.g. their one-minute return is null live but defined in backfill), so the longest-
# window features diverge live-vs-backfill — a parity break. Our longest minute window today is
# price_levels' 240m; 300 leaves headroom. (The separate latest-minute compute mode removes the
# per-minute cost of recomputing this whole buffer at 10k-ticker scale.)
DEFAULT_BUFFER_MINUTES = 300


class CaptureState:
    """Rolling buffer + accumulated per-group output. Shared across any connection adapter."""

    def __init__(self) -> None:
        self.buffer: pl.DataFrame | None = None  # trailing-window bars, kept AS a frame (no per-minute round-trip)
        self.accumulated: dict[str, pl.DataFrame] = {}  # one DEDUPED frame per group
        self.minutes = 0
        self.group_timings: dict[str, float] = {}  # last per-group compute ms (first-class live timing)


def process_bars(
    state: CaptureState,
    bars: list[dict],
    root: str,
    mode: str,
    day: str | None,
    window: int,
    snapshots: dict[str, pl.DataFrame] | None = None,
    exclude_groups: tuple[str, ...] = (),
    only_groups: tuple[str, ...] | None = None,
    write: bool = True,
    shard: int | None = None,
    accumulate: bool = False,
) -> None:
    """The SHARED compute→store core (connection-agnostic). ``bars`` are normalized dicts with keys
    S, o, c, h, l, v, t — the parity boundary: both the mock JSON feed and the real Alpaca Bar objects
    normalize to this shape, then run through the identical code. Robust to RE-DELIVERED minutes
    (reconnect/replay): de-dups on (symbol, minute) keep-last so no duplicate cells corrupt parity.

    ``snapshots`` are slowly-changing reference frames (e.g. ``reference`` for sector/flags, ``daily``
    for multi-day) loaded once and held by the caller, merged into the compute context each minute so
    those groups self-select and serve live — the same frames the backfill/parity path provides.

    ``exclude_groups`` are computed elsewhere — in the sharded executor the cross-sectional reduce
    groups (universe-wide rank) run in a gather phase, not per shard. ``write=False`` skips the store
    (used by the gather phase). ``accumulate=True`` also keeps each group's outputs concatenated in
    ``state.accumulated`` — the in-memory cross-minute record the sharding parity test inspects; it is
    OFF in production because the durable record is the per-minute store files, not RAM."""
    # Build ONLY the new minute's bars into a frame and concat onto the kept buffer — keeping the buffer
    # as a DataFrame avoids round-tripping the whole trailing window through list[dict] every minute (at
    # 10k symbols x window that round-trip dominated the reader/worker per-minute cost). concat([old,new])
    # + unique(keep="last") preserves re-delivery dedup (the new copy of a minute wins).
    new_frame = pl.DataFrame(
        [
            {"symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]), "open": float(bar["o"]),
             "close": float(bar["c"]), "high": float(bar["h"]), "low": float(bar["l"]), "volume": float(bar["v"])}
            for bar in bars
        ],
        schema=BARS_SCHEMA,
    )
    frame = new_frame if state.buffer is None else pl.concat([state.buffer, new_frame])
    frame = frame.unique(subset=["symbol", "minute"], keep="last")
    recent = sorted(frame["minute"].unique())[-window:]
    frame = frame.filter(pl.col("minute").is_in(recent))
    state.buffer = frame  # bound to the trailing window (de-duped), kept as a frame for the next minute
    latest = frame["minute"].max()
    target_day = day or str(latest.date())
    frames = {"minute_agg": frame, **(snapshots or {})}
    ctx = BatchContext(frames=frames)
    for group in runnable(frames):
        if group.name in exclude_groups or (only_groups is not None and group.name not in only_groups):
            continue
        group_start = time.perf_counter()
        out = group.compute_latest(ctx)  # live = aggregate-at-T (fast where overridden; parity-guarded)
        state.group_timings[group.name] = (time.perf_counter() - group_start) * 1000.0
        if accumulate:
            prior = state.accumulated.get(group.name)
            state.accumulated[group.name] = (
                out if prior is None else pl.concat([prior, out]).unique(subset=["symbol", "minute"], keep="last")
            )
        if write:
            # Append THIS minute only (minute-keyed file) — O(1) per tick, idempotent on re-delivery.
            store.write_group(root, group.name, group.version, "stream", target_day, out, mode=mode, shard=shard, minute=latest)
    metrics.record_group_timings(state.group_timings)  # -> Prometheus histogram, graphed per-group in Grafana
    state.minutes += 1


async def capture(
    url: str,
    symbols: list[str],
    root: str,
    mode: str,
    window: int = DEFAULT_BUFFER_MINUTES,
    day: str | None = None,
    snapshots: dict[str, pl.DataFrame] | None = None,
) -> int:
    """Websocket adapter (mock feed) → the shared core. Runs until the stream closes."""
    state = CaptureState()
    async with websockets.connect(url) as websocket:
        await websocket.send(json.dumps({"action": "subscribe", "symbols": symbols}))
        async for raw in websocket:
            bars = [b for b in json.loads(raw) if b.get("T") == "b"]
            if bars:
                process_bars(state, bars, root, mode, day, window, snapshots)
    return state.minutes


def main() -> None:
    if len(sys.argv) < 5:
        raise SystemExit("usage: python -m quantlib.features.capture <url> <sym,sym> <root> <mock|real> [day]")
    url, symbols, root, mode = sys.argv[1], sys.argv[2].split(","), sys.argv[3], sys.argv[4]
    day = sys.argv[5] if len(sys.argv) > 5 else None
    count = asyncio.run(capture(url, symbols, root, mode, day=day))
    print(f"captured {count} minutes -> {root} (mode={mode})")


if __name__ == "__main__":
    main()
