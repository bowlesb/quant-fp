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
from datetime import datetime

import polars as pl
import websockets

from quantlib.features import store
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.engine import run_group

BARS_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "volume": pl.Float64,
}


class CaptureState:
    """Rolling buffer + accumulated per-group output. Shared across any connection adapter."""

    def __init__(self) -> None:
        self.buffer: list[dict] = []
        self.accumulated: dict[str, pl.DataFrame] = {}  # one DEDUPED frame per group
        self.minutes = 0


def process_bars(state: CaptureState, bars: list[dict], root: str, mode: str, day: str | None, window: int) -> None:
    """The SHARED compute→store core (connection-agnostic). ``bars`` are normalized dicts with keys
    S, c, h, l, v, t — the parity boundary: both the mock JSON feed and the real Alpaca Bar objects
    normalize to this shape, then run through the identical code. Robust to RE-DELIVERED minutes
    (reconnect/replay): de-dups on (symbol, minute) keep-last so no duplicate cells corrupt parity."""
    for bar in bars:
        state.buffer.append(
            {"symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]), "open": float(bar["o"]),
             "close": float(bar["c"]), "high": float(bar["h"]), "low": float(bar["l"]), "volume": float(bar["v"])}
        )
    frame = pl.DataFrame(state.buffer, schema=BARS_SCHEMA).unique(subset=["symbol", "minute"], keep="last")
    recent = sorted(frame["minute"].unique())[-window:]
    frame = frame.filter(pl.col("minute").is_in(recent))
    state.buffer = frame.to_dicts()  # bound memory to the trailing window (de-duped)
    latest = frame["minute"].max()
    target_day = day or str(latest.date())
    ctx = BatchContext(frames={"minute_agg": frame})
    for group in runnable({"minute_agg": frame}):
        out = run_group(group, ctx, validate=False).filter(pl.col("minute") == latest)
        prior = state.accumulated.get(group.name)
        combined = out if prior is None else pl.concat([prior, out]).unique(subset=["symbol", "minute"], keep="last")
        state.accumulated[group.name] = combined
        store.write_group(root, group.name, group.version, "stream", target_day, combined, mode=mode)
    state.minutes += 1


async def capture(
    url: str, symbols: list[str], root: str, mode: str, window: int = 60, day: str | None = None
) -> int:
    """Websocket adapter (mock feed) → the shared core. Runs until the stream closes."""
    state = CaptureState()
    async with websockets.connect(url) as websocket:
        await websocket.send(json.dumps({"action": "subscribe", "symbols": symbols}))
        async for raw in websocket:
            bars = [b for b in json.loads(raw) if b.get("T") == "b"]
            if bars:
                process_bars(state, bars, root, mode, day, window)
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
