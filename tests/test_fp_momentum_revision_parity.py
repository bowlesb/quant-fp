"""Momentum windowed-mean-over-returns parity under provisional-vs-settled bar REVISION.

Root-cause (2026-06-24): the live websocket minute bar is provisional; the settled backfill bar is the
consolidated tape (late/out-of-sequence prints applied overnight). A single revised CLOSE perturbs the
one-minute return at TWO minutes (numerator of one return, denominator of the next), so momentum's
``mean_abs_ret_{W}m`` / ``up_ratio_{W}m`` — both means OVER those returns — disagree live-vs-settled on
any window that spans a revised minute. The disagreement GROWS with the window (a wider window is likelier
to contain a revised minute), so exact-cell parity false-fails these worse the longer the lookback — the
irreducible Layer-A "~99.5%" revision class (docs/PARITY_PLAYBOOK.md). These tests:

  1. reproduce that W-scaling exact-cell degradation on a synthesized revised tape,
  2. assert the DISTRIBUTIONAL grade (the method these specs now declare) PASSES it (the value
     distributions agree), and the within-day certifier path dispatches the same way,
  3. keep the harness honest: a genuine within-symbol SCRAMBLE still fails distributionally (anti-gaming).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features import REGISTRY, BatchContext, run_group
from quantlib.features.compare import cell_verdict, diff
from quantlib.features.groups.momentum import REVISION_DIST_TOL
from quantlib.features.within_day_parity import _distributional_summary_row

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
N_MIN = 400  # minutes per symbol (>> the 180m longest window, and >= MIN_PARITY_CELLS across symbols)
SYMBOLS = [f"S{i:02d}" for i in range(8)]


def _closes(seed: int) -> list[float]:
    """A plausible ~$100 intraday close path: a tiny random walk in basis points."""
    rng = random.Random(seed)
    price = 100.0
    out = [price]
    for _ in range(N_MIN - 1):
        price *= 1.0 + rng.gauss(0.0, 0.0008)  # ~8 bps per-minute vol
        out.append(price)
    return out


def _revise(closes: list[float], minutes: list[int], seed: int) -> list[float]:
    """Apply a settled-vs-provisional revision at ``minutes``: nudge that minute's close by a few bps,
    exactly the late-print correction the live bar never saw."""
    rng = random.Random(seed)
    revised = closes[:]
    for minute in minutes:
        # a late-print correction of a few bps — the measured live-vs-settled close revision magnitude.
        revised[minute] *= 1.0 + rng.choice([-1.0, 1.0]) * rng.uniform(1e-4, 3e-4)
    return revised


def _frame(close_by_symbol: dict[str, list[float]]) -> pl.DataFrame:
    rows = {"symbol": [], "minute": [], "close": []}
    for symbol, closes in close_by_symbol.items():
        rows["symbol"].extend([symbol] * len(closes))
        rows["minute"].extend(BASE + timedelta(minutes=i) for i in range(len(closes)))
        rows["close"].extend(closes)
    return pl.DataFrame(rows)


def _momentum(close_by_symbol: dict[str, list[float]]) -> pl.DataFrame:
    out = run_group(
        REGISTRY.get_group("momentum"),
        BatchContext(frames={"minute_agg": _frame(close_by_symbol)}),
        validate=False,
    )
    return out


def _live_and_settled() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Live = provisional closes; settled = the same path with a few minutes revised. Each symbol gets a
    couple of revised minutes (sparse, ~0.5-1% of minutes — the measured live revision rate)."""
    settled_closes, live_closes = {}, {}
    for index, symbol in enumerate(SYMBOLS):
        base = _closes(seed=index)
        live_closes[symbol] = base
        revised_minutes = [
            60 + index,
            280 - index,
        ]  # ~0.5% of minutes revised per symbol (the measured rate)
        settled_closes[symbol] = _revise(base, revised_minutes, seed=1000 + index)
    return _momentum(live_closes), _momentum(settled_closes)


def _exact_pass_rate(live: pl.DataFrame, settled: pl.DataFrame, feature: str) -> float:
    """The exact-cell match fraction the OLD grade used: |a-b| <= 1e-12 + tol*|b| per cell."""
    spec = {s.name: s for s in REGISTRY.get_group("momentum").declare()}[feature]
    joined = live.join(settled, on=["symbol", "minute"], how="inner", suffix="_bk")
    verdicts = joined.select(cell_verdict(spec, feature, joined.schema).alias("v"))
    tally = dict(verdicts.group_by("v").len().iter_rows())
    compared = tally.get("match", 0) + tally.get("mismatch", 0)
    return tally.get("match", 0) / compared if compared else 1.0


