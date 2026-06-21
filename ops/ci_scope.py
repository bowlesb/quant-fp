"""CI scope classification — the AUTO (TIER-1) vs GATED (TIER-2) safety boundary.

docs/CONTINUOUS_DEPLOY.md §"The safety boundary". A PR is TIER-1 (eligible for auto-merge + auto-deploy of a
single safe container) iff BOTH hold (fail-closed):

  1. FINGERPRINT UNCHANGED — ``BusSchema.from_registry().fingerprint`` byte-identical vs origin/main. A moved
     fingerprint means ``fc`` / every strategy must recompile → the Lead's controlled relaunch window only.
  2. SAFE-SURFACE ONLY     — every changed path is on the fp-neutral allowlist (dashboard / docs / tests /
     ops / frontend / non-fingerprint surfaces) and NONE is on the danger denylist (fc / strategy /
     crypto-capture / execution / the feature registry itself).

Anything else, or any ambiguity, → TIER-2 (gated). This module is PURE: it takes the already-gathered changed
path list + the two fingerprints (the caller computes them in the two worktrees) and decides. It runs no git
/ docker / live state itself, so it is fully unit-testable offline — mirroring within_day_scope_guard.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    """TIER-1 may auto-merge + auto-deploy a safe container; TIER-2 is gated to the Lead's window."""

    AUTO = "tier-1-auto"
    GATED = "tier-2-gated"


# Paths whose changes can NEVER auto-deploy — they touch the live trading hot path or the fingerprint.
# Checked FIRST: a single danger path forces TIER-2 even if everything else looks safe (fail-closed).
DANGER_PATTERNS: tuple[str, ...] = (
    r"^services/fc/",  # the live feature-computer
    r"^services/ingestor/",  # the live market-data ingest
    r"^services/executor/",  # order execution
    r"^services/strategies/",  # trading strategies
    r"^services/crypto[-_]capture/",  # crypto capture
    r"^services/news[-_]capture/",  # news capture
    r"^quantlib/features/groups/",  # feature definitions — fingerprint-affecting by construction
    r"^quantlib/features/registry",  # the registry itself
    r"^quantlib/features/declarative",  # declarative feature specs
    r"^quantlib/bus/schema",  # the fingerprint definition
    r"^quantlib/strategy/",  # strategy logic
    r"^quantlib/execution/",  # execution/state layer
    r"^rust/",  # compiled kernels — change the image, fingerprint-relevant
)

# Paths that are fp-neutral and safe to auto-merge (and, for the container subset, auto-deploy).
# A PR is SAFE-SURFACE only if EVERY changed path matches one of these AND none matches a danger pattern.
SAFE_PATTERNS: tuple[str, ...] = (
    r"^services/dashboard/",  # the dashboard container (TIER-1 deploy target)
    r"^frontend/",  # dashboard frontend assets
    r"^docs/",  # documentation
    r"^tests/",  # tests
    r"^ops/",  # ops scripts / this CI machinery
    r"^experiments/",  # research experiments (never live)
    r"^\.github/",  # CI config
    r"^[^/]+\.md$",  # top-level markdown (README etc.)
    r"^Makefile",  # build targets
    r"^docker-compose\.(news|crypto|strategies|fp)\.yml$",  # auxiliary compose overlays (not the core)
    r"^\.gitignore$",
)

_DANGER_RE = [re.compile(pattern) for pattern in DANGER_PATTERNS]
_SAFE_RE = [re.compile(pattern) for pattern in SAFE_PATTERNS]


def path_is_danger(path: str) -> bool:
    """True if the path touches the live hot path / fingerprint (forces TIER-2)."""
    return any(regex.search(path) for regex in _DANGER_RE)


def path_is_safe(path: str) -> bool:
    """True if the path is on the fp-neutral safe-surface allowlist."""
    return any(regex.search(path) for regex in _SAFE_RE)


@dataclass
class ScopeResult:
    """The classification + the exact reasons (so the CI comment can explain the verdict)."""

    tier: Tier
    fingerprint_unchanged: bool
    reasons: list[str] = field(default_factory=list)
    danger_paths: list[str] = field(default_factory=list)
    unrecognized_paths: list[str] = field(default_factory=list)

    @property
    def is_auto(self) -> bool:
        return self.tier is Tier.AUTO


def classify(
    changed_paths: list[str],
    fingerprint_base: int,
    fingerprint_head: int,
) -> ScopeResult:
    """Classify a PR as TIER-1 (auto) or TIER-2 (gated), fail-closed.

    ``changed_paths`` = ``git diff --name-only origin/main...HEAD`` for the PR.
    ``fingerprint_base`` / ``fingerprint_head`` = ``BusSchema.from_registry().fingerprint`` computed in the
    origin/main worktree and the PR worktree respectively.
    """
    reasons: list[str] = []

    fingerprint_unchanged = fingerprint_base == fingerprint_head
    if not fingerprint_unchanged:
        reasons.append(
            f"fingerprint moved {fingerprint_base:#018x} -> {fingerprint_head:#018x} "
            "(fc + strategies must recompile)"
        )

    if not changed_paths:
        # No diff vs base — nothing to deploy. Gate it (fail-closed): an empty PR should not auto-merge.
        reasons.append("empty diff vs origin/main (nothing to merge)")
        return ScopeResult(
            tier=Tier.GATED,
            fingerprint_unchanged=fingerprint_unchanged,
            reasons=reasons,
        )

    danger_paths = [path for path in changed_paths if path_is_danger(path)]
    if danger_paths:
        reasons.append(f"touches live hot-path / fingerprint surface: {danger_paths}")

    unrecognized_paths = [
        path for path in changed_paths if not path_is_safe(path) and not path_is_danger(path)
    ]
    if unrecognized_paths:
        reasons.append(f"unrecognized (not on the safe allowlist): {unrecognized_paths}")

    is_auto = fingerprint_unchanged and not danger_paths and not unrecognized_paths
    tier = Tier.AUTO if is_auto else Tier.GATED
    if is_auto:
        reasons.append("fp-neutral + every path on the safe allowlist — eligible for AUTO")

    return ScopeResult(
        tier=tier,
        fingerprint_unchanged=fingerprint_unchanged,
        reasons=reasons,
        danger_paths=danger_paths,
        unrecognized_paths=unrecognized_paths,
    )


# Changed-path prefix -> the single safe container to restart for an auto-deploy (Phase 3). Anything not
# mapped here is NOT auto-deployable (escalate). fc / strategies are deliberately absent — they can NEVER
# appear, by construction, because a change touching them is already TIER-2 (DANGER_PATTERNS).
DEPLOY_TARGETS: dict[str, str] = {
    "services/dashboard/": "dashboard",
    "frontend/": "dashboard",
}


def deploy_target(changed_paths: list[str]) -> str | None:
    """The single safe container to restart for these changes, or None if not auto-deployable.

    Returns a target ONLY if every change maps to the SAME container (a deploy touches one service). If the
    changes span multiple containers, or any change touches a service-like path with no mapped container,
    returns None (escalate). Pure doc / test / ops / config changes (never under ``services/`` or
    ``frontend/``) carry no deploy target — they just merge, so they are ignored when deciding the target.
    """
    targets: set[str] = set()
    for path in changed_paths:
        matched = None
        for prefix, service in DEPLOY_TARGETS.items():
            if path.startswith(prefix):
                matched = service
                break
        if matched is not None:
            targets.add(matched)
        elif path.startswith("services/") or path.startswith("frontend/"):
            # A container-bearing path we don't know how to deploy → escalate, don't silently ignore.
            return None
    if len(targets) == 1:
        return targets.pop()
    return None
