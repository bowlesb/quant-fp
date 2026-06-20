"""Anti-cheat self-proofs — the harness proving it is NOT fooling itself (Ben's central concern,
REQ-A1 in docs/STRATEGY_EXECUTION_ABSTRACTION.md §6.3).

Each proof CONTROLS the feature and runs it through the SAME battery machinery against REAL labels +
the REAL cost model, then asserts the verdict an honest harness must reach. They are the executable
form of "how do we know we're not fooling ourselves":

  - noise        : a pure-noise feature must NOT light up the leaderboard (after BY-FDR). If random
                   noise earns "edge", the harness manufactures signal — fatal.
  - planted      : a feature built from the FORWARD label (+noise) MUST be detected — else the harness
                   is too blunt to find a real edge that exists.
  - look_ahead   : a genuine as-of-t feature vs the SAME feature shifted FORWARD one bar (a peek). The
                   peeking version must NOT show a larger surviving edge under the walk-forward purge;
                   if peeking helps, the purge/embargo is broken (silent look-ahead).
  - shuffle      : the within-timestamp shuffle canary on the planted feature must collapse the edge
                   to ~0 — the leakage arbiter.
  - tradeable    : the panel's earliest entry minute is >= 09:35 ET (never the 09:30 print).

These run on a daily `Panel` (real overnight labels) with the feature column SWAPPED — so only the
feature is synthetic; the labels, the cost model, the folds, BY-FDR are all the production path.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

import numpy as np

from quantlib.battery.family import benjamini_yekutieli, one_sided_p_from_t
from quantlib.battery.panel import Panel
from quantlib.battery.result import Verdict
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing
from quantlib.battery.strategy import CrossSectionalLS

_OVERNIGHT = ArchetypeSpec("cross_sectional_ls", Horizon.OVERNIGHT, Conditioner.NONE, Sizing.EW)


@dataclass
class AntiCheatResult:
    name: str
    passed: bool
    detail: str


def _panel_with_feature(panel: Panel, feature: np.ndarray) -> Panel:
    """A copy of `panel` whose feature matrix is the single engineered `feature` column (the raw
    fast-path ranks column 0). Everything else — labels via exec prices, cost, folds — is unchanged."""
    return replace(
        panel,
        feature_names=["__probe__"],
        feature_matrix=feature.reshape(-1, 1).astype(float),
    )


def _overnight_label(panel: Panel) -> np.ndarray:
    """The REAL overnight forward-excess label the strategy would grade against (so a probe feature is
    tested against genuine forward returns, not a synthetic target)."""
    label, _ = CrossSectionalLS(_OVERNIGHT, use_gbm=False).label(panel)
    return label


def _run(panel: Panel, feature: np.ndarray, seed: int = 13):
    return CrossSectionalLS(_OVERNIGHT, seed=seed, use_gbm=False).backtest(
        _panel_with_feature(panel, feature)
    )


def proof_noise(panel: Panel, *, seed: int = 13) -> AntiCheatResult:
    """A pure-noise feature must NOT survive BY-FDR (empty leaderboard)."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, panel.n_rows)
    result = _run(panel, noise, seed=seed)
    pval = one_sided_p_from_t(result.nw_t)
    correction = benjamini_yekutieli(["noise"], [pval], q=0.10, pre_registered=True)
    survived = bool(correction.reject[0]) and result.verdict == Verdict.PASS
    return AntiCheatResult(
        name="noise",
        passed=not survived,
        detail=f"IC={result.mean_ic:+.5f} NW_t={result.nw_t:+.3f} BY_p={pval:.3f} "
        f"verdict={result.verdict.value} survived={survived} (want survived=False)",
    )


def proof_planted(panel: Panel, *, edge: float = 5.0, seed: int = 13) -> AntiCheatResult:
    """A feature = edge * forward_label + noise MUST be detected (positive edge vs shuffle, IC clearly
    above 0). Tests the harness is not too blunt to see a real signal that genuinely exists."""
    rng = np.random.default_rng(seed)
    label = _overnight_label(panel)
    clean = np.where(np.isfinite(label), label, 0.0)
    feature = edge * clean + rng.normal(0.0, np.nanstd(clean) + 1e-9, panel.n_rows)
    result = _run(panel, feature, seed=seed)
    detected = result.mean_ic > 0.02 and result.edge_vs_shuffle > 0.02
    return AntiCheatResult(
        name="planted",
        passed=bool(detected),
        detail=f"IC={result.mean_ic:+.5f} edge_vs_shuffle={result.edge_vs_shuffle:+.5f} "
        f"NW_t={result.nw_t:+.3f} detected={detected} (want IC>0.02 & edge>0.02)",
    )


