-- Per-symbol latency drill-down — the bounded "WHICH tickers were delivered latest this minute" record
-- behind the aggregate bar->vector Prometheus histograms. We CANNOT put 11k symbols x per-minute into
-- Prometheus (unbounded cardinality), so each sharded capture worker keeps only the TOP-K (=20) LATEST-
-- DELIVERED symbols per (shard, minute) and writes those rows here, best-effort and off the hot path.
--
-- Rows are ranked by arrival_lag_s (the latest-delivered symbols), the only genuinely per-symbol signal:
--   arrival_lag_s   = symbol-bar-arrival - minute-boundary  -> how late ALPACA delivered that bar.
--   total_latency_s = vector-ready - symbol-bar-arrival     -> recorded for CONTEXT only, NOT ranked on.
-- total_latency_s is NOT per-symbol attributable (vector-ready is one shard-level instant; our compute is
-- per-shard) and is gated on the next-minute dispatch trigger (saturates ~60s in sparse hours), so ranking
-- by it is misleading. For per-shard compute use the feature_shard_compute_seconds Prometheus histogram.
CREATE TABLE IF NOT EXISTS latency_slow_symbols (
    minute          timestamptz      NOT NULL,
    shard           int              NOT NULL,
    symbol          text             NOT NULL,
    arrival_lag_s   double precision NOT NULL,
    total_latency_s double precision NOT NULL,
    captured_at     timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (minute, shard, symbol)
);
CREATE INDEX IF NOT EXISTS idx_latency_slow_minute ON latency_slow_symbols (minute);
CREATE INDEX IF NOT EXISTS idx_latency_slow_total ON latency_slow_symbols (total_latency_s);
