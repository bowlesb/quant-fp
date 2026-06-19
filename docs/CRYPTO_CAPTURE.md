# 24/7 Crypto Capture — off-hours live pipeline validation

Crypto trades **continuously**. Alpaca's crypto websocket (`v1beta3/crypto/us`, `CryptoFeed.US`) has no
SIP and no market-hours gating, so it gives us a **live feed any time** — nights, weekends, holidays —
when the equity `feature-computer` is idle (equity capture only runs 06:30–13:00 PT on weekdays).

`crypto-capture` is a **validation / test harness**: it exercises the SAME production path as the equity
fc — **capture → feature compute → bus emit** — against this 24/7 feed, so we can validate the fc,
feature compute, latency, and the bus against a live feed at any hour. **It is not a crypto trading
strategy** (trading is separate/later).

## Isolation — it cannot touch the equity fc

By design it is fully isolated from the live equity feature-computer, its 688-feature fingerprint, its
store, and its bus:

| Concern | Equity fc | crypto-capture |
|---|---|---|
| Container | `feature-computer` | `crypto-capture` (separate) |
| Feed | SIP equity (`StockDataStream`) | `CryptoDataStream` (`v1beta3/crypto/us`, 24/7) |
| Store volume | `fp_store_real` | `fp_store_crypto` (separate) |
| Bus namespace | `fv:<SYMBOL>` | `fv:crypto:<SYMBOL>` (separate) |
| Metrics port | 9200 (+9201..) | 9300 |

The crypto path reuses `quantlib.features.capture.process_bars` **unmodified**. It passes only the
`minute_agg` (+ `trades`) input frames and **no** `reference` / `daily` / `universe` snapshots, so
`runnable()` self-selects exactly the universal bar/trade groups and skips every equity-specific group
*by construction*. The two SPY-relative groups (`market_context`, `market_beta`) are explicitly excluded
(there is no crypto equity index). No equity code, container, store, or stream is touched.

## Crypto-applicable feature families

The universal groups that compute on crypto bars + trades (36 groups, verified live):

- **Price / returns / momentum**: `price_returns`, `price_levels`, `price_volume`, `momentum`,
  `momentum_run`, `momentum_consistency`, `clean_momentum`, `return_dynamics`, `technical`,
  `round_levels`, `efficiency`, `trend_quality`, `residual_analysis`
- **Volatility / range**: `volatility`, `ohlc_vol`, `realized_range`, `distribution`, `swing`
- **Candles**: `candlestick`
- **Volume / liquidity**: `volume`, `volume_exhaustion`, `volume_leads_price`, `liquidity`,
  `trade_freq_z`
- **Order flow / microstructure** (from the crypto trade stream): `trade_flow`, `signed_trade_ratio`,
  `inter_arrival`, `large_print_burst`, `microstructure_burst`, `tick_runlength`, `trade_size_dist`
- **Cross-sectional**: `cross_sectional_rank` (over the crypto symbol set)
- **Calendar / time-of-day**: `calendar`, `calendar_events`, `intraday_seasonality`

**Not applicable** (correctly skipped — no `reference`/`daily` snapshots, no equity index, no NBBO quote
stream): `sector`, `breadth`, `peer_relative`, `multi_day_*`, `prior_day`, `daily_beta`,
`overnight_beta`, `market_context`, `market_beta`, `liquidity_rank`, and `quote_spread` (no crypto
quotes here → honest-null).

## Run it

```bash
docker build -f Dockerfile.fp -t fp-dev .                            # shared base image
docker compose -f docker-compose.crypto.yml up -d --build crypto-capture
docker logs -f crypto-capture                                        # per-minute compute lines
```

Symbols and bus toggle are env-driven (`docker-compose.crypto.yml`):

- `FP_CRYPTO_SYMBOLS="BTC/USD,ETH/USD,SOL/USD,LTC/USD,DOGE/USD"` — Alpaca slash pairs (normalized to a
  slashless store/bus symbol, e.g. `BTCUSD`).
- `FP_BUS=1` — publish per-symbol vectors to `fv:crypto:<SYMBOL>`.
- `CRYPTO_METRICS_PORT=9300` — Prometheus `/metrics` (ingestion counters, per-group compute timings).

### Inspect the live feed and bus

```bash
# ingestion counters (proves the 24/7 feed is delivering)
docker exec crypto-capture python -c \
  "import urllib.request as u;print(u.urlopen('http://localhost:9300/metrics').read().decode())" \
  | grep feature_.*_ingested_total

# crypto vectors on the isolated bus namespace
docker run --rm --network quant_default redis:7-alpine \
  redis-cli -h quant-redis --scan --pattern 'fv:crypto:*'
docker run --rm --network quant_default redis:7-alpine \
  redis-cli -h quant-redis xlen fv:crypto:BTCUSD
```

## What it validates

- The **production capture → compute → bus** path runs against a **live feed off-hours** (when equity is
  closed) — fc liveness, feature compute, and the bus, on a continuous feed.
- The **feature engine** computes the universal families on a different asset class (a strong generality
  / no-equity-assumption check).
- The **bus apparatus** (per-symbol streams, encode/XADD, bounded MAXLEN) under a separate namespace,
  consumable by an independent strategy/echo container exactly like the equity bus.
- **Latency** of bar-arrival → vector-ready on a live feed at any hour.

## Tested

- `tests/test_fp_crypto_capture.py` — symbol normalization, namespace isolation, env override, and that
  the shared core runs the universal groups and **no** equity-specific group on crypto bars+trades.
- Live run verified against Alpaca's crypto websocket: bars+trades ingested and per-symbol vectors
  emitted to `fv:crypto:BTCUSD` / `fv:crypto:SOLUSD`, with the equity `fv:*` streams untouched.
