-- Quant trading system schema. Applied on first DB boot.
-- Design notes in ARCHITECTURE.md. Principle: append-only, fully reconstructable.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Market data: 1-minute bars (permanent)
-- ---------------------------------------------------------------------------
CREATE TABLE bars_1m (
    symbol       text        NOT NULL,
    ts           timestamptz NOT NULL,            -- bar open time, UTC
    open         double precision NOT NULL,
    high         double precision NOT NULL,
    low          double precision NOT NULL,
    close        double precision NOT NULL,
    volume       bigint           NOT NULL,
    vwap         double precision,
    trade_count  integer,
    source       text        NOT NULL DEFAULT 'stream',  -- stream | backfill | repair
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, source)
);
SELECT create_hypertable('bars_1m', 'ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE bars_1m SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');
SELECT add_compression_policy('bars_1m', INTERVAL '7 days');

-- ---------------------------------------------------------------------------
-- Per-minute NBBO quote aggregates (permanent; raw quotes not stored)
-- ---------------------------------------------------------------------------
CREATE TABLE quote_agg_1m (
    symbol            text        NOT NULL,
    ts                timestamptz NOT NULL,
    mean_spread_bps   double precision,
    median_spread_bps double precision,
    mean_bid_size     double precision,
    mean_ask_size     double precision,
    quote_imbalance   double precision,           -- (bid_sz - ask_sz)/(bid_sz + ask_sz)
    n_quotes          integer,
    source            text        NOT NULL DEFAULT 'stream',
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, source)
);
SELECT create_hypertable('quote_agg_1m', 'ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE quote_agg_1m SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');
SELECT add_compression_policy('quote_agg_1m', INTERVAL '7 days');

-- ---------------------------------------------------------------------------
-- Per-minute trade (tick) aggregates (permanent)
-- ---------------------------------------------------------------------------
CREATE TABLE trade_agg_1m (
    symbol           text        NOT NULL,
    ts               timestamptz NOT NULL,
    signed_volume    double precision,            -- tick-rule buy minus sell volume
    buy_volume       double precision,
    sell_volume      double precision,
    large_print_cnt  integer,                     -- prints above a size threshold
    trade_intensity  double precision,            -- trades per second
    median_size      double precision,
    p95_size         double precision,
    n_trades         integer,
    source           text        NOT NULL DEFAULT 'stream',
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, source)
);
SELECT create_hypertable('trade_agg_1m', 'ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE trade_agg_1m SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');
SELECT add_compression_policy('trade_agg_1m', INTERVAL '7 days');

-- ---------------------------------------------------------------------------
-- Raw trade prints (30-day rolling; for debugging and new-aggregate dev)
-- ---------------------------------------------------------------------------
CREATE TABLE trades_raw (
    symbol      text        NOT NULL,
    ts          timestamptz NOT NULL,
    price       double precision NOT NULL,
    size        double precision NOT NULL,
    exchange    text,
    conditions  text,
    tape        text,
    ingested_at timestamptz NOT NULL DEFAULT now()
);
SELECT create_hypertable('trades_raw', 'ts', chunk_time_interval => INTERVAL '1 hour');
ALTER TABLE trades_raw SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');
SELECT add_compression_policy('trades_raw', INTERVAL '1 day');
SELECT add_retention_policy('trades_raw', INTERVAL '30 days');

-- ---------------------------------------------------------------------------
-- News (permanent, small)
-- ---------------------------------------------------------------------------
CREATE TABLE news (
    id          bigint      PRIMARY KEY,          -- Alpaca article id
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz,
    headline    text,
    summary     text,
    source      text,
    url         text,
    symbols     text[],
    ingested_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX news_created_idx ON news (created_at);
CREATE INDEX news_symbols_idx ON news USING gin (symbols);

-- ---------------------------------------------------------------------------
-- Point-in-time universe membership
-- ---------------------------------------------------------------------------
CREATE TABLE universe_membership (
    trade_date       date    NOT NULL,
    symbol           text    NOT NULL,
    in_universe      boolean NOT NULL,
    adv_dollar       double precision,            -- average daily $ volume
    median_spread_bps double precision,
    price            double precision,
    PRIMARY KEY (trade_date, symbol)
);

-- ---------------------------------------------------------------------------
-- Feature sets and computed feature vectors
-- ---------------------------------------------------------------------------
CREATE TABLE feature_sets (
    version    text        PRIMARY KEY,           -- e.g. v1.0.0
    names      text[]      NOT NULL,              -- ordered feature names; vector[i] <-> names[i]
    created_at timestamptz NOT NULL DEFAULT now(),
    notes      text
);

CREATE TABLE feature_vectors (
    symbol      text        NOT NULL,
    ts          timestamptz NOT NULL,
    set_version text        NOT NULL REFERENCES feature_sets(version),
    vector      double precision[] NOT NULL,      -- ordered to match feature_sets.names
    source      text        NOT NULL DEFAULT 'stream',  -- stream | historical
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, set_version, source)
);
SELECT create_hypertable('feature_vectors', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Forward labels (kept separate so we never accidentally feed them as inputs)
CREATE TABLE labels (
    symbol     text        NOT NULL,
    ts         timestamptz NOT NULL,
    horizon    text        NOT NULL,              -- e.g. fwd_30m, fwd_overnight
    value      double precision NOT NULL,         -- excess return vs universe
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, horizon)
);
SELECT create_hypertable('labels', 'ts', chunk_time_interval => INTERVAL '1 day');

