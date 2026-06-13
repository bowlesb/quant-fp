"""Live capture client — buffers per-minute bars from a websocket stream, computes the bar features
over a trailing window, and writes ``source=stream`` to the store.

SAME code for mock and real: the URL selects the feed (a local mock vs the real data websocket), and
the store root's mode marker ('mock'|'real') guarantees simulated data never lands in the real
store. This is the FP2 live path and the Monday capture service.
"""
from __future__ import annotations

import json
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
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
}


async def capture(
    url: str, symbols: list[str], root: str, mode: str, window: int = 60, day: str | None = None
) -> int:
    """Stream → store loop. Returns the number of minutes captured. Runs until the stream closes."""
    buffer: list[dict] = []
    accumulated: dict[str, list[pl.DataFrame]] = {}
    minutes = 0
    async with websockets.connect(url) as websocket:
        await websocket.send(json.dumps({"action": "subscribe", "symbols": symbols}))
        async for raw in websocket:
            bars = [b for b in json.loads(raw) if b.get("T") == "b"]
            if not bars:
                continue
            for bar in bars:
                buffer.append(
                    {"symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]),
                     "close": float(bar["c"]), "high": float(bar["h"]), "low": float(bar["l"])}
                )
            frame = pl.DataFrame(buffer, schema=BARS_SCHEMA)
            recent = sorted(frame["minute"].unique())[-window:]
            frame = frame.filter(pl.col("minute").is_in(recent))
            buffer = frame.to_dicts()  # bound memory to the trailing window
            latest = frame["minute"].max()
            target_day = day or str(latest.date())
            ctx = BatchContext(frames={"minute_agg": frame})
            for group in runnable({"minute_agg": frame}):
                out = run_group(group, ctx, validate=False).filter(pl.col("minute") == latest)
                accumulated.setdefault(group.name, []).append(out)
                store.write_group(
                    root, group.name, group.version, "stream", target_day,
                    pl.concat(accumulated[group.name]), mode=mode,
                )
            minutes += 1
    return minutes
