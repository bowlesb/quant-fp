"""Latency gate — enforces Ben's rule that a feature earns its place only if it is timed and fast.

Runs the per-group profiler at a fixed reference scale and fails if any group is pathologically slow
per feature. Deliberately LOOSE (catches 5x+ regressions / accidental O(n^2), not normal CI variance);
the precise tracking is the full-scale profiler run on every batch. This just stops an egregiously
slow group from merging unnoticed.

Host-load tolerance: the profiler times wall-clock and takes the MIN over reps, so the best-case sample
reflects true compute cost — but only if enough samples are taken to catch an uncontended one. On a busy
box shared with the other loops, a thin min-of-2 is noisy and the slowest group's tail can cross the
ceiling with nothing actually regressed. So this gate (a) takes a healthy rep count, and (b) RE-CONFIRMS
any apparent offender with a higher-rep re-measure before failing: a genuine algorithmic regression stays
over the ceiling on the re-measure, transient contention does not. This keeps the gate sharp for real
5x+ regressions while immune to the load-induced false fails that previously red-herring'd the suite.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.profile import build_frames, profile

REFERENCE_TICKERS = 500
PER_FEATURE_CEILING_US = 60_000.0  # us/feature at 500 tickers; current worst (~37k clean) has headroom
SCREEN_REPS = 4  # first pass — min over these to shake off most contention noise
CONFIRM_REPS = 12  # re-measure suspects with many more samples; a real regression survives, noise doesn't


def confirmed_offenders(frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Groups over the ceiling on BOTH a screen and a higher-rep re-confirm (host-load robust)."""
    screened = profile(frames, reps=SCREEN_REPS)
    suspects = screened.filter(screened["us_per_feature"] > PER_FEATURE_CEILING_US)
    if suspects.height == 0:
        return suspects
    reconfirmed = profile(frames, reps=CONFIRM_REPS)
    return reconfirmed.filter(reconfirmed["us_per_feature"] > PER_FEATURE_CEILING_US)


def test_every_group_under_latency_ceiling() -> None:
    frames = build_frames(n_tickers=REFERENCE_TICKERS, window_min=120, daily_days=120)
    offenders = confirmed_offenders(frames)
    assert offenders.height == 0, (
        f"groups over {PER_FEATURE_CEILING_US:.0f} us/feature at {REFERENCE_TICKERS} tickers, "
        f"CONFIRMED on a {CONFIRM_REPS}-rep re-measure (a real regression, not transient host load) — "
        f"profile and optimize (materialize shared rolling sums) before merging:\n"
        f"{offenders.select('group', 'us_per_feature', 'ms')}"
    )