def proof_shuffle(panel: Panel, *, edge: float = 5.0, seed: int = 13) -> AntiCheatResult:
    """The within-timestamp shuffle canary on the planted feature collapses the edge to ~0."""
    rng = np.random.default_rng(seed)
    label = _overnight_label(panel)
    clean = np.where(np.isfinite(label), label, 0.0)
    feature = edge * clean + rng.normal(0.0, np.nanstd(clean) + 1e-9, panel.n_rows)
    result = _run(panel, feature, seed=seed)
    canary_near_zero = abs(result.shuffle_canary.ic) < 0.01
    return AntiCheatResult(
        name="shuffle",
        passed=bool(canary_near_zero),
        detail=f"real_IC={result.mean_ic:+.5f} shuffle_canary_IC={result.shuffle_canary.ic:+.5f} "
        f"near_zero={canary_near_zero} (want |canary|<0.01)",
    )


def proof_look_ahead(panel: Panel, *, seed: int = 13) -> AntiCheatResult:
    """A genuine as-of-t feature vs the SAME feature shifted FORWARD one bar per symbol (a peek at the
    next bar). Under a correct walk-forward purge, peeking must NOT yield a materially larger surviving
    edge; if it does, the purge is broken. We use the real `ret_co_1d`-like momentum proxy: prior-day
    overnight-excess; the peeked version uses tomorrow's value (look-ahead)."""
    label = _overnight_label(panel)
    base = np.where(np.isfinite(label), label, 0.0)  # use the (real) signal surface as the probe base
    # honest probe: yesterday's label value (shift BACK one bar per symbol = as-of-t, no peek)
    honest = _shift_within_symbol(base, panel.symbol_code, +1)
    # cheating probe: tomorrow's label value (shift FORWARD one bar = a peek at the future)
    peek = _shift_within_symbol(base, panel.symbol_code, -1)
    honest_res = _run(panel, honest, seed=seed)
    peek_res = _run(panel, peek, seed=seed)
    # the peek must NOT produce a large edge the purge failed to remove. A correct purge keeps the
    # peeked edge from dominating; we require the peek's surviving edge_vs_shuffle to be small.
    peek_contained = peek_res.edge_vs_shuffle < 0.05
    return AntiCheatResult(
        name="look_ahead",
        passed=bool(peek_contained),
        detail=f"honest_edge={honest_res.edge_vs_shuffle:+.5f} peek_edge={peek_res.edge_vs_shuffle:+.5f} "
        f"peek_contained={peek_contained} (want peek_edge<0.05 — purge removes the look-ahead)",
    )


def proof_tradeable_entry(panel: Panel) -> AntiCheatResult:
    """The earliest entry minute in the panel is >= 09:35 ET (13:35 UTC) — never the 09:30 print."""
    if panel.minute_epoch.size == 0:
        return AntiCheatResult("tradeable_entry", True, "empty panel")
    earliest = min(dt.datetime.fromtimestamp(int(ns) / 1e9, tz=dt.timezone.utc) for ns in panel.minute_epoch)
    minute_of_day = earliest.hour * 60 + earliest.minute
    ok = minute_of_day >= (13 * 60 + 35)
    return AntiCheatResult(
        name="tradeable_entry",
        passed=ok,
        detail=f"earliest_entry_utc={earliest.time().isoformat()} (>= 13:35 UTC == 09:35 ET) ok={ok}",
    )


def _shift_within_symbol(values: np.ndarray, symbol_code: np.ndarray, k: int) -> np.ndarray:
    """Shift `values` by k rows within each contiguous symbol block. k>0 = use a PAST row (as-of-t);
    k<0 = use a FUTURE row (a peek). NaN-filled at the boundary -> 0 (no signal there)."""
    out = np.full_like(values, np.nan)
    n = values.size
    if abs(k) >= n:
        return np.zeros_like(values)
    if k > 0:
        same = symbol_code[k:] == symbol_code[:-k]
        out[k:][same] = values[:-k][same]
    elif k < 0:
        j = -k
        same = symbol_code[:-j] == symbol_code[j:]
        out[:-j][same] = values[j:][same]
    return np.where(np.isfinite(out), out, 0.0)


def run_all(panel: Panel, *, seed: int = 13) -> list[AntiCheatResult]:
    """Run every anti-cheat proof on the panel. ALL must pass for the harness to be trusted."""
    return [
        proof_noise(panel, seed=seed),
        proof_planted(panel, seed=seed),
        proof_shuffle(panel, seed=seed),
        proof_look_ahead(panel, seed=seed),
        proof_tradeable_entry(panel),
    ]
