"""Real-Alpaca capture adapter — connects via alpaca-py's StockDataStream and feeds the SHARED
``process_bars`` core (the same compute/store code as the mock; only the connection differs).

Alpaca delivers 1-minute bars one-at-a-time per symbol shortly after each minute closes; we batch by
minute and flush a completed minute to the core when the next minute's bars start arriving.
``STREAM_URL_OVERRIDE`` (env) can point at a protocol-faithful mock; unset = the real feed.
"""
from __future__ import annotations

import os
import sys

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from quantlib.features.capture import CaptureState, process_bars


def build_stream() -> StockDataStream:
    feed = DataFeed.SIP if os.environ.get("ALPACA_DATA_FEED", "sip").lower() == "sip" else DataFeed.IEX
    return StockDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        feed=feed,
        url_override=os.environ.get("STREAM_URL_OVERRIDE"),
    )


def run_capture(symbols: list[str], root: str, mode: str, window: int = 60, day: str | None = None) -> None:
    state = CaptureState()
    pending: dict = {"minute": None, "bars": []}
    stream = build_stream()

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            process_bars(state, pending["bars"], root, mode, day, window)
            pending["bars"] = []
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "c": float(bar.close), "h": float(bar.high), "l": float(bar.low), "t": bar.timestamp.isoformat()}
        )

    stream.subscribe_bars(on_bar, *symbols)
    stream.run()


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.real_capture <sym,sym> <root> <real|mock> [day]")
    run_capture(sys.argv[1].split(","), sys.argv[2], sys.argv[3], day=sys.argv[4] if len(sys.argv) > 4 else None)


if __name__ == "__main__":
    main()
