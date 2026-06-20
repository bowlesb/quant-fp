"""Phase-1 NON-DIRECTION archetypes: MAGNITUDE and VOLUME targets + the partial-r marginal control.

The edge hunt has settled 5 DIRECTION-nulls; the only real signal across every surface is
MAGNITUDE/VOLUME intensity (turbulence->vol, EDGAR->volume, 8-K->volume — #187/#197), not direction.
The directional battery (cross_sectional_ls) can't evaluate that surface. These archetypes do, with
the ⭐ anti-fooling control the directional `BacktestResult` lacks:

  PARTIAL-R OWN-VOL CONTROL (the #187/#197 lesson, made first-class): most "magnitude/volume edge" is
  just "this is a volatile/active name". So we regress the per-row target on the name's OWN baseline
  vol (own_rv_20d) and report the COLLAPSE RATIO = |residual rank-IC| / |raw rank-IC|. Collapse ~0 =>
  the predictability was vol-persistence, NOT a net-new signal (the fooling caught). Collapse ~1 =>
  genuinely independent of vol persistence. This is the headline anti-fooling number, not optional.

  NET-OF-COST MEDIAN (the #197 tradeability gate): a magnitude predictor has no signed P&L, but a
  vol/straddle bet around the event is only tradeable if the realized |move| beats the straddle-cost
  proxy (own-baseline expected |move| + round-trip) on the MEDIAN event — a positive MEAN driven by a
  fat right tail is NOT tradeable. We report the median net bps + win-rate.

Columnar by construction (one Polars/NumPy expression, batch over the panel == per-event live), per the
single-implementation invariant. NO P&L / NO L/S leg (a magnitude/volume target is not a directional
bet) — the verdict is DESCRIPTIVE-ONLY unless the residual (post-collapse) signal is BOTH significant
AND median-net-positive, which the 8-K faithfulness anchor shows it is NOT.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from quantlib.backtest import mean_ic, newey_west_tstat, per_timestamp_ic, shuffle_within_groups
from quantlib.battery.panel import Panel
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing

MIN_CROSS_SECTION = 20
N_FOLDS = 5
# the non-direction archetypes are daily/overnight-cadence (the targets are forward daily aggregates)
_DEFAULT_HORIZON = Horizon.OVERNIGHT
_DEFAULT_CONDITIONER = Conditioner.NONE
_DEFAULT_SIZING = Sizing.EW


class TargetKind(str, Enum):
    """The non-direction target families (each owns a panel column + the own-vol control)."""

    ABSRET = "fwd_absret_1d"  # the realized abnormal MOVE magnitude (#197 absret — the prize candidate)
    RV = "fwd_rv_5d"  # forward realized vol (the range/vol-magnitude surface)
    LOGVOL = "fwd_logvol_1d"  # forward log dollar-volume (the participation/intensity surface)

    @property
    def is_volume(self) -> bool:
        return self is TargetKind.LOGVOL


@dataclass
class MagnitudeVolumeResult:
    """The non-direction evidence bundle. The partial-r collapse + net-of-cost median are the
    verdict drivers (a magnitude/volume target has no L/S P&L)."""

    spec: ArchetypeSpec
    target: TargetKind

    # predictability of the cell's signal vs the magnitude/volume target
    raw_ic: float  # cross-sectional rank-IC (feature -> target)
    nw_t: float
    shuffle_ic: float  # the within-ts shuffle canary (must be ~0)
    edge_vs_shuffle: float
    n_test_ts: int
    n_rows: int

    # ⭐ the partial-r own-vol control (the anti-fooling headline)
    resid_ic: float  # rank-IC AFTER partialling own-vol out of BOTH the signal and target
    collapse: float  # |resid_ic| / |raw_ic| — ~0 = vol-persistence, ~1 = net-new

    # the tradeability gate (magnitude only; the straddle-cost proxy from #197)
    net_bps_median: float | None  # median realized |move| - (own-baseline expected |move| + round-trip)
    win_rate: float | None

    directional: bool = False  # magnitude/volume is NEVER directional (trap #2)
    verdict: str = "DESCRIPTIVE-ONLY"
    verdict_reason: str = ""
    by_window: dict[str, float] = field(default_factory=dict)


# A target SURVIVES the anti-fooling gate iff it predicts AND that prediction is NOT own-vol-explained
# AND (for a magnitude move) it is median-net-positive. The 8-K anchor fails the last two by design.
COLLAPSE_NETNEW = 0.30  # collapse >= 0.30 => meaningfully independent of vol persistence
GATE_IC = 0.02
GATE_NW_T = 2.0


def _residualize_on_own_vol(values: np.ndarray, own_vol: np.ndarray) -> np.ndarray:
    """Partial out the name's own baseline vol: regress `values` on log(own_vol) (cross-section-wide)
    and return the residual. The #197 own_vol_collapse control, applied to a vector. NaN-safe."""
    out = np.full_like(values, np.nan)
    finite = np.isfinite(values) & np.isfinite(own_vol) & (own_vol > 0)
    if finite.sum() < 30:
        return out
    y = values[finite]
    x = np.log(own_vol[finite])
    if np.std(x) < 1e-12:
        out[finite] = y - np.median(y)
        return out
    design = np.column_stack([np.ones(x.size), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    out[finite] = y - design @ beta
    return out


def evaluate_magnitude_volume(
    panel: Panel,
    spec: ArchetypeSpec,
    target: TargetKind,
    *,
    seed: int = 13,
    signal_col: int = 0,
) -> MagnitudeVolumeResult:
    """Score the cell's signal against a magnitude/volume target with the partial-r own-vol control.

    The signal is the leading feature column (the raw-rank fast path — columnar, batch==per-event). We
    measure: (1) the raw cross-sectional rank-IC vs the target, (2) the shuffle canary, (3) the
    own-vol-RESIDUALIZED rank-IC + the collapse ratio (the anti-fooling headline), and (4) the
    net-of-cost median for an absret move."""
    label = panel.extra[target.value]
    own_vol = panel.extra["own_rv_20d"]
    signal = panel.feature_matrix[:, signal_col].astype(float)
    minute = panel.minute_epoch

    keep = np.isfinite(label) & np.isfinite(signal)
    idx = np.where(keep)[0]
    sig = signal[idx]
    lab = label[idx]
    vol = own_vol[idx]
    groups = [int(m) for m in minute[idx]]

    # raw cross-sectional rank-IC of the signal vs the magnitude/volume target
    real = per_timestamp_ic(list(sig), list(lab), groups, min_names=MIN_CROSS_SECTION)
    raw_ic = mean_ic(real)
    shuffled = shuffle_within_groups(list(lab), groups, seed)
    shuf = per_timestamp_ic(list(sig), shuffled, groups, min_names=MIN_CROSS_SECTION)
    shuffle_ic = mean_ic(shuf)
    nw_t = newey_west_tstat(real, max(1, spec.horizon_minutes // spec.cadence_min))

    # ⭐ partial-r own-vol control: residualize BOTH the target and the signal on own-vol, re-IC.
    # If the predictability was "volatile name predicts more vol", residualizing kills it -> collapse~0.
    resid_label = _residualize_on_own_vol(lab, vol)
    resid_signal = _residualize_on_own_vol(sig, vol)
    both = np.isfinite(resid_label) & np.isfinite(resid_signal)
    resid_groups = [groups[i] for i in range(len(groups)) if both[i]]
    resid_real = per_timestamp_ic(
        list(resid_signal[both]), list(resid_label[both]), resid_groups, min_names=MIN_CROSS_SECTION
    )
    resid_ic = mean_ic(resid_real)
    collapse = abs(resid_ic) / abs(raw_ic) if (raw_ic == raw_ic and abs(raw_ic) > 1e-9) else float("nan")

    net_bps_median, win_rate = (None, None)
    if target is TargetKind.ABSRET:
        net_bps_median, win_rate = _net_of_cost_median(lab, vol)

    result = MagnitudeVolumeResult(
        spec=spec,
        target=target,
        raw_ic=raw_ic,
        nw_t=nw_t,
        shuffle_ic=shuffle_ic,
        edge_vs_shuffle=(raw_ic - shuffle_ic) if raw_ic == raw_ic else float("nan"),
        n_test_ts=len(real),
        n_rows=len(idx),
        resid_ic=resid_ic,
        collapse=collapse,
        net_bps_median=net_bps_median,
        win_rate=win_rate,
    )
    result.verdict, result.verdict_reason = _decide(result)
    return result


def _net_of_cost_median(absret: np.ndarray, own_vol: np.ndarray) -> tuple[float, float]:
    """The #197 straddle-cost gate, on the MEDIAN event: the realized |move| must beat the own-baseline
    expected move (own_vol as the straddle premium proxy) + a 10bps round-trip. Median, not mean —
    a positive mean driven by a fat right tail is NOT tradeable. Returns (median net bps, win-rate)."""
    finite = np.isfinite(absret) & np.isfinite(own_vol) & (own_vol > 0)
    if finite.sum() < 30:
        return float("nan"), float("nan")
    realized = absret[finite]
    premium = own_vol[finite]  # the expected |move| an event-vol bet pays for
    cost = premium + 2.0 * 10.0 / 1e4  # premium + 10bps round-trip (in + out)
    net = realized - cost
    return float(np.median(net) * 1e4), float(np.mean(realized > cost))


def evaluate_magnitude_volume_battery(
    panel: Panel, *, seed: int = 13, signal_col: int = 0
) -> dict[str, MagnitudeVolumeResult]:
    """One-call non-direction battery: run ALL magnitude/volume targets over a loaded daily panel and
    return {target -> result}. This is the 'does this feature buy a tradeable MAGNITUDE/VOLUME edge?'
    question Modeller's non-direction pivot needs — every cell carries the partial-r own-vol collapse
    (so vol-persistence is auto-flagged) + the net-of-cost median (so a fat-tail mean can't pass)."""
    spec = ArchetypeSpec("magnitude_volume", _DEFAULT_HORIZON, _DEFAULT_CONDITIONER, _DEFAULT_SIZING)
    out: dict[str, MagnitudeVolumeResult] = {}
    for target in TargetKind:
        out[target.name.lower()] = evaluate_magnitude_volume(
            panel, spec, target, seed=seed, signal_col=signal_col
        )
    return out


def _decide(result: MagnitudeVolumeResult) -> tuple[str, str]:
    """A magnitude/volume target is DESCRIPTIVE-ONLY (never a directional P&L). It is flagged
    TRADEABLE-CANDIDATE only if it (a) predicts (IC + NW-t), (b) does NOT collapse under own-vol
    (>= COLLAPSE_NETNEW = net-new, not vol-persistence), AND (c) for a move, is median-net-POSITIVE.
    The 8-K anchor fails (b) for vol/volume and (c) for absret — exactly the honest null."""
    predicts = (
        result.raw_ic >= GATE_IC and abs(result.nw_t) >= GATE_NW_T if result.nw_t == result.nw_t else False
    )
    if not predicts:
        return "DESCRIPTIVE-ONLY", "no cross-sectional predictability of the target (IC/t gate)"
    net_new = result.collapse == result.collapse and result.collapse >= COLLAPSE_NETNEW
    if not net_new:
        return (
            "DESCRIPTIVE-ONLY",
            f"predictability COLLAPSES under own-vol control (collapse={result.collapse:.3f} < "
            f"{COLLAPSE_NETNEW}) — vol-persistence, not net-new",
        )
    if result.net_bps_median is not None and result.net_bps_median <= 0.0:
        return (
            "DESCRIPTIVE-ONLY",
            f"net-new but median-NEGATIVE net-of-cost ({result.net_bps_median:.1f}bps) — the typical "
            f"event does not beat the straddle cost",
        )
    return "TRADEABLE-CANDIDATE", "net-new (survives own-vol) AND median-net-positive — warrants replication"
