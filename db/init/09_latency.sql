-- Per-symbol latency drill-down — the bounded "WHICH tickers were too slow this minute" record behind
-- the aggregate bar->vector Prometheus histograms. We CANNOT put 11k symbols x per-minute into Prometheus
-- (unbounded cardinality), so each sharded capture worker keeps only the TOP-K (=20) slowest symbols per
-- (shard, minute) and writes those rows here, best-effort and off the hot path.
--
-- The two columns separate the CAUSE of slowness so an operator can tell the difference:
--   arrival_lag_s   = symbol-bar-arrival - minute-boundary  -> how late ALPACA delivered that bar.
--   total_latency_s = vector-ready - symbol-bar-arrival     -> our end-to-end for that symbol.
-- A high arrival_lag_s with low total_latency_s = Alpaca delivered late (not us); the reverse = our
-- pipeline is the bottleneck for that name.
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
