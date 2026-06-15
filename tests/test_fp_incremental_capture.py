"""Incremental fast path wired into ``capture.process_bars`` == the batch path, cell-for-cell.

This is the live-integration parity gate for P1 #1 (per-symbol fast path). ``process_bars`` is the shared
compute core for the mock, real, and sharded capture clients; with ``FP_INCREMENTAL=1`` it assembles the
batched ``ReductionGroup``s from per-bucket ``IncrementalEngine`` running sums (via ``step`` — the SAME
``assemble_from_long`` the batch uses, so warmup/flag null handling is byte-identical) instead of
recomputing the whole buffer each minute. Parity is sacred (CLAUDE.md): the incremental output must equal
the batch output within tolerance, under a FLUCTUATING active symbol set (the live regime).

Default (no env set) the path is byte-identical to the batch — guaranteed by ``test_default_is_batch``.

KNOWN CONDITIONING CAVEAT (``test_ols_r2_near_perfect_fit_is_flagged``): sum-based OLS r2/corr near a
PERFECT fit is a difference of large near-equal sums; the incremental running add/subtract rounds
differently from the batch's fresh window sums, so r2≈1 can diverge far beyond tolerance. The self-check
exists to SURFACE exactly that before the fast path is trusted as the source.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features import capture
from quantlib.features.capture import (
    _PARITY_BREACH_RATIO,
    CaptureState,
    _incremental_config,
    _incremental_parity,
    process_bars,
)

BASE = dt.datetime(2026, 6, 16, 14, 0, tzinfo=dt.timezone.utc)


def _stream_minutes(n_sym: int, n_min: int, present_p: float, seed: int, vol: float = 0.02) -> list[list[dict]]:
    """A normalized-bar stream (list of per-minute bar batches) where minute 0 carries every symbol (clean
    warmup) and each later minute carries a random ~``present_p`` subset — the live membership-churn shape.
    ``vol`` sets the per-minute return noise; the default 0.02 keeps regressions well-conditioned (r2 well
    below 1) so the parity comparison is not dominated by sum-based r2 cancellation near a perfect fit."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        bars: list[dict] = []
        for s in range(n_sym):
            present = mi == 0 or rng.random() < present_p
            price[s] *= 1.0 + (rng.standard_normal() * vol)
            if not present:
                continue
            c = price[s]
            bars.append({"S": f"S{s}", "o": c * 0.999, "c": c, "h": c * 1.002, "l": c * 0.998,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        out.append(bars)
    return out


def _run(stream: list[list[dict]], root: str) -> dict[str, pl.DataFrame]:
    """Drive a stream through ``process_bars`` (accumulate in RAM, no store) and return the per-group output."""
    state = CaptureState()
    for bars in stream:
        process_bars(state, bars, root, "mock", "2026-06-16", 120, accumulate=True, write=False)
    return state.accumulated


def _worst_tol_ratio(batch: dict, inc: dict, *, cols_drop: tuple[str, ...] = ()) -> float:
    """Worst divergence between two accumulated per-group dicts, as a multiple of the parity tolerance,
    joined on (symbol, minute). ``cols_drop`` excludes named feature columns (used to isolate the r2 family)."""
    assert set(batch) == set(inc), "group set differs"
    worst = 0.0
    for name, bframe in batch.items():
        iframe = inc[name]
        assert set(bframe.columns) == set(iframe.columns), f"{name}: columns differ"
        keys = ["symbol", "minute"]
        cols = [c for c in bframe.columns if c not in keys and not any(d in c for d in cols_drop)]
        j = bframe.sort(keys).join(iframe.sort(keys).select([*keys, *cols]), on=keys, suffix="__i")
        assert j.height == bframe.height, f"{name}: row set differs"
        for col in cols:
            a, b = pl.col(col), pl.col(f"{col}__i")
            assert j.filter(a.is_null() != b.is_null()).height == 0, f"{name}.{col}: null/non-null mismatch"
            ratio = j.select(((a - b).abs() / (1e-6 + 1e-6 * a.abs())).fill_null(0.0).max()).item()
            if ratio is not None:
                worst = max(worst, float(ratio))
    return worst


def test_default_is_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env set the incremental path is inert: config reads all-off (output is the batch output)."""
    for var in ("FP_INCREMENTAL", "FP_INCREMENTAL_PARITY", "FP_INCREMENTAL_SLICE"):
        monkeypatch.delenv(var, raising=False)
    assert _incremental_config() == (False, False, False)


def test_incremental_capture_matches_batch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """FP_INCREMENTAL=1 (gap-safe whole-buffer derive) produces the SAME per-group output as the batch path
    on well-conditioned data, under a fluctuating active symbol set across many minutes — within a small
    multiple of the parity tolerance (benign float drift)."""
    stream = _stream_minutes(n_sym=8, n_min=50, present_p=0.7, seed=3)

    monkeypatch.delenv("FP_INCREMENTAL", raising=False)
    batch = _run(stream, str(tmp_path / "batch"))

    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)  # gap-safe whole-buffer derive (open slice constraint)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(stream, str(tmp_path / "inc"))

    assert batch, "expected reduction-group output"
    worst = _worst_tol_ratio(batch, inc)
    assert worst < _PARITY_BREACH_RATIO, f"incremental diverged from batch: worst {worst}x tolerance"


def test_parity_selfcheck_records_clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """FP_INCREMENTAL=1 + FP_INCREMENTAL_PARITY=1 runs BOTH paths each minute, writes the batch truth, and
    records a within-drift divergence (no breach) on well-conditioned data. Exercises the self-check wiring."""
    stream = _stream_minutes(n_sym=6, n_min=40, present_p=0.7, seed=9)
    seen: list[tuple[str, float, bool]] = []
    monkeypatch.setattr(capture.metrics, "record_incremental_parity",
                        lambda ri, r, b: seen.append((ri, r, b)))
    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.setenv("FP_INCREMENTAL_PARITY", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)

    _run(stream, str(tmp_path / "selfcheck"))

    assert seen, "self-check should record a parity sample each minute"
    assert not any(breached for _, _, breached in seen), \
        f"no minute should breach on well-conditioned data; worst {max(r for _, r, _ in seen)}x tolerance"


def test_ols_near_perfect_fit_is_flagged(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """KNOWN CONDITIONING CAVEAT: on a SMOOTH (near-linear) price walk the sum-based OLS family fits
    near-perfectly, so the incremental running sums diverge from the batch fresh sums far beyond tolerance.
    The self-check FLAGS it (a breach) — proving it detects real divergence — whereas well-conditioned data
    (``test_parity_selfcheck_records_clean``) does not. This is the divergence to harden before the fast
    path is trusted as the source (see docs/AUTONOMOUS_BACKLOG.md P1 #1)."""
    smooth = _stream_minutes(n_sym=8, n_min=20, present_p=0.7, seed=3, vol=0.002)  # near-linear -> near-perfect fit

    monkeypatch.delenv("FP_INCREMENTAL", raising=False)
    batch = _run(smooth, str(tmp_path / "b"))
    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    inc = _run(smooth, str(tmp_path / "i"))

    assert _worst_tol_ratio(batch, inc) > _PARITY_BREACH_RATIO, "expected near-perfect-fit conditioning divergence"


def test_incremental_parity_helper() -> None:
    """``_incremental_parity`` returns 0 for identical frames, a large tolerance-multiple for a perturbed
    one, and inf for a missing group or a null/non-null mismatch."""
    a = pl.DataFrame({"symbol": ["A", "B"], "minute": [BASE, BASE], "f": [1.0, 2.0]})
    assert _incremental_parity({"g": a}, {"g": a}) == 0.0
    perturbed = {"g": a.with_columns((pl.col("f") + 1.0).alias("f"))}
    assert _incremental_parity({"g": a}, perturbed) > _PARITY_BREACH_RATIO
    assert _incremental_parity({"g": a}, {}) == float("inf")  # missing group
    nulled = {"g": a.with_columns(pl.lit(None, dtype=pl.Float64).alias("f"))}
    assert _incremental_parity({"g": a}, nulled) == float("inf")  # null vs non-null
