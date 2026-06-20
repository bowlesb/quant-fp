"""Within-Day Parity Certifier — PHASE 2a: the mismatch ROOT-CAUSE classifier.

Per docs/WITHIN_DAY_PARITY_CERTIFICATION.md §1.1. When the phase-1 compare surfaces a real mismatch (on
clean symbols, in coverage, coherent minutes — NOT one of the §2.4 known non-bugs), this maps the
mismatch SIGNATURE to the likely code path so the fix-loop knows where to look:

    live side  : incremental.py / stateful.py / aggregates.py (the fast/stream compute)
    backfill   : raw_loaders.py / materialize.py (the canonical slow compute)

It produces a TRIAGE REPORT, not a fix — the human/agent writes the fix as a worktree→PR that routes
through the Lead to deploy (the WDPC NEVER auto-pushes code to the live pipeline). It also screens the
THREE known non-bugs (FP_TICK_SYMBOLS coverage gap / warm-up / capture-start, §2.4) so they are reported
as ARTIFACT, never as a code defect to chase.

Read-only: it reasons over the compare summary + a small sample of diverging exemplar cells; it touches
no store, DB, or live state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

# Long-window features warming from the capture anchor diverge on long windows when capture started
# mid-session — the capture-start artifact (§2.4). A window whose lookback exceeds the minutes-since-open
# at the compared minute is still warming on BOTH sides, so a divergence there is structural, not a bug.
_LONG_WINDOW_ARTIFACT_MINUTES = (
    90  # > the 90-min warm-up: above this, treat long-window divergence as suspect-artifact
)

# Verdict-signature → (suspected side, suspected modules, human note). The fix-loop reads `modules`.
ARTIFACT = "artifact"
LIVE_FAST_PATH = "live_fast_path"
BACKFILL_PATH = "backfill_path"
SIGN_CONVENTION = "sign_convention"
DEGENERATE_GUARD = "degenerate_guard"
COVERAGE_GAP = "coverage_gap"
UNKNOWN = "unknown"

_CLASS_MODULES = {
    LIVE_FAST_PATH: ("incremental.py", "aggregates.py"),
    BACKFILL_PATH: ("raw_loaders.py", "materialize.py"),
    SIGN_CONVENTION: ("stateful.py", "raw_loaders.py"),
    DEGENERATE_GUARD: ("incremental.py", "compare.py"),
    COVERAGE_GAP: (),
    ARTIFACT: (),
    UNKNOWN: ("incremental.py", "aggregates.py", "raw_loaders.py", "materialize.py"),
}


@dataclass
class RootCause:
    feature: str
    classification: str
    suspected_modules: tuple[str, ...]
    note: str
    n_mismatch: int
    n_extra_live: int
    n_missing_live: int
    value_rate: float | None
    exemplars: list[dict[str, object]] = field(default_factory=list)

    def is_actionable(self) -> bool:
        """True if this is a real code defect to fix (not a known artifact / coverage gap)."""
        return self.classification not in (ARTIFACT, COVERAGE_GAP)


def _is_long_window_feature(feature: str) -> bool:
    """A feature whose name encodes a window > the warm-up (e.g. *_120m, *_180m) — long-window cells are
    the capture-start artifact suspects."""
    for token in feature.replace("-", "_").split("_"):
        if token.endswith("m") and token[:-1].isdigit():
            if int(token[:-1]) > _LONG_WINDOW_ARTIFACT_MINUTES:
                return True
    return False


def classify_feature(
    feature: str,
    n_mismatch: int,
    n_extra_live: int,
    n_missing_live: int,
    value_rate: float | None,
    is_tick_feature: bool,
    on_tick_symbol: bool,
    exemplars: list[dict[str, object]] | None = None,
) -> RootCause:
    """Classify ONE feature's mismatch signature into a likely cause + suspected modules.

    The order matters: screen the known NON-BUGS first (artifact / coverage), so they are never reported as
    a code defect to chase. Only a divergence that survives those screens is routed to a code path."""
    exemplars = exemplars or []

    # 1. KNOWN NON-BUG: a tick/quote feature compared on a non-subscribed symbol — null both sides, a
    #    coverage artifact (FP_TICK_SYMBOLS gap, §2.4), NOT a math bug.
    if is_tick_feature and not on_tick_symbol:
        return RootCause(
            feature,
            COVERAGE_GAP,
            _CLASS_MODULES[COVERAGE_GAP],
            "tick feature off the subscribed set — FP_TICK_SYMBOLS coverage gap, not a bug",
            n_mismatch,
            n_extra_live,
            n_missing_live,
            value_rate,
            exemplars,
        )

    # 2. KNOWN NON-BUG: a long-window feature diverging — capture-start / warm-up artifact (§2.4): the long
    #    lookback covers a different bar SET when capture started mid-session. Suspect-artifact, not a bug.
    if _is_long_window_feature(feature) and n_mismatch > 0:
        return RootCause(
            feature,
            ARTIFACT,
            _CLASS_MODULES[ARTIFACT],
            "long-window divergence — capture-start/warm-up artifact (different bar set), not a bug",
            n_mismatch,
            n_extra_live,
            n_missing_live,
            value_rate,
            exemplars,
        )

    # 3. extra_live dominates with ~no mismatch: live emitted cells backfill did not — over-capture / a
    #    busted/late minute the settle window shouldn't have included. Coverage/settle, not a math bug.
    if n_extra_live > 0 and n_mismatch == 0:
        return RootCause(
            feature,
            COVERAGE_GAP,
            _CLASS_MODULES[COVERAGE_GAP],
            "extra_live with no mismatch — over-capture / unsettled minute; widen SETTLE_LAG",
            n_mismatch,
            n_extra_live,
            n_missing_live,
            value_rate,
            exemplars,
        )

    if n_mismatch == 0:
        return RootCause(
            feature,
            ARTIFACT,
            _CLASS_MODULES[ARTIFACT],
            "no value mismatch — nothing to fix",
            n_mismatch,
            n_extra_live,
            n_missing_live,
            value_rate,
            exemplars,
        )

    # 4. REAL mismatch — route to a code path by the exemplar signature.
    classification = _classify_real_mismatch(exemplars)
    return RootCause(
        feature,
        classification,
        _CLASS_MODULES[classification],
        _NOTE_FOR[classification],
        n_mismatch,
        n_extra_live,
        n_missing_live,
        value_rate,
        exemplars,
    )


_NOTE_FOR = {
    SIGN_CONVENTION: "live/backfill differ in SIGN — signed-flow accumulation (stateful) vs trade signing (loaders)",
    DEGENERATE_GUARD: "stream NaN vs backfill NULL on a degenerate window — unguarded value>threshold after a kernel NaN (bb/#122, OLS/#131 class)",
    LIVE_FAST_PATH: "small float-eps divergence on otherwise-matching cells — incremental/aggregate cancellation (Welford/centered-std)",
    UNKNOWN: "mismatch with no clear signature — inspect both the incremental fast path and the backfill loaders",
}


def _classify_real_mismatch(exemplars: list[dict[str, object]]) -> str:
    """From a few diverging exemplar cells, infer which code path likely diverged."""
    if not exemplars:
        return UNKNOWN
    n = 0
    n_sign_flip = 0
    n_nan_vs_null = 0
    n_small_eps = 0
    for cell in exemplars:
        live = cell.get("stream_value")
        back = cell.get("backfill_value")
        n += 1
        if live is None or back is None:
            n_nan_vs_null += 1
            continue
        try:
            live_f = float(live)  # type: ignore[arg-type]
            back_f = float(back)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if live_f == live_f and back_f == back_f:  # both non-NaN
            if (live_f < 0) != (back_f < 0) and abs(live_f) > 0 and abs(back_f) > 0:
                n_sign_flip += 1
            elif abs(back_f) > 0 and abs(live_f - back_f) <= 1e-3 * abs(back_f):
                n_small_eps += 1
        else:
            n_nan_vs_null += 1
    if n == 0:
        return UNKNOWN
    if n_sign_flip / n >= 0.5:
        return SIGN_CONVENTION
    if n_nan_vs_null / n >= 0.5:
        return DEGENERATE_GUARD
    if n_small_eps / n >= 0.5:
        return LIVE_FAST_PATH
    return UNKNOWN


def triage_report(causes: list[RootCause]) -> pl.DataFrame:
    """A human-readable triage table over a group's per-feature root causes (actionable first)."""
    rows = [
        {
            "feature": cause.feature,
            "classification": cause.classification,
            "actionable": cause.is_actionable(),
            "suspected_modules": ", ".join(cause.suspected_modules),
            "n_mismatch": cause.n_mismatch,
            "value_rate": cause.value_rate,
            "note": cause.note,
        }
        for cause in causes
    ]
    frame = pl.DataFrame(rows)
    if frame.height == 0:
        return frame
    return frame.sort(["actionable", "n_mismatch"], descending=[True, True])
