"""Per-feature-group compute-latency metrics for Prometheus / Grafana (definition-of-done #5).

Each sharded capture worker records its per-group ``compute_latest`` time every minute into a Prometheus
histogram labelled by group, and exposes ``/metrics`` on a per-shard port (WORKER_METRICS_BASE_PORT +
shard_id) — the same pattern the ingestor uses for coverage gauges. Grafana then graphs p50/p99 latency
PER GROUP, so a feature an agent just added that's slow shows up immediately on the dashboard. Mirrors
the prior Edgar system's FEATURE_GROUP_DURATION histogram.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram, start_http_server

# Buckets span 0.5ms .. 2.5s — the range a per-group live compute can plausibly take at shard scale.
GROUP_COMPUTE_SECONDS = Histogram(
    "feature_group_compute_seconds",
    "Per-group compute_latest wall time, by feature group",
    ["group"],
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# FIRST-bar anchor = END-TO-END latency incl. Alpaca's bar-delivery window, per shard. The wall-clock
# from the instant the capture process RECEIVED the minute's FIRST bar off the Alpaca websocket to the
# instant THIS shard's feature vector for that minute is assembled (BEFORE the parquet write — the write
# is post-bet and excluded).
#
# HONESTY CAVEAT: because the anchor is the minute's FIRST bar, this number CONFLATES two things: (a)
# Alpaca's per-minute bar-DELIVERY spread (bars stream in one-at-a-time over several seconds after each
# minute closes) and (b) OUR compute. At the widened ~11k-symbol universe the delivery spread dominates
# and pushed the value into the old 5s top bucket (p50=p95=p99=5.000s == overflow). Buckets now span
# 50ms .. 60s so multi-second end-to-end latency is actually measurable. To isolate OUR compute from the
# delivery spread, compare against feature_assemble_seconds (last-bar anchor) below.
#
# NOTE: Prometheus histograms cannot change buckets without recreating the metric, so this bucket change
# only takes effect after a CAPTURE PROCESS RESTART.
BAR_TO_VECTOR_SECONDS = Histogram(
    "feature_vector_latency_seconds",
    "Bar-arrival(FIRST bar of minute) -> vector-ready wall time per shard (end-to-end incl. Alpaca "
    "delivery spread; excludes the post-bet parquet write)",
    ["shard"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0),
)

# LAST-bar anchor = PURE COMPUTE latency, per shard. The wall-clock from the instant the LAST bar of the
# shard's minute landed off the websocket (i.e. once Alpaca has finished delivering that minute's bars
# for this shard's symbols) to the instant THIS shard's feature vector is assembled (BEFORE the parquet
# write). By anchoring on the LAST bar this EXCLUDES Alpaca's bar-delivery spread that feature_vector_
# latency_seconds includes — it measures only the route-dispatch + queue + compute hops that are OURS.
# Comparing the two side by side separates "slow because Alpaca delivered late" from "slow in our
# pipeline". Same bucket range as above so the two are directly comparable.
#
# NOTE: same bucket-change-needs-restart caveat as above.
ASSEMBLE_SECONDS = Histogram(
    "feature_assemble_seconds",
    "Bar-arrival(LAST bar of minute) -> vector-ready wall time per shard (pure compute, excludes both "
    "Alpaca delivery spread and the post-bet parquet write)",
    ["shard"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0),
)


# Incremental-path parity self-check (FP_INCREMENTAL_PARITY). Each minute the incremental engine output is
# compared cell-for-cell against the batch recompute (the truth); we observe the WORST divergence per
# reduce_input bucket as a MULTIPLE of the parity tolerance (1.0 == exactly at tolerance) and count any
# minute that breaches benign drift. While the incremental fast path is DEFAULT-OFF this is the live
# evidence that it stays parity-true before it is ever flipped on. Parity is sacred (CLAUDE.md): a
# non-zero breach counter must block enabling the fast path.
INCREMENTAL_PARITY_TOL_RATIO = Histogram(
    "feature_incremental_parity_tol_ratio",
    "Worst per-minute incremental-vs-batch divergence as a multiple of the parity tolerance, by reduce_input",
    ["reduce_input"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 50.0, 100.0, 1000.0),
)
INCREMENTAL_PARITY_BREACH = Counter(
    "feature_incremental_parity_breach_total",
    "Minutes where incremental-vs-batch divergence exceeded benign drift, by reduce_input bucket",
    ["reduce_input"],
)


def record_incremental_parity(reduce_input: str, tol_ratio: float, breached: bool) -> None:
    """Observe one minute's incremental-vs-batch parity self-check for ``reduce_input``: the worst
    divergence as a multiple of the parity tolerance, and whether it breached benign drift (see
    capture._incremental_parity)."""
    INCREMENTAL_PARITY_TOL_RATIO.labels(reduce_input=reduce_input).observe(tol_ratio)
    if breached:
        INCREMENTAL_PARITY_BREACH.labels(reduce_input=reduce_input).inc()


def record_group_timings(timings: dict[str, float]) -> None:
    """Observe one minute's per-group compute times. ``timings`` is {group_name: milliseconds} from
    ``CaptureState.group_timings``."""
    for group, ms in timings.items():
        GROUP_COMPUTE_SECONDS.labels(group=group).observe(ms / 1000.0)


def record_bar_to_vector(shard_id: int, seconds: float) -> None:
    """Observe one minute's FIRST-bar-anchored end-to-end latency for ``shard_id`` (seconds, write
    excluded). This INCLUDES Alpaca's per-minute bar-delivery spread (see BAR_TO_VECTOR_SECONDS doc).

    ``seconds`` MUST be measured with a clock comparable ACROSS the reader and worker processes (i.e.
    ``time.time()`` wall clock, NOT ``time.perf_counter()`` which is per-process). The caller computes
    ``time.time() - first_bar_arrival_wallclock`` after the vector is assembled and subtracts the write
    time."""
    BAR_TO_VECTOR_SECONDS.labels(shard=str(shard_id)).observe(seconds)


def record_assemble(shard_id: int, seconds: float) -> None:
    """Observe one minute's LAST-bar-anchored PURE-COMPUTE latency for ``shard_id`` (seconds, write
    excluded). This EXCLUDES Alpaca's bar-delivery spread (see ASSEMBLE_SECONDS doc).

    ``seconds`` MUST be measured with the same cross-process wall clock (``time.time()``). The caller
    computes ``time.time() - last_bar_arrival_wallclock`` after the vector is assembled and subtracts the
    write time."""
    ASSEMBLE_SECONDS.labels(shard=str(shard_id)).observe(seconds)


def start_metrics_server(port: int) -> None:
    """Expose /metrics for Prometheus to scrape (one port per shard worker)."""
    start_http_server(port)
