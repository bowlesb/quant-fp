"""24/7 CRYPTO live-feed capture — an ISOLATED validation harness that exercises the SAME production
pipeline (capture -> feature compute -> bus emit) as the equity ``live_capture``, but against Alpaca's
crypto websocket, which trades continuously. Equity capture only runs 06:30-13:00 PT on weekdays; crypto
gives us a LIVE feed any time (nights / weekends / holidays) to validate fc / features / latency / bus.

Design (deliberately isolated from the equity feature-computer so it can NEVER touch the live equity fc,
its 688-feature fingerprint, its store, or its bus):

* **Feed** — ``alpaca.data.live.CryptoDataStream`` (endpoint ``v1beta3/crypto/us``, ``CryptoFeed.US``).
  NO SIP, NO market-hours gating, runs 24/7. Same Alpaca paper keys as equity (read from env).
* **Symbols** — a small liquid set (BTC/USD, ETH/USD, SOL/USD, ...). Alpaca crypto pairs use a SLASH
  (``BTC/USD``); we normalize to a slashless store/bus symbol (``BTCUSD``) so the bus stream key is
  ``fv:crypto:BTCUSD`` and the store partition is a clean symbol — and so it can never collide with an
  equity ticker.
* **Compute** — the IDENTICAL ``capture.process_bars`` core. We pass ONLY the ``minute_agg`` (+ optional
  ``trades``) input frames and NO ``reference`` / ``daily`` / ``universe`` snapshots, so ``runnable``
  self-selects exactly the UNIVERSAL bar/trade feature groups (price / volatility / range / volume /
  order-flow / candlestick / microstructure) and skips every equity-specific group (sector, breadth,
  peer_relative, multi-day, market-relative beta) — by construction, not by special-casing. The
  index-relative groups that key off SPY-in-the-frame (``market_context`` / ``market_beta``) are
  explicitly excluded since there is no crypto equity index.
* **Bus** — a SEPARATE namespace. ``BusPublisher(prefix="fv:crypto")`` -> ``fv:crypto:BTCUSD`` streams,
  so crypto vectors NEVER land on the equity ``fv:<symbol>`` streams or pollute the equity fingerprint.
* **Store** — a SEPARATE root (``/store`` mapped to its own volume) with ``mode="real"`` but a crypto
  source marker, so crypto partitions never mix with equity data.

Run inside the fp-dev image (separate ``crypto-capture`` container — NOT the equity ``feature-computer``)::

    docker run --rm --env-file .env --network quant_default \\
        -e FP_BUS=1 -e FP_CRYPTO_SYMBOLS="BTC/USD,ETH/USD,SOL/USD" \\
        -v fp_store_crypto:/store -v "$PWD":/app -w /app fp-dev \\
        python -m quantlib.features.crypto_capture /store

This is a TEST / VALIDATION harness (prove the pipeline runs live 24/7), NOT a crypto trading strategy.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import polars as pl
from alpaca.data.enums import CryptoFeed
from alpaca.data.live import CryptoDataStream

from quantlib.aggregates import TickState, TradeTick, bucket_minute
from quantlib.bus.publisher import BusPublisher
from quantlib.features import crypto_input_store, metrics
from quantlib.features.bus_hook import BusHook, bus_publish_enabled
from quantlib.features.capture import (
    DEFAULT_BUFFER_MINUTES,
    CaptureState,
    _bars_to_frame,
    process_bars,
)
from quantlib.features.tick_capture import enrich_bars_with_ticks, trades_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("crypto_capture")

# A small liquid crypto set — enough to exercise the pipeline with multiple symbols without overweighting
# this validation harness. Alpaca crypto pairs use a SLASH; FP_CRYPTO_SYMBOLS overrides (comma list).
DEFAULT_CRYPTO_SYMBOLS: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "DOGE/USD")

# The crypto bus namespace — completely separate from the equity ``fv:<symbol>`` streams so crypto
# vectors can never pollute the equity bus or the equity 688-feature fingerprint.
CRYPTO_BUS_PREFIX = "fv:crypto"

# Index-relative groups key off a SPY row inside ``minute_agg`` (there is none in crypto); exclude them so
# we emit only meaningful crypto features rather than all-null market-relative columns. The cross-sectional
# REDUCE groups (cross_sectional_rank / breadth) are not excluded here — cross_sectional_rank runs over the
# crypto symbol set fine; breadth self-skips (it needs the ``reference``/``daily`` frames we don't pass).
EXCLUDED_GROUPS: tuple[str, ...] = ("market_context", "market_beta")

# Crypto is the firehose-friendly case (a handful of liquid symbols), so we ALWAYS aggregate trades into
# the minute_agg tick columns + raw trades frame, lighting up the order-flow / microstructure groups.


def crypto_symbols() -> list[str]:
    """The crypto pairs to subscribe (``FP_CRYPTO_SYMBOLS`` overrides: a comma list of ``BASE/QUOTE``)."""
    env = os.environ.get("FP_CRYPTO_SYMBOLS", "").strip()
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return list(DEFAULT_CRYPTO_SYMBOLS)


def store_symbol(pair: str) -> str:
    """Normalize a slashed Alpaca crypto pair (``BTC/USD``) to a slashless store/bus symbol (``BTCUSD``)."""
    return pair.replace("/", "")


def build_stream() -> CryptoDataStream:
    """The 24/7 crypto websocket (v1beta3/crypto/us). Same Alpaca paper keys as equity (read from env)."""
    return CryptoDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        feed=CryptoFeed.US,
        url_override=os.environ.get("CRYPTO_STREAM_URL_OVERRIDE"),
    )


def aggregate_crypto_ticks(
    bars: list[dict],
    trades: list[dict],
    minute_epoch: int,
    tick_states: dict[str, TickState],
) -> tuple[list[dict], pl.DataFrame]:
    """Aggregate THIS minute's raw crypto trades into the bars (minute_agg tick columns) + a raw trades
    frame — the SAME aggregation the equity sharded worker does, so the order-flow / microstructure groups
    (trade_flow, signed_trade_ratio, inter_arrival, large_print_burst, microstructure_burst, tick_runlength,
    trade_size_dist) light up. Crypto has no NBBO quote stream here, so quotes are empty (the quote-spread
    columns stay honest-null), but trades are the rich crypto microstructure signal."""
    trades_by_symbol: dict[str, list[TradeTick]] = defaultdict(list)
    for trade in trades:
        if bucket_minute(trade["ts_epoch"]) == minute_epoch:
            trades_by_symbol[trade["S"]].append(
                TradeTick(ts_epoch=trade["ts_epoch"], price=trade["p"], size=trade["s"])
            )
    enriched = enrich_bars_with_ticks(bars, dict(trades_by_symbol), {}, tick_states)
    return enriched, trades_frame(dict(trades_by_symbol))


def run_crypto_capture(
    pairs: list[str], root: str, window: int = DEFAULT_BUFFER_MINUTES
) -> None:  # pragma: no cover (live websocket loop; the compute core + helpers are unit-tested)
    """Single-process crypto capture: own the crypto websocket, batch a completed minute, aggregate its
    trades, run the universal feature groups, write ``source=crypto`` partitions, and publish the per-symbol
    vectors to the ``fv:crypto:*`` bus. One process is plenty — the crypto set is small (a few symbols),
    so no sharding is needed; this is a validation harness, not the 11k-ticker equity scale-out."""
    mode = "real"
    state = CaptureState()
    # No snapshots (reference/daily/universe) — the crypto path deliberately runs ONLY the universal
    # bar/trade groups (runnable self-selects), skipping every equity-specific group by construction.
    pending: dict = {"minute": None, "bars": [], "trades": [], "arrival": 0.0, "last_arrival": 0.0}
    tick_states: dict[str, TickState] = {}

    bus_on = bus_publish_enabled()
    if bus_on:
        # Crypto vectors -> a SEPARATE bus namespace (fv:crypto:<SYMBOL>); the BusHook is shared code but
        # publishes through a crypto-prefixed publisher, so equity streams are never touched.
        state.bus_hook = BusHook(publisher=BusPublisher(prefix=CRYPTO_BUS_PREFIX))
        logger.info("crypto bus publishing ON -> %s:<SYMBOL> streams", CRYPTO_BUS_PREFIX)

    stream = build_stream()
    subscribe_pairs = list(pairs)

    def dispatch(minute: datetime) -> None:
        bars = pending["bars"]
        if not bars:
            return
        start = time.perf_counter()
        minute_epoch = bucket_minute(minute.timestamp())
        enriched, trades_df = aggregate_crypto_ticks(bars, pending["trades"], minute_epoch, tick_states)
        extra_frames = {"trades": trades_df} if trades_df.height else None
        # Persist THIS minute's compute INPUTS (opt-in) so the crypto parity sweep can recompute the
        # source=backfill side from the exact same minute_agg/trades the live path consumed (docs/CRYPTO_E2E.md
        # §3). Off by default — the live container starts persisting only on a sanctioned relaunch with
        # FP_CRYPTO_PERSIST_INPUTS=1. Persisting BEFORE process_bars (which mutates nothing here) keeps the
        # stored input byte-identical to the live compute input.
        if crypto_input_store.persist_inputs_enabled():
            day_str = str(minute.date())
            crypto_input_store.write_input(root, "minute_agg", day_str, _bars_to_frame(enriched), minute)
            if trades_df.height:
                crypto_input_store.write_input(root, "trades", day_str, trades_df, minute)
        # The SAME shared compute core as the equity path. snapshots=None + excluding the SPY-relative
        # groups => exactly the universal crypto-applicable feature set. mode="real" so the bus hook fires;
        # the crypto store source marker keeps the partitions isolated from equity (see store.source_for_mode).
        process_bars(
            state, enriched, root, mode, day=None, window=window,
            snapshots=None, exclude_groups=EXCLUDED_GROUPS, extra_frames=extra_frames,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.info(
            "minute=%s symbols=%d trades=%d compute_ms=%.0f groups=%d",
            minute.isoformat(), len(bars), len(pending["trades"]), elapsed_ms, len(state.group_timings),
        )

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            dispatch(pending["minute"])
            pending["bars"] = []
            pending["trades"] = []
        now = time.time()
        if not pending["bars"]:
            pending["arrival"] = now
        pending["last_arrival"] = now
        pending["minute"] = minute
        metrics.BARS_INGESTED.inc()
        pending["bars"].append(
            {"S": store_symbol(bar.symbol), "o": float(bar.open), "c": float(bar.close),
             "h": float(bar.high), "l": float(bar.low), "v": float(bar.volume),
             "t": bar.timestamp.replace(second=0, microsecond=0).isoformat()}
        )

    async def on_trade(trade) -> None:  # type: ignore[no-untyped-def]
        metrics.TRADES_INGESTED.inc()
        pending["trades"].append(
            {"S": store_symbol(trade.symbol), "p": float(trade.price), "s": float(trade.size),
             "ts_epoch": trade.timestamp.timestamp()}
        )

    metrics.start_metrics_server(int(os.environ.get("CRYPTO_METRICS_PORT", "9300")))
    stream.subscribe_bars(on_bar, *subscribe_pairs)
    stream.subscribe_trades(on_trade, *subscribe_pairs)
    logger.info(
        "crypto capture starting: pairs=%s store=%s window=%d bus=%s feed=v1beta3/crypto/us (24/7)",
        subscribe_pairs, root, window, "on" if bus_on else "off",
    )
    stream.run()


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "/store"
    pairs = crypto_symbols()
    logger.info("[crypto_capture] %d pairs -> store=%s (UTC now=%s)", len(pairs), root,
                datetime.now(timezone.utc).isoformat())
    run_crypto_capture(pairs, root=root)


if __name__ == "__main__":
    main()
