"""Within-Day Parity Certifier — THE PER-GROUP LIFECYCLE ORCHESTRATOR (the single runnable a subagent OWNS).

The certification lifecycle was built as separate primitives — the assignment lock
(:mod:`within_day_assignment`), the Phase-3 monitor loop (:mod:`within_day_monitor`), the version-awareness
reset (:mod:`within_day_version`), and the FIFO deploy queue (:mod:`within_day_deploy_queue`). This module
is the ONE call that strings them together so a subagent can take a group all the way through the lifecycle
in a single invocation — the missing "run it end-to-end" entry point Ben asked for.

What one ``run_group_lifecycle`` call does, in order (docs/WITHIN_DAY_PARITY_CERTIFICATION.md §1 + §4 + §5):

  1. CLAIM the group via the disjoint assignment lock (:mod:`within_day_assignment`). The lock IS the claim
     in the orchestrator's hands — if another agent holds it, this call aborts (no two agents on one group).
  2. CHECK VERSION-AWARENESS up front (:func:`within_day_version.version_status`): if the live-running code's
     content hash diverged from a feature's trust grant (a hot-swap / refactor landed), the group is the §4
     "JUST-REFACTORED first" case — it must re-earn trust under the NEW code, which is exactly what the
     monitor is about to do. We surface that as the run's ``version_before`` so the subagent sees the state.
  3. MONITOR until certify (:func:`within_day_monitor.monitor`) — the streak loop that compares the settled
     window live==backfill, increments on a clean cycle, resets on a mismatch, and on a sustained streak
     stamps ``within_day_parity_cert`` + grants binary trust (both ``dry_run`` by default).
  4. ON CERTIFY, FIRE THE VERSION RESET (:func:`within_day_version.reset_trust_on_content_change`): a group
     whose deployed code changed gets its stale grants reset so the just-written cert re-earns under the live
     content hash — keeping (feature, version, content_hash) honest. (A no-op when nothing diverged.)
  5. CONSULT THE DEPLOY QUEUE (:func:`within_day_deploy_queue.pending`): report whether this group has a fix
     waiting in the FIFO for the serialized applier (:mod:`within_day_deploy_run`). The orchestrator does
     NOT apply — the live hot-swap is the Lead-gated seam — but a subagent owning the group needs to SEE
     that its queued fix is in line, closing Ben's "a subagent can see its new version reach prod" loop.

The orchestrator OWNS the group from claim to release; the monitor releases the lock on certify, and this
module releases it on any early exit (no-certify / abort) so a group is never left locked by a finished run.

⭐ SAFETY: every state mutation defaults to ``dry_run`` — the lock, the cert/trust grant, and the version
reset all stay inert unless the Lead enables them (``--write-lock`` / ``--write-cert``). The deploy-queue
read is a peek; the orchestrator never enqueues, claims, or applies a deploy. The whole lifecycle is
exercisable offline (dry-run + ``--max-cycles``) and the live activation is the Lead's gated step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from dataclasses import dataclass, field

from quantlib.features import (
    within_day_assignment,
    within_day_deploy_queue,
    within_day_monitor,
    within_day_version,
)
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_parity import DEFAULT_SAMPLE_SIZE, DEFAULT_WINDOW_MINUTES
from quantlib.features.within_day_trust import CertResult
from quantlib.features.within_day_version import VersionStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("within_day_run")


@dataclass
class LifecycleResult:
    """The outcome of one per-group lifecycle run — what the owning subagent reports back.

    ``certified`` is the monitor's first-feature CertResult on a successful streak (None if the run ended
    without certifying — aborted claim, max-cycles, or a persistent mismatch). The version/queue fields make
    the run legible without a second DB round-trip: what the deployed-vs-trusted state was when the run
    started, what the version reset touched on certify, and whether a fix is queued for this group."""

    group_name: str
    agent_id: str
    claimed: bool
    certified: CertResult | None = None
    version_before: list[tuple[str, str]] = field(default_factory=list)  # (feature, VersionStatus.value)
    reset_features: list[str] = field(default_factory=list)
    queued_jobs: int = 0

    @property
    def did_certify(self) -> bool:
        return self.certified is not None

    @property
    def diverged_features(self) -> list[str]:
        """Features whose LIVE code diverged from their trust grant when the run started (the §4 just-
        refactored case the monitor re-earns). Empty when the deployed code already matched every grant."""
        return [
            feature for feature, status in self.version_before if status == VersionStatus.LIVE_DIVERGED.value
        ]


def _version_snapshot(group_name: str, *, dry_run: bool) -> list[tuple[str, str]]:
    """The per-feature (feature, version-status) of the LIVE-running group vs its trust grant — the §4
    ordering signal (a diverged feature is the just-refactored, must-re-earn case). dry_run reads no DB
    (everything NOT_REGISTERED)."""
    group = within_day_version.group_by_name(group_name)
    if group is None:
        return []
    return [
        (report.feature, report.status.value)
        for report in within_day_version.version_status(group, dry_run=dry_run)
    ]


def _consult_deploy_queue(group_name: str, *, dry_run: bool) -> int:
    """How many fixes for ``group_name`` are waiting in the FIFO deploy queue for the serialized applier —
    the "is my fix in line to reach prod?" peek (read-only, never enqueues/claims). dry_run returns 0
    (``pending`` opens no DB in dry-run)."""
    return sum(1 for job in within_day_deploy_queue.pending(dry_run=dry_run) if job.group_name == group_name)


def _fire_version_reset(group_name: str, *, dry_run: bool) -> list[str]:
    """On certify, reset trust for any feature whose deployed content hash diverged from its grant so the
    fresh cert re-earns under the live code (idempotent no-op when nothing diverged)."""
    group = within_day_version.group_by_name(group_name)
    if group is None:
        logger.warning("version reset skipped: group %s not registered", group_name)
        return []
    return within_day_version.reset_trust_on_content_change(group, dry_run=dry_run)


def run_group_lifecycle(
    feature_root: str,
    group_name: str,
    agent_id: str,
    *,
    mode: str = "live",
    day: dt.date | None = None,
    poll_seconds: int = within_day_monitor.DEFAULT_POLL_SECONDS,
    stable_cycles_required: int = within_day_monitor.DEFAULT_STABLE_CYCLES,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    materialize_backfill: bool = False,
    dry_run_cert: bool = True,
    dry_run_lock: bool = True,
    max_cycles: int | None = None,
) -> LifecycleResult:
    """OWN ``group_name`` through the whole within-day lifecycle in one call — the subagent's entire job.

    claim (lock) → version snapshot → monitor-to-certify → on-certify version reset → deploy-queue peek.
    Returns a :class:`LifecycleResult` summarizing the run. The monitor releases the lock on certify; this
    function releases it on any early exit so the group is never left locked. ``materialize_backfill`` opts
    the monitor into on-demand settled-window materialization (the live-intraday path — see
    :mod:`within_day_monitor`); off keeps the swept-day behaviour.

    All writes (lock, cert, trust grant, version reset) honour ``dry_run_*`` — the live activation is the
    Lead's gated step. ``max_cycles`` bounds an offline / replay run."""
    REGISTRY.get_group(group_name)  # fail fast on a bad group name before any claim
    if not within_day_assignment.claim(group_name, agent_id, dry_run=dry_run_lock):
        logger.warning("LIFECYCLE abort: group=%s already held by another agent", group_name)
        return LifecycleResult(group_name=group_name, agent_id=agent_id, claimed=False)

    result = LifecycleResult(group_name=group_name, agent_id=agent_id, claimed=True)
    result.version_before = _version_snapshot(group_name, dry_run=dry_run_cert)
    if result.diverged_features:
        logger.info(
            "LIFECYCLE group=%s is JUST-REFACTORED (§4): %d feature(s) diverged from their grant — "
            "re-earning under the live content hash",
            group_name,
            len(result.diverged_features),
        )

    try:
        certified = within_day_monitor.monitor(
            feature_root,
            group_name,
            agent_id,
            mode=mode,
            day=day,
            poll_seconds=poll_seconds,
            stable_cycles_required=stable_cycles_required,
            window_minutes=window_minutes,
            sample_size=sample_size,
            materialize_backfill=materialize_backfill,
            dry_run_cert=dry_run_cert,
            # The orchestrator already holds the lock; the monitor must not re-claim it (a second claim
            # against a live lock would fail). It still heartbeats/releases under the same dry_run flag.
            dry_run_lock=dry_run_lock,
            claim_lock=False,
            max_cycles=max_cycles,
        )
    finally:
        # The monitor releases the lock on a successful certify; on any early exit (abort / max-cycles /
        # persistent mismatch) it does NOT, so release here to never leave the group locked by a done run.
        within_day_assignment.release(group_name, agent_id, dry_run=dry_run_lock)

    if certified is None:
        logger.info(
            "LIFECYCLE group=%s ended WITHOUT certifying (no streak / max-cycles / abort)", group_name
        )
        result.queued_jobs = _consult_deploy_queue(group_name, dry_run=dry_run_cert)
        return result

    result.certified = certified
    result.reset_features = _fire_version_reset(group_name, dry_run=dry_run_cert)
    result.queued_jobs = _consult_deploy_queue(group_name, dry_run=dry_run_cert)
    logger.info(
        "LIFECYCLE group=%s CERTIFIED (cert_day=%s value_rate=%.5f); version-reset %d feature(s); "
        "%d fix(es) queued for the applier",
        group_name,
        certified.cert_day,
        certified.value_rate or 0.0,
        len(result.reset_features),
        result.queued_jobs,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-root", default="/store")
    parser.add_argument(
        "--group", required=True, help="the single feature group to own through the lifecycle"
    )
    parser.add_argument("--agent-id", required=True, help="the owning subagent's id (assignment lock)")
    parser.add_argument("--mode", choices=["live", "replay"], default="live")
    parser.add_argument("--day", default=None, help="YYYY-MM-DD (replay day, or live cert day)")
    parser.add_argument("--poll-seconds", type=int, default=within_day_monitor.DEFAULT_POLL_SECONDS)
    parser.add_argument("--stable-cycles", type=int, default=within_day_monitor.DEFAULT_STABLE_CYCLES)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument(
        "--materialize-backfill",
        action="store_true",
        help="LIVE-INTRADAY: materialize the settled window from raw before each compare (else swept-day only)",
    )
    parser.add_argument("--write-cert", action="store_true", help="LIVE: write cert rows (default dry-run)")
    parser.add_argument("--write-lock", action="store_true", help="LIVE: take the assignment lock in DB")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    day = dt.date.fromisoformat(args.day) if args.day else None
    result = run_group_lifecycle(
        args.feature_root,
        args.group,
        args.agent_id,
        mode=args.mode,
        day=day,
        poll_seconds=args.poll_seconds,
        stable_cycles_required=args.stable_cycles,
        window_minutes=args.window_minutes,
        sample_size=args.sample_size,
        materialize_backfill=args.materialize_backfill,
        dry_run_cert=not args.write_cert,
        dry_run_lock=not args.write_lock,
        max_cycles=args.max_cycles,
    )
    logger.info(
        "=== LIFECYCLE RESULT group=%s claimed=%s certified=%s diverged=%d reset=%d queued=%d ===",
        result.group_name,
        result.claimed,
        result.did_certify,
        len(result.diverged_features),
        len(result.reset_features),
        result.queued_jobs,
    )


if __name__ == "__main__":
    main()
