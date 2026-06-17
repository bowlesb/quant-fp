# Raw Market-Data Streams (`md:`)

The platform has **two** live Redis-Streams channels, kept strictly separate:

| Channel | Streams | Carries | Module |
|---------|---------|---------|--------|
| Feature-vector bus | `fv:<symbol>` | the packed, **fingerprinted** feature vector (one frame/symbol/min) | `quantlib/bus/{codec,publisher,consumer}.py` |
| **Raw market-data** | `md:<tier>:<symbol>` | the **raw inputs** — OHLCV bars + individual trades/quotes | `quantlib/bus/market_data.py` |

`md:` is **opt-in** and **fingerprint-neutral**: it touches none of the feature/group code and nothing on
the `fv:` `FVB1` fingerprint. It exists so a strategy container or the research / Modelling Agent can
subscribe to raw ticks and minute bars **live** — not just feature vectors — without re-deriving them.

## Streams

One stream per symbol per tier:

| Tier | Stream | Cadence | Frame | Gate |
|------|--------|---------|-------|------|
| per-minute | `md:bar:<symbol>` | 1/symbol/min | OHLCV bar (`MDB1`) | `FP_PUBLISH_MD` |
| per-minute | `md:trades:<symbol>` | 1/symbol/min | the minute's trades, batched (`MDT1`) | `FP_PUBLISH_MD` |
| per-minute | `md:quotes:<symbol>` | 1/symbol/min | the minute's quotes, batched (`MDQ1`) | `FP_PUBLISH_MD` |
| tick firehose | `md:tick_trades:<symbol>` | per trade (~2-4k/s live) | one trade (`MDT1`) | `FP_PUBLISH_TICKS` |
| tick firehose | `md:tick_quotes:<symbol>` | per quote (heavy) | one quote (`MDQ1`) | `FP_PUBLISH_TICKS` |

Every stream is trimmed to a bounded **approximate** `MAXLEN` (minute tiers ~4h; the firehose a short
rolling window), so total Redis memory is capped regardless of uptime.

Frames have their **own** compact little-endian magic (`MDB1` / `MDT1` / `MDQ1`) — wholly independent of
the `fv:` fingerprint. The tick payload is a contiguous `float64` matrix decoded as a zero-copy numpy
**view** (`[ts_us, price, size]` for trades; `[ts_us, bid, ask, bid_sz, ask_sz]` for quotes), mirroring
the `codec.py` style.

## Opt-in env flags (producer side: the capture reader)

Checked **once at reader startup** (cheap booleans on the hot path — no per-message string parsing). All
default OFF, so the live capture path is byte-for-byte unchanged until set.

| Flag | Default | Effect |
|------|---------|--------|
| `FP_PUBLISH_MD` | off | publish the per-minute tier (`md:bar` / `md:trades` / `md:quotes`) |
| `FP_PUBLISH_TICKS` | off | publish the tick firehose (`md:tick_trades` / `md:tick_quotes`) |
| `FP_TICK_SYMBOLS` | (canary set) | doubles as the firehose allowlist (comma list, or `all`); empty = all tick-subscribed symbols |

The firehose can only publish symbols that are actually tick-**subscribed** (you can't stream ticks you
never receive), so `FP_TICK_SYMBOLS` always intersects with the subscribed set. A microstructure strategy
that needs ticks for only a handful of names sets `FP_TICK_SYMBOLS=AAPL,MSFT` and gets just those.

### Hot-path safety

All `md:` publishing is **fault-isolated**: a Redis error logs a warning and continues — it never stalls
or crashes the capture hot path. It uses bounded `maxlen` with `approximate=True`, pipelines the
per-minute frames into one round-trip, and adds **zero overhead when the flags are off** (the whole thing
is guarded behind a boolean resolved once at startup; the publisher object is not even constructed).

## Consumer ergonomics (containers AND research)

`MarketDataConsumer` mirrors `BusConsumer.poll()` — declare the symbols + tiers you want and poll. You
only subscribe to what you ask for. It runs happily alongside a `BusConsumer` in the same strategy.

```python
from quantlib.bus.market_data import MarketDataConsumer

# A microstructure strategy: live minute bars + the trade firehose for two names.
md = MarketDataConsumer(symbols=["AAPL", "MSFT"], streams=["bar", "tick_trades"])
while True:
    for stream, record in md.poll(block_ms=1000):
        if stream == "bar":
            print(record.symbol, record.minute, record.close)       # BarRecord
        else:
            print(record.symbol, record.kind, record.rows.shape)    # TickBatch (zero-copy rows)
```

Run it next to the feature-vector bus in the same loop:

```python
from quantlib.bus.consumer import BusConsumer
from quantlib.bus.market_data import MarketDataConsumer

fv = BusConsumer(symbols=["AAPL"])                         # packed feature vectors
md = MarketDataConsumer(symbols=["AAPL"], streams=["bar"])  # raw bars
# poll both each tick; the channels are independent.
```

## Research / Modelling Agent: two access paths

The MA has **two** ways to reach raw market data:

1. **Historical (parquet)** — settled raw under `/store/raw/<tier>/symbol=.../date=...` (bars + ticks).
   This is the source of truth for backtests and for parity audits.
2. **Live (`md:`)** — `MarketDataConsumer` on the `md:` streams, the *same* raw shapes the per-minute /
   firehose tiers carry. Use this to prototype a live microstructure signal without waiting for the
   parquet to settle.

The live `md:` frames and the historical parquet carry the same raw fields, so a signal prototyped on one
transfers to the other.

## Self-test (no live producer)

`tests/test_market_data.py` includes a **synthetic** publish→consume round-trip (publishes known `md:`
frames to a private prefix and reads them back complete), mirroring `inspect_bus --synthetic` — so the
codec + transport are testable without the live producer or market hours. The Redis-dependent tests skip
cleanly when no broker is reachable.

## TO ENABLE IN PRODUCTION

`md:` is **off** on the live system. To turn it on:

1. Set the flag(s) on the **feature-computer** (the capture reader) service environment:
   - per-minute tier: `FP_PUBLISH_MD=1`
   - tick firehose: `FP_PUBLISH_TICKS=1` (plus `FP_TICK_SYMBOLS=...` to scope the firehose)
2. **Restart** the feature-computer so the reader re-reads the flags at startup.

Enabling the firehose for many symbols is heavy (~2-4k frames/s) — scope it with `FP_TICK_SYMBOLS` and
roll out deliberately. The per-minute tier is cheap (~1 publish/symbol/min).