def test_exact_cell_pass_rate_degrades_with_window() -> None:
    """The false-fail this PR fixes: under sparse bar revision a single revised close spreads across the
    whole window's mean, so exact-cell parity on mean_abs_ret is WORSE the wider the window (more revised
    minutes land in the lookback) and falls below the 0.999 windowed trust bar — even though no compute is
    wrong. The long-window rate must be materially below the short-window rate AND below the trust bar."""
    live, settled = _live_and_settled()
    rates = {w: _exact_pass_rate(live, settled, f"mean_abs_ret_{w}m") for w in (5, 30, 60, 180)}
    assert rates[5] > rates[60] > rates[5] - 1.0  # short clearly beats mid (the W-scaling direction)
    assert rates[60] < rates[5], f"60m should be worse than 5m under revision noise: {rates}"
    # every window — even the short one — falls below the _WINDOWED min_pass_rate (0.999): exact-cell
    # would DEFECT them all, the false-fail this PR removes.
    for window, rate in rates.items():
        assert rate < 0.999, f"mean_abs_ret_{window}m should false-fail exact-cell, got {rate}"


def test_distributional_grade_passes_revision_noise() -> None:
    """The fix: graded distributionally at the revision tolerance, the momentum windowed means PASS —
    the live and settled VALUE DISTRIBUTIONS agree (the divergence is sparse revision noise, not a
    compute difference). Asserts on the longest, worst-case window."""
    live, settled = _live_and_settled()
    tiers = pl.DataFrame(
        {"symbol": SYMBOLS, "tier": [1] * len(SYMBOLS)}, schema={"symbol": pl.String, "tier": pl.Int32}
    )
    report = diff(live, settled, tiers)
    for feature in ("mean_abs_ret_180m", "up_ratio_180m", "mean_abs_ret_60m"):
        row = report.filter((pl.col("feature") == feature) & (pl.col("tier") == 1)).row(0, named=True)
        assert row["method"] == "distributional", f"{feature} must be graded distributionally"
        assert row["passed"] is True, f"{feature} should PASS distributionally under revision noise: {row}"


def test_distributional_grade_still_bites_on_real_divergence() -> None:
    """Anti-gaming: distributional is NOT a free pass. A real divergence that SHIFTS the value
    distribution (not sparse revision noise — here the live mean is scaled 20% on every cell, the kind of
    genuine compute bug the grade must catch) breaks the quantile-shape agreement -> must FAIL."""
    live, settled = _live_and_settled()
    feature = "mean_abs_ret_60m"
    broken = live.with_columns((pl.col(feature) * 1.20).alias(feature))  # a 20% systematic shift
    tiers = pl.DataFrame(
        {"symbol": SYMBOLS, "tier": [1] * len(SYMBOLS)}, schema={"symbol": pl.String, "tier": pl.Int32}
    )
    row = (
        diff(broken, settled, tiers)
        .filter((pl.col("feature") == feature) & (pl.col("tier") == 1))
        .row(0, named=True)
    )
    assert row["passed"] is False, f"a 20% distribution shift must fail distributionally: {row}"


def test_within_day_compare_window_dispatches_distributional() -> None:
    """The within-day certifier path (compare_window) must dispatch on parity_method too — else the spec
    change is cosmetic for within-day trust. We assert the dispatch helper grades a distributional feature
    via dist_score (paired-cells-as-match), not cell-verdict, by checking value_rate is 1.0 on a clean
    pairing and the revision tolerance is the one carried."""
    live, settled = _live_and_settled()
    joined = live.join(settled, on=["symbol", "minute"], how="full", suffix="_bk", coalesce=True)
    spec = {s.name: s for s in REGISTRY.get_group("momentum").declare()}["mean_abs_ret_180m"]
    assert spec.parity_method == "distributional"
    assert spec.tolerance == REVISION_DIST_TOL
    row = _distributional_summary_row(joined, "mean_abs_ret_180m", spec)
    assert row["n_compared"] > 0
    # passes the distributional bar -> all paired cells count as match -> value_rate 1.0 (>= 0.999 min_pass)
    assert row["value_rate"] == 1.0, f"clean revision-noise pairing should certify within-day: {row}"
