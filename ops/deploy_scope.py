"""Auto-deploy SCOPE — map a merge's changed paths to the affected services + their deploy tier.

docs/AUTO_DEPLOY.md. This is the PURE decision core of the continuous auto-deploy system (mirrors
``ci_scope`` for the merge gate): given a merge's changed-path list, it answers "which running containers are
now stale, and how is each safe to redeploy?" It runs no git/docker/live state, so it is fully unit-testable
offline.

Two deploy TIERS, fail-closed:

  * **TIER-1 (auto)** — a self-contained container whose code can be rebuilt + recreated by name WITHOUT
    touching the live feature-computer or moving the bus fingerprint: the dashboard, the store-grid worker,
    the news/edgar capture services, the individual trading strategies. These deploy IMMEDIATELY on merge via
    an isolated image build + ``docker compose up -d --no-deps --build <svc>`` (the proven #368/#382 pattern).
  * **TIER-2 (batched / coordinated)** — anything that changes the FEATURE-COMPUTE surface or the bus
    fingerprint (``quantlib/features/groups``, the registry, ``quantlib/bus/schema``, the Rust kernels, the fc
    service): these are NEVER hot-deployed mid-session. They BATCH onto the next coordinated market-closed
    relaunch (``ops/nightly_relaunch.sh``), behind the existing fingerprint-deploy discipline + Ben's gate.

A path that matches no known service is IGNORED for deploy (docs/tests/ops only change nothing running) —
EXCEPT a path under ``services/``/``strategies/`` we don't recognise, which ESCALATES (unknown container →
don't silently skip a stale service).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class DeployTier(str, Enum):
    """How a service is safe to redeploy."""

    AUTO = "tier-1-auto"  # rebuild image + recreate by name now (no fc touch, no fingerprint move)
    COORDINATED = "tier-2-coordinated"  # batch onto the Ben-gated market-closed relaunch (fc / fingerprint)


@dataclass(frozen=True)
class ServiceSpec:
    """One deployable service: its compose name, the compose file it lives in, and its deploy tier."""

    name: str  # the running container / compose service name
    compose_file: str  # the -f file for `docker compose` (the core file is docker-compose.yml)
    tier: DeployTier


# The fc / fingerprint surface: a change here moves the bus fingerprint or the live compute path → TIER-2,
# batched onto the coordinated relaunch. Checked FIRST so a feature change can never be misrouted to a hot
# rebuild. (Mirrors ci_scope.DANGER_PATTERNS; kept in lock-step.)
FC_SURFACE_PATTERNS: tuple[str, ...] = (
    r"^services/fc/",
    r"^services/ingestor/",
    r"^services/executor/",
    r"^quantlib/features/",  # any feature code can shift the fingerprint / compute
    r"^quantlib/bus/",
    r"^quantlib/strategy/",  # shared strategy lib compiled into the fc-adjacent path
    r"^quantlib/execution/",
    r"^rust/",  # compiled kernels → image roll, fingerprint-relevant
)
_FC_SURFACE_RE = [re.compile(pattern) for pattern in FC_SURFACE_PATTERNS]

# Path prefix -> the TIER-1 service it redeploys (rebuild-by-name, no fc touch). Order matters: first match
# wins. ``frontend/`` + ``services/dashboard/`` both map to the dashboard; the store-grid worker shares the
# dashboard image build but is its own container, so a dashboard-app change redeploys BOTH (handled in
# affected_services via the worker's own dependency on the dashboard image, expressed as a second prefix).
PATH_SERVICE_MAP: tuple[tuple[str, str], ...] = (
    ("services/dashboard/", "dashboard"),
    ("frontend/", "dashboard"),
    ("services/news_capture/", "news-capture"),
    ("services/news-capture/", "news-capture"),
    ("services/edgar/", "quant-edgar"),
    ("services/strategies/smoke/", "smoke-strategy"),
    ("services/strategies/reversion/", "reversion-strategy"),
    ("services/strategies/overnight_beta/", "overnight-beta-strategy"),
    ("services/crypto_strategy/", "crypto-momentum-strategy"),
    ("services/store_grid_worker/", "store-grid-worker"),
    ("ops/ci_watcher.py", "ci-grade-daemon"),  # pseudo-service: the daemon refreshes its own CI checkout
    ("ops/ci_scope.py", "ci-grade-daemon"),
    ("ops/deploy_scope.py", "ci-grade-daemon"),
    ("ops/ci_deploy.py", "ci-grade-daemon"),
    ("ops/auto_deploy.py", "ci-grade-daemon"),
    ("ops/deploy_queue.py", "ci-grade-daemon"),
)

# Service registry: name -> ServiceSpec. The compose file + tier for each deployable container. fc / fp-surface
# services are TIER-2 (coordinated). The ci-grade-daemon is a pseudo-service: "deploying" it = the guard's
# next `reset --hard origin/main` of /home/ben/.ci-repo (already automatic), so it is a no-op marker here.
SERVICE_REGISTRY: dict[str, ServiceSpec] = {
    "dashboard": ServiceSpec("dashboard", "docker-compose.yml", DeployTier.AUTO),
    "store-grid-worker": ServiceSpec("store-grid-worker", "docker-compose.yml", DeployTier.AUTO),
    "news-capture": ServiceSpec("news-capture", "docker-compose.news.yml", DeployTier.AUTO),
    "quant-edgar": ServiceSpec("quant-edgar", "docker-compose.yml", DeployTier.AUTO),
    "smoke-strategy": ServiceSpec("smoke-strategy", "docker-compose.strategies.yml", DeployTier.AUTO),
    "reversion-strategy": ServiceSpec(
        "reversion-strategy", "docker-compose.strategies.yml", DeployTier.AUTO
    ),
    "overnight-beta-strategy": ServiceSpec(
        "overnight-beta-strategy", "docker-compose.strategies.yml", DeployTier.AUTO
    ),
    "crypto-momentum-strategy": ServiceSpec(
        "crypto-momentum-strategy", "docker-compose.crypto-strategy.yml", DeployTier.AUTO
    ),
    # The fc itself: TIER-2, deployed ONLY by the coordinated relaunch (never by this system's applier).
    "feature-computer": ServiceSpec("feature-computer", "docker-compose.yml", DeployTier.COORDINATED),
    # Pseudo-service: the grade daemon self-refreshes its CI checkout; recorded for visibility, no container op.
    "ci-grade-daemon": ServiceSpec("ci-grade-daemon", "(none)", DeployTier.AUTO),
}

# Container-bearing path roots: a change under one of these that maps to NO known service ESCALATES (unknown
# stale container) rather than being silently ignored.
_CONTAINER_PATH_ROOTS = ("services/", "frontend/", "strategies/")


def _path_is_fc_surface(path: str) -> bool:
    return any(regex.search(path) for regex in _FC_SURFACE_RE)


@dataclass
class DeployPlan:
    """The deploy verdict for a merge: which services to redeploy, split by tier, + escalations.

    ``auto`` = TIER-1 services to redeploy now (rebuild-by-name). ``coordinated`` = TIER-2 (fc/fp surface) that
    batch onto the Ben-gated relaunch. ``unknown_paths`` = container-bearing paths with no mapped service
    (escalate). ``ignored`` True when the merge changed nothing deployable (docs/tests/pure-ops)."""

    auto: list[str] = field(default_factory=list)
    coordinated: list[str] = field(default_factory=list)
    unknown_paths: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ignored(self) -> bool:
        return not self.auto and not self.coordinated and not self.unknown_paths

    @property
    def needs_coordinated_relaunch(self) -> bool:
        return bool(self.coordinated)


def affected_services(changed_paths: list[str]) -> DeployPlan:
    """Map a merge's changed paths to the services to redeploy, each tier-classified, fail-closed.

    ``changed_paths`` = ``git diff --name-only <prev-deployed-sha> <new-main-sha>``.
    """
    auto: set[str] = set()
    coordinated: set[str] = set()
    unknown: list[str] = []
    reasons: list[str] = []

    for path in changed_paths:
        if _path_is_fc_surface(path):
            coordinated.add("feature-computer")
            continue
        matched: str | None = None
        for prefix, service in PATH_SERVICE_MAP:
            if path.startswith(prefix):
                matched = service
                break
        if matched is not None:
            spec = SERVICE_REGISTRY[matched]
            (coordinated if spec.tier is DeployTier.COORDINATED else auto).add(matched)
        elif any(path.startswith(root) for root in _CONTAINER_PATH_ROOTS):
            unknown.append(path)

    # The grade-daemon pseudo-service needs no container op (the guard auto-refreshes its checkout); drop it
    # from the AUTO deploy set but note it so the audit trail records the daemon picked up the change.
    daemon_touched = "ci-grade-daemon" in auto
    auto.discard("ci-grade-daemon")

    if coordinated:
        reasons.append(
            f"fc/fingerprint surface changed → BATCH for the coordinated relaunch (Ben-gated): "
            f"{sorted(coordinated)}"
        )
    if auto:
        reasons.append(f"TIER-1 services to auto-redeploy now (rebuild-by-name): {sorted(auto)}")
    if daemon_touched:
        reasons.append(
            "ops/ci|deploy code changed → the grade daemon refreshes its CI checkout automatically"
        )
    if unknown:
        reasons.append(f"UNKNOWN container-bearing paths (escalate — no mapped service): {unknown}")
    if not auto and not coordinated and not unknown:
        reasons.append("no deployable surface changed (docs/tests/pure-ops) — nothing to redeploy")

    return DeployPlan(
        auto=sorted(auto),
        coordinated=sorted(coordinated),
        unknown_paths=unknown,
        reasons=reasons,
    )


def _compose_prefix(spec: ServiceSpec) -> list[str]:
    """``docker compose`` + the right ``-f`` overlay for a service (the core file is implicit by default)."""
    compose = ["docker", "compose"]
    if spec.compose_file != "docker-compose.yml":
        compose += ["-f", "docker-compose.yml", "-f", spec.compose_file]
    return compose


def deploy_commands(service: str, git_sha: str) -> list[list[str]]:
    """The ordered ``docker compose`` steps to rebuild + recreate ONE TIER-1 service by name, SHA-stamped.

    TWO steps (mirrors the proven ``scripts/run_tool.sh`` pattern — the only place that already builds with a
    correct ``GIT_SHA``):

      1. ``docker compose build --build-arg GIT_SHA=<sha> <svc>`` — bake the deployed source SHA into the
         image (the services' Dockerfiles take ``ARG GIT_SHA`` → ``ENV GIT_SHA``). Without this the
         auto-built image stamps ``GIT_SHA=unknown``, so the deployed-sha verification surface (the dashboard
         footer / ``scripts/assert_image_fresh.sh`` / ``baked_sha``) reads ``unknown`` for every auto-deploy.
      2. ``docker compose up -d --no-deps <svc>`` — recreate ONLY that container from the freshly-built
         image (``--no-deps``, no ``--remove-orphans`` so sibling containers from other compose contexts are
         left untouched). No ``--build`` here: step 1 already built WITH the build-arg, and a second
         ``--build`` would drop the arg and overwrite the image with a ``GIT_SHA=unknown`` rebuild.

    The caller FFs the live tree first, computes ``git_sha`` from THAT tree (so the stamp matches the deployed
    code), and runs these with ``cwd``=the real repo dir so ``.env`` loads. NEVER returns commands for a
    TIER-2/fc service (raises — the applier refuses those).
    """
    spec = SERVICE_REGISTRY[service]
    if spec.tier is DeployTier.COORDINATED:
        raise ValueError(f"{service} is TIER-2 (coordinated) — not auto-deployable; use the relaunch window")
    compose = _compose_prefix(spec)
    build = [*compose, "build", "--build-arg", f"GIT_SHA={git_sha}", spec.name]
    up = [*compose, "up", "-d", "--no-deps", spec.name]
    return [build, up]
