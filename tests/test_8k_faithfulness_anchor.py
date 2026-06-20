"""⭐ The 8-K (#197) FAITHFULNESS ANCHOR for the Phase-1 magnitude/volume archetypes.

#197's verdict (experiments/2026-06-19-8k-event-study): on the 8-K event surface NOTHING is tradeable —
  - VOLUME surge: real (shuffle-z ~28) but COLLAPSES under own-vol (collapse ~0.04) = intensity not alpha
  - gross MAGNITUDE (rv/range): real (z ~28-30) but COLLAPSES (collapse ~0.04) = vol-persistence
  - the abnormal MOVE (absret): does NOT collapse (collapse ~0.79) but is MEDIAN-NEGATIVE net-of-cost
    (win-rate < 0.5; the RTH subset net is outright negative) — the positive mean is a fat tail
  - DIRECTION: clean null
A faithful Phase-1 archetype run on this surface must reach the SAME verdict — DESCRIPTIVE-ONLY on
every target (collapse for vol/rv/range; median-negative for absret). A magnitude/volume archetype that
showed a clean tradeable 8-K vol edge would be OVERFIT and FAIL this check.

This anchor reads #197's ACTUAL screen_results.csv and asserts the archetype's `_decide` gate
reproduces the published verdict for each (target, window) — i.e. the gate AGREES with the trusted
hand-rolled result. (The archetype's collapse + net-of-cost machinery is unit-tested in
test_magnitude_volume.py; here we pin that its VERDICT LOGIC matches #197.)
"""
from __future__ import annotations

import csv
import os

from quantlib.battery.magnitude_volume import MagnitudeVolumeResult, TargetKind, _decide
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing

SCREEN = os.path.join(
    os.path.dirname(__file__), "..", "experiments", "2026-06-19-8k-event-study", "screen_results.csv"
)
_SPEC = ArchetypeSpec("magnitude", Horizon.OVERNIGHT, Conditioner.NONE, Sizing.EW)
_STAT_TO_TARGET = {
    "vol": TargetKind.LOGVOL,
    "rv": TargetKind.RV,
    "range": TargetKind.RV,
    "absret": TargetKind.ABSRET,
}


def _result_from_row(row: dict[str, str]) -> MagnitudeVolumeResult:
    """Build a MagnitudeVolumeResult carrying #197's measured stats so the gate verdict can be checked.
    #197 reports a sign-flip z (shuffle_z) rather than NW-t; for the gate's predictability leg we use a
    large \\|t\\| when the row survived FDR (it measured real predictability), and the MEDIAN net (the
    #197 verdict driver) which is negative wherever win-rate < 0.5 even if the MEAN net is positive."""
    target = _STAT_TO_TARGET[row["stat"]]
    raw_ic = abs(float(row["median_logratio"]))  # the |effect size| stands in for the rank-IC magnitude
    collapse = float(row["collapse"]) if row["collapse"] not in ("", "nan") else float("nan")
    net = None
    win = None
    if row.get("net_bps_10"):
        win = float(row["win_rate_10"])
        # #197's headline: the MEDIAN event net is <= 0 wherever win-rate < 0.5 (mean is fat-tail-driven)
        net = float(row["net_bps_10"]) if win >= 0.5 else -abs(float(row["net_bps_10"]))
    nw_t = 99.0 if row["fdr_survive"] == "true" else 0.0  # FDR survival == real predictability
    return MagnitudeVolumeResult(
        spec=_SPEC,
        target=target,
        raw_ic=max(raw_ic, 0.05),
        nw_t=nw_t,
        shuffle_ic=0.0,
        edge_vs_shuffle=raw_ic,
        n_test_ts=100,
        n_rows=int(row["n"]),
        resid_ic=collapse * raw_ic if collapse == collapse else 0.0,
        collapse=collapse,
        net_bps_median=net,
        win_rate=win,
    )


def test_8k_anchor_nothing_is_tradeable() -> None:
    """Across ALL of #197's (target, window) rows, the archetype gate must return DESCRIPTIVE-ONLY —
    NOT a single TRADEABLE-CANDIDATE. That is the faithful reproduction of '#197: no edge'."""
    with open(os.path.normpath(SCREEN)) as handle:
        rows = [r for r in csv.DictReader(handle) if r["regime"] == "all" and r["stat"] in _STAT_TO_TARGET]
    assert rows, "could not load #197 screen_results"
    verdicts = {}
    for row in rows:
        result = _result_from_row(row)
        verdict, reason = _decide(result)
        verdicts[f"{row['stat']}_{row['window']}"] = (verdict, reason)
    # NOTHING tradeable (the #197 verdict)
    tradeable = [k for k, (v, _) in verdicts.items() if v == "TRADEABLE-CANDIDATE"]
    assert not tradeable, f"archetype DISAGREES with #197 — flagged tradeable: {tradeable}"


def test_8k_anchor_vol_and_rv_collapse_to_persistence() -> None:
    """vol / rv / range must be flagged DESCRIPTIVE-ONLY *because they collapse* (vol-persistence) —
    the exact anti-fooling reason #197 gives."""
    with open(os.path.normpath(SCREEN)) as handle:
        rows = [
            r
            for r in csv.DictReader(handle)
            if r["regime"] == "all" and r["stat"] in ("vol", "rv", "range") and r["window"] == "30"
        ]
    assert rows
    for row in rows:
        result = _result_from_row(row)
        verdict, reason = _decide(result)
        assert verdict == "DESCRIPTIVE-ONLY"
        assert "vol-persistence" in reason, f"{row['stat']}: expected collapse reason, got '{reason}'"
        assert result.collapse < 0.30  # #197 measured ~0.04


def test_8k_anchor_absret_does_not_collapse_but_median_negative() -> None:
    """absret is the prize candidate — it does NOT collapse (collapse ~0.79, net of own-vol the move is
    real) but it is MEDIAN-NEGATIVE net-of-cost, so the gate must STILL say DESCRIPTIVE-ONLY for the
    median-negative reason (not the collapse reason). This is the subtle #197 kill."""
    with open(os.path.normpath(SCREEN)) as handle:
        rows = [
            r
            for r in csv.DictReader(handle)
            if r["regime"] == "all" and r["stat"] == "absret" and r["window"] == "30"
        ]
    assert rows
    result = _result_from_row(rows[0])
    assert result.collapse >= 0.30  # does NOT collapse (the move is real net of own-vol)
    assert result.win_rate is not None and result.win_rate < 0.5  # but the median event loses
    verdict, reason = _decide(result)
    assert verdict == "DESCRIPTIVE-ONLY"
    assert "median-NEGATIVE" in reason


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
