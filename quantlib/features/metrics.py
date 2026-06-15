"""Per-feature-group compute-latency metrics for Prometheus / Grafana (definition-of-done #5).

Each sharded capture worker records its per-group ``compute_latest`` time every minute into a Prometheus
histogram labelled by group, and exposes ``/metrics`` on a per-shard port (WORKER_METRICS_BASE_PORT +
shard_id) — the same pattern the ingestor uses for coverage gauges. Grafana then graphs p50/p99 latency
PER GROUP, so a feature an agent just added that's slow shows up immediately on the dashboard. Mirrors
the prior Edgar system's FEATURE_GROUP_DURATION histogram.
"""
from __future__ import annotations

from prometheus_client import Histogram, start_http_server

# Buckets span 0.5ms .. 2.5s — the range a per-group live compute can plausibly take at shard scale.
GROUP_COMPUTE_SECONDS = Histogram(
    "feature_group_compute_seconds",
    "Per-group compute_latest wall time, by feature group",
    ["group"],
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# Bar-arrival -> vector-ready end-to-end latency, per shard. This is the bet-relevant number from
# Ben's memory ("Bet-latency metric"): the wall-clock from the instant the capture process RECEIVED a
# minute's bar off the Alpaca websocket to the instant THIS shard's feature vector for that minute is
# assembled (BEFORE the parquet write — the write is post-bet and excluded). Buckets span 5ms .. 5s,
# the plausible range for a single shard's assemble across the reader-dispatch + queue + compute hops.
BAR_TO_VECTOR_SECONDS = Histogram(
    "feature_vector_latency_seconds",
    "Bar-arrival -> vector-ready wall time per shard (excludes the post-bet parquet write)",
    ["shard"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0),
)


def record_group_timings(timings: dict[str, float]) -> None:
    """Observe one minute's per-group compute times. ``timings`` is {group_name: milliseconds} from
    ``CaptureState.group_timings``."""
    for group, ms in timings.items():
        GROUP_COMPUTE_SECONDS.labels(group=group).observe(ms / 1000.0)


def record_bar_to_vector(shard_id: int, seconds: float) -> None:
    """Observe one minute's bar-arrival -> vector-ready latency for ``shard_id`` (seconds, write excluded).

    ``seconds`` MUST be measured with a clock comparable ACROSS the reader and worker processes (i.e.
    ``time.time()`` wall clock, NOT ``time.perf_counter()`` which is per-process). The caller computes
    ``time.time() - arrival_wallclock`` after the vector is assembled and subtracts the write time."""
    BAR_TO_VECTOR_SECONDS.labels(shard=str(shard_id)).observe(seconds)


def start_metrics_server(port: int) -> None:
    """Expose /metrics for Prometheus to scrape (one port per shard worker)."""
    start_http_server(port)
