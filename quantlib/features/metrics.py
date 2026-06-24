"""Per-feature-group compute-latency metrics for Prometheus / Grafana (definition-of-done #5).

Each sharded capture worker records its per-group ``compute_latest`` time every minute into a Prometheus
histogram labelled by group, and exposes ``/metrics`` on a per-shard port (WORKER_METRICS_BASE_PORT +
shard_id) — the same pattern the ingestor uses for coverage gauges. Grafana then graphs p50/p99 latency
PER GROUP, so a feature an agent just added that's slow shows up immediately on the dashboard. Mirrors
the prior Edgar system's FEATURE_GROUP_DURATION histogram.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

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
#
# SATURATION CAVEAT: the minute's batch is DISPATCHED to the workers only when the NEXT minute's first bar
# arrives (real_capture.on_bar), so ``ready_wallclock`` — and therefore this end-to-end number — is GATED
# on the next-minute-bar dispatch trigger. In sparse / extended hours, where the next bar can be a minute
# or more away, this SATURATES at the top (~60s) bucket and stops reflecting our pipeline at all. For a
# dispatch-INDEPENDENT view use feature_shard_compute_seconds (pure queue-pickup->assemble compute) and
# feature_feed_delivery_seconds (provider-only delivery lag); both are below and never saturate this way.
BAR_TO_VECTOR_SECONDS = Histogram(
    "feature_vector_latency_seconds",
    "Bar-arrival(FIRST bar of minute) -> vector-ready wall time per shard (end-to-end incl. Alpaca "
    "delivery spread; excludes the post-bet parquet write). GATED on the next-minute-bar dispatch "
    "trigger, so it SATURATES (~60s) in sparse/extended hours — read feature_shard_compute_seconds + "
    "feature_feed_delivery_seconds for the dispatch-independent breakdown",
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
#
# SAME SATURATION CAVEAT as feature_vector_latency_seconds: this still ends at ``ready_wallclock``, which is
# GATED on the next-minute-bar dispatch trigger (real_capture.on_bar dispatches a minute only once the next
# minute's first bar arrives). So in sparse / extended hours it ALSO saturates (~60s) despite being the
# "pure compute" anchor — the wait between the last bar landing and the dispatch firing is counted here. For
# a dispatch-INDEPENDENT compute number that never saturates use feature_shard_compute_seconds below
# (measured from queue-pickup, after dispatch); use feature_feed_delivery_seconds to isolate provider lag.
ASSEMBLE_SECONDS = Histogram(
    "feature_assemble_seconds",
    "Bar-arrival(LAST bar of minute) -> vector-ready wall time per shard (pure compute, excludes both "
    "Alpaca delivery spread and the post-bet parquet write). GATED on the next-minute-bar dispatch "
    "trigger, so it SATURATES (~60s) in sparse/extended hours — read feature_shard_compute_seconds for "
    "the dispatch-independent compute and feature_feed_delivery_seconds for provider lag",
    ["shard"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0),
)


# Dispatch-INDEPENDENT pure compute, per shard. The wall-clock from the instant THIS worker PICKED the
# minute's batch off its queue (``start = time.perf_counter()``, sharded_capture.worker_main) to the
# instant the vector is assembled, with the post-bet parquet write subtracted. Because the timer starts at
# queue-pickup — AFTER the reader's next-minute-bar dispatch already fired — it measures only the worker's
# map step (tick aggregation + process_shard), with ZERO of the dispatch wait that saturates
# feature_vector_latency_seconds / feature_assemble_seconds. This is the honest "how long does OUR compute
# take" number that stays meaningful in sparse / extended hours. perf_counter is per-process, which is fine:
# both endpoints live in the same worker process.
SHARD_COMPUTE_SECONDS = Histogram(
    "feature_shard_compute_seconds",
    "Per-shard pure compute time (queue-pickup -> vector assembled, write excluded); dispatch-INDEPENDENT "
    "so it never saturates, unlike feature_assemble_seconds",
    ["shard"],
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# Gather-phase compute, single-process (reader). The wall-clock to run the universe-wide reduce groups
# (cross_sectional_rank + breadth) over ALL symbols once per minute in the reader's dispatch. This is the
# "+ gather" half of the bet-latency principle (the per-shard map is feature_shard_compute_seconds; the
# gather runs ONCE over the whole universe, so it has no shard label). Like feature_shard_compute_seconds
# it is measured inline (perf_counter around process_reduce), so it is dispatch-INDEPENDENT and never
# saturates the way feature_vector_latency_seconds / feature_assemble_seconds do.
GATHER_SECONDS = Histogram(
    "feature_gather_seconds",
    "Per-minute gather-phase compute time: the universe-wide reduce groups (cross_sectional_rank + "
    "breadth) over ALL symbols in the reader, single-process (no shard label); dispatch-INDEPENDENT",
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# Provider-only feed-delivery lag, per shard. The wall-clock from the bar-minute's CLOSE (minute boundary +
# 60s) to the instant that minute's FIRST bar arrived off the Alpaca websocket. This isolates Alpaca's feed
# latency — how long AFTER a minute closes before its first bar even reaches us — from anything in our
# pipeline. Large values here mean "the provider delivered late", not "our compute is slow", which the
# end-to-end feature_vector_latency_seconds alone cannot distinguish.
FEED_DELIVERY_SECONDS = Histogram(
    "feature_feed_delivery_seconds",
    "Alpaca delivery lag: wall-clock from bar-minute CLOSE to the minute's first bar arriving off the "
    "websocket, per shard (provider-bound, isolates feed latency from our compute)",
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

POINT_RING_BREACH = Counter(
    "feature_point_ring_breach_total",
    "Minutes where the PointRing carried __pt_ columns diverged from the whole-buffer resolve_points truth "
    "(FP_POINT_RING_PARITY self-check) — any common-symbol __pt_ cell beyond 1e-12 absolute, or a symbol "
    "present in resolve_points but missing from the ring (only_truth>0)",
)
POINT_RING_MAX_ABS_DIFF = Gauge(
    "feature_point_ring_max_abs_diff",
    "Worst per-minute absolute __pt_ cell divergence between PointRing and resolve_points (0.0 when clean)",
)


# Ingestion-rate counters, incremented in the READER process per message off the websocket and exposed on
# the reader's own /metrics port, so `rate(feature_*_ingested_total[1m])` gives bars/trades/quotes per
# second — the live ingestion frequency for the dashboard.
BARS_INGESTED = Counter("feature_bars_ingested_total", "Minute bars received off the stream")
TRADES_INGESTED = Counter("feature_trades_ingested_total", "Trade ticks received off the stream")
QUOTES_INGESTED = Counter("feature_quotes_ingested_total", "Quote ticks received off the stream")


def record_incremental_parity(reduce_input: str, tol_ratio: float, breached: bool) -> None:
    """Observe one minute's incremental-vs-batch parity self-check for ``reduce_input``: the worst
    divergence as a multiple of the parity tolerance, and whether it breached benign drift (see
    capture._incremental_parity)."""
    INCREMENTAL_PARITY_TOL_RATIO.labels(reduce_input=reduce_input).observe(tol_ratio)
    if breached:
        INCREMENTAL_PARITY_BREACH.labels(reduce_input=reduce_input).inc()


def record_point_ring_parity(max_abs_diff: float, breached: bool) -> None:
    """Observe one minute's PointRing-vs-resolve_points self-check (FP_POINT_RING_PARITY): the worst absolute
    __pt_ cell divergence, and whether it breached (any common-symbol cell > 1e-12 or a symbol present in
    resolve_points but absent from the ring). MONITORING-ONLY — the served output is still the ring's."""
    POINT_RING_MAX_ABS_DIFF.set(max_abs_diff)
    if breached:
        POINT_RING_BREACH.inc()


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


