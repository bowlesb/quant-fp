"""Latency gate — enforces Ben's rule that a feature earns its place only if it is timed and fast.

Runs the per-group profiler at a fixed reference scale and fails if any group is pathologically slow
per feature. Deliberately LOOSE (catches 5x+ regressions / accidental O(n^2), not normal CI variance);
the precise tracking is the full-scale profiler run on every batch. This just stops an egregiously
slow group from merging unnoticed.
"""
from __future__ import annotations

from quantlib.features.profile import build_frames, profile

REFERENCE_TICKERS = 500
PER_FEATURE_CEILING_US = 60_000.0  # us/feature at 500 tickers; current worst (~12k) has 5x headroom


def test_every_group_under_latency_ceiling() -> None:
    frames = build_frames(n_tickers=REFERENCE_TICKERS, window_min=120, daily_days=120)
    table = profile(frames, reps=2)
    offenders = table.filter(table["us_per_feature"] > PER_FEATURE_CEILING_US)
    assert offenders.height == 0, (
        f"groups over {PER_FEATURE_CEILING_US:.0f} us/feature at {REFERENCE_TICKERS} tickers — "
        f"profile and optimize (materialize shared rolling sums) before merging:\n"
        f"{offenders.select('group', 'us_per_feature', 'ms')}"
    )
