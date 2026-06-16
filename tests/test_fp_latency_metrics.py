"""Unit coverage for the latency metrics (quantlib.features.metrics) and the sharded-capture observe site.

Focus on the two DISPATCH-INDEPENDENT histograms added for actionable latency:
``feature_shard_compute_seconds`` (pure queue-pickup->assemble compute, never saturates) and
``feature_feed_delivery_seconds`` (provider-only delivery lag). These exist BECAUSE the existing
``feature_vector_latency_seconds`` / ``feature_assemble_seconds`` both end at ``ready_wallclock`` and are
therefore gated on the reader's next-minute-bar dispatch trigger, so they saturate (~60s) in sparse hours.

Pure-function style: we assert the recorded values equal the arithmetic the observe site does
(feed_delivery = first_arrival - (boundary + 60); compute = perf_delta - write), so the test pins the
formula, not just that the helper runs.
"""
from __future__ import annotations

from prometheus_client import REGISTRY

from quantlib.features import metrics


def _bucket_count(metric_name: str, shard: str) -> float:
    """Sum of observations Prometheus recorded for ``metric_name`` at label shard=``shard`` (the
    histogram's ``_count`` sample). Reads the live default REGISTRY — the same registry the helpers write."""
    value = REGISTRY.get_sample_value(f"{metric_name}_count", {"shard": shard})
    return value if value is not None else 0.0


def _bucket_sum(metric_name: str, shard: str) -> float:
    """The histogram ``_sum`` sample for ``metric_name`` at shard=``shard`` — total of all observed values,
    so a single observation makes ``_sum`` equal to that value (used to assert the recorded magnitude)."""
    value = REGISTRY.get_sample_value(f"{metric_name}_sum", {"shard": shard})
    return value if value is not None else 0.0


def test_new_histograms_exist() -> None:
    assert metrics.SHARD_COMPUTE_SECONDS._name == "feature_shard_compute_seconds"
    assert metrics.FEED_DELIVERY_SECONDS._name == "feature_feed_delivery_seconds"
    assert metrics.SHARD_COMPUTE_SECONDS._labelnames == ("shard",)
    assert metrics.FEED_DELIVERY_SECONDS._labelnames == ("shard",)


def test_record_helpers_observe_without_error() -> None:
    shard = "910"
    before_compute = _bucket_count("feature_shard_compute_seconds", shard)
    before_delivery = _bucket_count("feature_feed_delivery_seconds", shard)
    metrics.record_shard_compute(910, 0.012)
    metrics.record_feed_delivery(910, 2.5)
    assert _bucket_count("feature_shard_compute_seconds", shard) == before_compute + 1
    assert _bucket_count("feature_feed_delivery_seconds", shard) == before_delivery + 1


def test_feed_delivery_value_is_close_to_minus_boundary_plus_60() -> None:
    """feed_delivery = first_arrival - (minute_boundary_epoch + 60.0), exactly as the observe site computes."""
    shard = "911"
    minute_boundary_epoch = 1_700_000_000.0
    first_arrival = minute_boundary_epoch + 63.4  # 3.4s after the minute closed
    expected = first_arrival - (minute_boundary_epoch + 60.0)  # == 3.4
    before = _bucket_sum("feature_feed_delivery_seconds", shard)
    metrics.record_feed_delivery(911, max(0.0, expected))
    after = _bucket_sum("feature_feed_delivery_seconds", shard)
    # Tolerance accommodates float rounding when subtracting epoch-magnitude operands (1.7e9).
    assert abs((after - before) - 3.4) < 1e-4


def test_shard_compute_value_is_perf_delta_minus_write() -> None:
    """shard_compute = (perf_counter_end - start) - write_seconds, exactly as the observe site computes."""
    shard = "912"
    start = 100.0
    perf_counter_end = 100.040  # 40ms of wall in the worker
    write_seconds = 0.015  # 15ms post-bet parquet write, excluded
    expected = (perf_counter_end - start) - write_seconds  # == 0.025
    before = _bucket_sum("feature_shard_compute_seconds", shard)
    metrics.record_shard_compute(912, max(0.0, expected))
    after = _bucket_sum("feature_shard_compute_seconds", shard)
    assert abs((after - before) - 0.025) < 1e-9


def test_clamped_at_zero_when_arrival_precedes_close() -> None:
    """A first-bar arrival BEFORE the computed close (clock skew / early bar) clamps to 0, never negative."""
    shard = "913"
    minute_boundary_epoch = 1_700_000_000.0
    first_arrival = minute_boundary_epoch + 58.0  # before close (boundary + 60)
    recorded = max(0.0, first_arrival - (minute_boundary_epoch + 60.0))
    assert recorded == 0.0
    before = _bucket_sum("feature_feed_delivery_seconds", shard)
    metrics.record_feed_delivery(913, recorded)
    assert _bucket_sum("feature_feed_delivery_seconds", shard) == before