def record_shard_compute(shard_id: int, seconds: float) -> None:
    """Observe one minute's DISPATCH-INDEPENDENT pure compute time for ``shard_id`` (seconds, write
    excluded). Unlike record_bar_to_vector / record_assemble — both of which end at ``ready_wallclock``
    and are therefore gated on the reader's next-minute-bar dispatch trigger and SATURATE in sparse hours
    — this is measured from the worker's queue-pickup (``time.perf_counter()`` at the top of the dispatched
    minute, after dispatch already fired) to the assemble, so it carries none of the dispatch wait and stays
    meaningful in sparse / extended hours.

    ``seconds`` is a within-process ``perf_counter`` delta (both endpoints in the same worker), so
    perf_counter is correct here — NOT the cross-process ``time.time()`` the other two require. The caller
    computes ``(time.perf_counter() - start) - write_seconds`` after the vector is assembled."""
    SHARD_COMPUTE_SECONDS.labels(shard=str(shard_id)).observe(seconds)


def record_gather(seconds: float) -> None:
    """Observe one minute's gather-phase compute time (seconds): the universe-wide reduce groups
    (cross_sectional_rank + breadth) run once over ALL symbols in the reader. Like record_shard_compute
    this is a within-process ``perf_counter`` delta (the reader process), so it is dispatch-independent and
    never saturates. The caller computes ``time.perf_counter() - start`` around process_reduce. Single
    gather per minute (not per shard), so there is no shard label."""
    GATHER_SECONDS.observe(seconds)


def record_feed_delivery(shard_id: int, seconds: float) -> None:
    """Observe one minute's PROVIDER-ONLY feed-delivery lag for ``shard_id`` (seconds): the wall-clock from
    the bar-minute's CLOSE (minute boundary + 60s) to the minute's first bar arriving off the Alpaca
    websocket. Isolates Alpaca's feed latency from our compute. The caller computes
    ``first_arrival - (minute_boundary_epoch + 60.0)`` (clamped at 0) — ``first_arrival`` is the
    cross-process ``time.time()`` stamp of the minute's first bar landing."""
    FEED_DELIVERY_SECONDS.labels(shard=str(shard_id)).observe(seconds)


def start_metrics_server(port: int) -> None:
    """Expose /metrics for Prometheus to scrape (one port per shard worker)."""
    start_http_server(port)
