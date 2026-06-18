"""Per-feature-type trust policy + provenance helpers (docs/TRUST_REDESIGN.md).

Trust is binary and earned: a ``(feature, version)`` becomes TRUSTED when, on ONE clean regular-session
day, its stream value reproduced its backfill recomputation within the feature's tolerance on at least
``min_pass_rate`` of compared cells — UNLESS it is deterministic-by-construction (pure timestamp), in
which case parity is guaranteed and it is trusted with no parity day.

"Within tolerance" is the existing relative-tolerance rule ``|stream - backfill| <= 1e-12 + rtol*|backfill|``.
This module sets the DEFAULT ``rtol`` + ``min_pass_rate`` per ``FeatureType`` (overridable per feature via
``FeatureSpec.tolerance``), and the determinism classification. It is the single auditable home for "what
threshold was required to trust this kind of feature".
"""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
from dataclasses import dataclass

from quantlib.features.base import FeatureType

MIN_CLEAN_DAYS_TO_TRUST = 1  # one clean day earns trust (was 2); bought back by random re-checks


@dataclass(frozen=True)
class TrustTolerance:
    """The parity bar for a feature kind: relative tolerance, the fraction of cells that must match, and
    whether the kind is deterministic-by-construction (no parity day needed) or must be validated over the
    FULL universe (cross-sectional features falsely fail on a sampled subset)."""

    rtol: float
    min_pass_rate: float
    deterministic: bool = False
    full_universe: bool = False


_EXACT = TrustTolerance(rtol=1e-6, min_pass_rate=0.9999)
_WINDOWED = TrustTolerance(rtol=1e-4, min_pass_rate=0.999)
_FLOW = TrustTolerance(rtol=1e-4, min_pass_rate=0.999)
_XS = TrustTolerance(rtol=1e-4, min_pass_rate=0.999, full_universe=True)
_DETERMINISTIC = TrustTolerance(rtol=0.0, min_pass_rate=1.0, deterministic=True)

TYPE_POLICY: dict[FeatureType, TrustTolerance] = {
    FeatureType.CALENDAR: _DETERMINISTIC,
    FeatureType.PRICE: _EXACT,
    FeatureType.VOLUME: _EXACT,
    FeatureType.PRICE_VOLUME: _EXACT,
    FeatureType.CANDLESTICK: _EXACT,
    FeatureType.REFERENCE: _EXACT,
    FeatureType.TECHNICAL: _WINDOWED,
    FeatureType.VOLATILITY: _WINDOWED,
    FeatureType.MOMENTUM: _WINDOWED,
    FeatureType.TREND_QUALITY: _WINDOWED,
    FeatureType.MULTI_DAY: _WINDOWED,
    FeatureType.TRADE_FLOW: _FLOW,
    FeatureType.MICROSTRUCTURE: _FLOW,
    FeatureType.QUOTE_SPREAD: _FLOW,
    FeatureType.CROSS_SECTIONAL: _XS,
}

_DEFAULT = _WINDOWED  # an unseen type defaults to the looser windowed bar, never silently exact


def policy_for(feature_type: FeatureType, spec_tolerance: float | None = None) -> TrustTolerance:
    """The trust bar for a feature: its type default, with ``FeatureSpec.tolerance`` overriding ``rtol``
    when the feature declares one different from the engine default (1e-6). The per-feature tolerance wins
    because a feature author who set it knows that feature's reproducibility better than the type default."""
    base = TYPE_POLICY.get(feature_type, _DEFAULT)
    if spec_tolerance is not None and spec_tolerance != 1e-6:
        return TrustTolerance(
            rtol=spec_tolerance,
            min_pass_rate=base.min_pass_rate,
            deterministic=base.deterministic,
            full_universe=base.full_universe,
        )
    return base


def group_content_hash(group: object) -> str:
    """A blake2b digest of the group's compute SOURCE — the machine-derived companion to the human
    ``version`` label. If the compute code changes without a version bump, this hash changes and the
    random-check surfaces the mismatch (the trust grant was for different code). 16 hex chars is ample."""
    source = inspect.getsource(type(group))
    return hashlib.blake2b(source.encode("utf-8"), digest_size=8).hexdigest()


def current_git_commit() -> str | None:
    """Short HEAD commit of the repo, recorded in the trust grant so a verdict can be replayed against the
    exact code. Prefers ``FP_GIT_COMMIT`` (the ops scripts inject it from the HOST, since the fp-dev image
    has no git), else tries git in-container, else None — the grant is still valid (the content hash is the
    machine-derived code-provenance); the commit is just convenience metadata."""
    env_commit = os.environ.get("FP_GIT_COMMIT")
    if env_commit:
        return env_commit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd="/app",
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