-- ---------------------------------------------------------------------------
-- Model predictions
-- ---------------------------------------------------------------------------
CREATE TABLE predictions (
    symbol        text        NOT NULL,
    ts            timestamptz NOT NULL,
    model_version text        NOT NULL,
    horizon       text        NOT NULL,
    score         double precision NOT NULL,
    rank          integer,
    decile        integer,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, model_version, horizon)
);
SELECT create_hypertable('predictions', 'ts', chunk_time_interval => INTERVAL '1 day');

-- ---------------------------------------------------------------------------
-- Orders, fills, reconciliation (the audit trail)
-- ---------------------------------------------------------------------------
CREATE TABLE orders_log (
    client_order_id   text PRIMARY KEY,
    symbol            text NOT NULL,
    side              text NOT NULL,              -- buy | sell
    qty               numeric NOT NULL,
    order_type        text NOT NULL,
    limit_price       numeric,
    mode              text NOT NULL,              -- paper | live
    intended_at       timestamptz NOT NULL,
    submitted_at      timestamptz,
    alpaca_order_id   text,
    status            text,
    features_snapshot jsonb,                      -- exactly what the model saw
    prediction        double precision,
    model_version     text,
    nbbo_bid          numeric,                    -- live NBBO at submit (arrival benchmark)
    nbbo_ask          numeric,
    nbbo_mid          numeric                     -- arrival mid: per-leg slippage reference
);
CREATE INDEX orders_intended_idx ON orders_log (intended_at);

CREATE TABLE fills_log (
    alpaca_order_id text NOT NULL,
    fill_ts         timestamptz NOT NULL,
    qty             numeric NOT NULL,
    price           numeric NOT NULL,
    symbol          text,                              -- broker truth: which name filled
    side            text,                              -- 'buy' | 'sell' (for signed cashflow)
    PRIMARY KEY (alpaca_order_id, fill_ts)
);

-- Realized P&L per name per day: signed cashflow over every fill (entry AND EOD-flatten
-- exit). For a fully round-tripped name (bought_qty == sold_qty) realized_pnl is the net
-- realized dollars; summed across names it reconciles to pnl_daily.day_pnl (modulo fees).
CREATE VIEW realized_pnl_by_name AS
SELECT fill_ts::date AS day,
       symbol,
       round(sum(CASE WHEN side = 'sell' THEN qty * price ELSE -qty * price END), 2) AS realized_pnl,
       sum(CASE WHEN side = 'buy'  THEN qty ELSE 0 END) AS bought_qty,
       sum(CASE WHEN side = 'sell' THEN qty ELSE 0 END) AS sold_qty,
       count(*) AS n_fills
FROM fills_log
WHERE symbol IS NOT NULL
GROUP BY 1, 2;

-- Per-leg execution slippage = our MEASURED one-way cost, directly comparable to the
-- battery's assumed cost_bps_oneway=2.0. slippage_bps = signed (fill - arrival_mid)/mid in
-- bps; positive = cost paid (bought above / sold below the arrival mid). arrival_mid is the
-- live NBBO mid captured at submit (arrival_src='nbbo'); for legs predating that capture it
-- falls back to the bars_1m close at the submit minute (arrival_src='bar_proxy').
CREATE VIEW execution_slippage AS
SELECT o.intended_at::date AS day, o.symbol, o.side, f.qty, f.price AS fill_price,
       o.limit_price, COALESCE(o.nbbo_mid, bar.close::numeric) AS arrival_mid,
       CASE WHEN o.nbbo_mid IS NOT NULL THEN 'nbbo' ELSE 'bar_proxy' END AS arrival_src,
       round((CASE WHEN o.side = 'buy' THEN f.price - COALESCE(o.nbbo_mid, bar.close::numeric)
                   ELSE COALESCE(o.nbbo_mid, bar.close::numeric) - f.price END)
             / NULLIF(COALESCE(o.nbbo_mid, bar.close::numeric), 0) * 10000, 2) AS slippage_bps,
       round((CASE WHEN o.side = 'buy' THEN f.price - COALESCE(o.nbbo_mid, bar.close::numeric)
                   ELSE COALESCE(o.nbbo_mid, bar.close::numeric) - f.price END) * f.qty, 2) AS slippage_usd,
       o.submitted_at AS submit_ts          -- PIT key so the cost model can join ADV$/price per name
FROM orders_log o
JOIN fills_log f ON f.alpaca_order_id = o.alpaca_order_id
LEFT JOIN LATERAL (SELECT close FROM bars_1m b
                   WHERE b.symbol = o.symbol AND b.ts < date_trunc('minute', o.submitted_at)
                   ORDER BY b.ts DESC LIMIT 1) bar ON true
WHERE o.status = 'submitted';

-- Daily roll-up: oneway_cost_bps_* is the real number to feed long_short_backtest(cost_bps_oneway=).
CREATE VIEW execution_slippage_daily AS
SELECT day, count(*) AS n_legs,
       round(avg(slippage_bps), 2) AS oneway_cost_bps_mean,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY slippage_bps))::numeric, 2) AS oneway_cost_bps_median,
       round(sum(slippage_usd), 2) AS slippage_usd_total,
       round(sum(qty * fill_price), 2) AS gross_traded_usd
FROM execution_slippage GROUP BY day ORDER BY day;

CREATE TABLE reconciliation_log (
    ts     timestamptz PRIMARY KEY,
    ok     boolean NOT NULL,
    detail jsonb
);

-- ---------------------------------------------------------------------------
-- Data quality (daily coverage vs trading calendar)
-- ---------------------------------------------------------------------------
CREATE TABLE data_quality_daily (
    trade_date       date NOT NULL,
    symbol           text NOT NULL,
    expected_minutes integer,
    received_minutes integer,
    repaired_minutes integer,
    PRIMARY KEY (trade_date, symbol)
);
