"""THE FEATURE-WORKER ORCHESTRATOR — one runnable that IS a single feature-worker in the fleet
(docs/FEATURE_WORKER_FLEET.md §4). The Lead/cron spawns N>=5 copies; each one loops:

    pick the highest-priority unclaimed group  (feature_queue.next_group, DIVERGENT first)
      -> ADVANCE it ONE lifecycle phase:
           * a CLEAN group (UNVERIFIED / CERTIFIED_PENDING_TRUST / MONITORING_STALE):
               run within_day_run.run_group_lifecycle — claim the assignment lock, monitor the settled
               window live==backfill to a stable streak, certify (within_day_parity_cert + trust grant),
               fire the version reset, peek the deploy queue, release the lock.
           * a DIVERGENT group (an OPEN feature_parity_defect):
               do NOT auto-edit code (the WDPC never auto-pushes). Read the defect exemplars and run the
               within_day_rootcause classifier to produce a TRIAGE report: which code path (incremental.py /
               stateful.py / raw_loaders.py / materialize.py) likely diverged, so a fixing agent picks it up
               and routes a worktree->PR through the Lead. The fix's deploy is the FIFO within_day_deploy_queue
               + the Lead-gated hot-swap (within_day_live_wiring / FP_WDPC_LIVE_SWAP) — never this worker.
      -> the lifecycle releases the lock; loop to the next group.

CONFLICT-FREEDOM: the queue excludes any group a live (active, non-stale) assignment lock already holds, and
the claim is the assignment lock's PK INSERT — two workers can never own one group. A worker that loses the
claim race (the row was taken between the queue read and the claim) simply skips and picks the next group.

⭐ SAFETY: ``dry_run`` is the DEFAULT on every state mutation (the assignment lock, the cert/trust grant, the
version reset). A dry-run worker reads the queue offline (empty), or with ``--write-lock``/``--write-cert``
runs the real lifecycle. This worker NEVER edits the live tree, restarts fc, applies a hot-swap, or enqueues
a deploy — those are Lead-gated. ``--once`` advances one group and exits (the cron-respawn unit); the default
loops until the queue is empty or ``--max-iterations`` is hit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field

import psycopg

from quantlib.features import feature_queue, within_day_monitor, within_day_rootcause, within_day_run
from quantlib.features.feature_queue import QueueItem, QueuePriority
from quantlib.features.validation_db import DB_KWARGS
from quantlib.features.within_day_parity import DEFAULT_SAMPLE_SIZE, DEFAULT_WINDOW_MINUTES
from quantlib.features.within_day_rootcause import RootCause

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("feature_worker")

# Between loop iterations when the queue is EMPTY (every group trusted or live-locked) — sleep before
# re-polling so an idle worker does not spin. The cron keeps >=5 alive; this just paces a quiet pool.
IDLE_SLEEP_SECONDS = 30


def make_agent_id() -> str:
    """A stable-per-process worker id for the assignment lock: host + short uuid, so the dashboard's active-
    owners panel shows which box + which worker holds each group."""
    return f"fworker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


@dataclass
class AdvanceResult:
    """The outcome of advancing ONE group one phase — what a worker iteration produced (for the loop log and
    the Lead's observability). Exactly one of ``lifecycle`` / ``triage`` is populated per the group's phase.
    """

    group_name: str
    phase: str
    claimed: bool
    advanced: bool
    detail: str
    triage: list[RootCause] = field(default_factory=list)  # populated for a DIVERGENT group


def _read_open_defects(group_name: str) -> list[dict[str, object]]:
    """The OPEN feature_parity_defect rows for ``group_name`` (the DIVERGENT evidence the triage reasons over:
    per-feature exemplar diverging cells + worst error). READ-ONLY."""
    rows: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT feature, worst_rel_err, exemplars
            FROM feature_parity_defect
            WHERE feature_group = %(group)s AND status IN ('open', 'investigating')
            ORDER BY worst_rel_err DESC NULLS LAST
            """,
            {"group": group_name},
        )
        for feature, worst_rel_err, exemplars in cur.fetchall():
            rows.append(
                {
                    "feature": str(feature),
                    "worst_rel_err": float(worst_rel_err) if worst_rel_err is not None else None,
                    "exemplars": exemplars if isinstance(exemplars, list) else [],
                }
            )
    return rows


def _is_tick_feature(feature: str) -> bool:
    """A quote/tick feature (whose live==backfill match is the FP_TICK_SYMBOLS coverage case the root-cause
    classifier screens as an artifact, not a code bug)."""
    lowered = feature.lower()
    return any(token in lowered for token in ("quote", "tick", "spread", "trade_"))


def triage_divergent_group(group_name: str, *, dry_run: bool = True) -> list[RootCause]:
    """Classify a DIVERGENT group's OPEN defects into root causes (which code path to fix) WITHOUT touching
    code — the actionable hand-off for a fixing agent. ``dry_run`` reads no DB and returns []."""
    if dry_run:
        logger.info("DRY-RUN triage group=%s (no DB read)", group_name)
        return []
    defects = _read_open_defects(group_name)
    causes: list[RootCause] = []
    for defect in defects:
        feature = str(defect["feature"])
        exemplars = list(defect["exemplars"]) if isinstance(defect["exemplars"], list) else []
        cause = within_day_rootcause.classify_feature(
            feature=feature,
            n_mismatch=len(exemplars) or 1,  # a present OPEN defect implies >=1 mismatch
            n_extra_live=0,
            n_missing_live=0,
            value_rate=None,
            is_tick_feature=_is_tick_feature(feature),
            on_tick_symbol=True,  # the defect was recorded on a clean comparable cell
            exemplars=exemplars,
        )
        causes.append(cause)
    return causes


def advance_group(
    item: QueueItem,
    agent_id: str,
    *,
    feature_root: str = "/store",
    mode: str = "live",
    day: dt.date | None = None,
    poll_seconds: int = within_day_monitor.DEFAULT_POLL_SECONDS,
    stable_cycles_required: int = within_day_monitor.DEFAULT_STABLE_CYCLES,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    materialize_backfill: bool = False,
    max_cycles: int | None = None,
    dry_run_cert: bool = True,
    dry_run_lock: bool = True,
) -> AdvanceResult:
    """Advance ONE group one lifecycle phase, branching on its queue phase.

    DIVERGENT -> a read-only root-cause triage (the fix is a human/agent worktree->PR, never auto-applied).
    Everything else -> within_day_run.run_group_lifecycle (claim -> monitor-to-certify -> reset -> release).
    """
    if item.priority == QueuePriority.DIVERGENT:
        causes = triage_divergent_group(item.group_name, dry_run=dry_run_cert)
        actionable = [cause for cause in causes if cause.is_actionable()]
        detail = (
            f"DIVERGENT: {item.n_open_defects} open defect(s); "
            f"{len(actionable)}/{len(causes)} actionable root-cause(s) — "
            "hand off to a fixing agent (worktree->PR->Lead). No code auto-edited."
        )
        logger.info(
            "ADVANCE group=%s phase=DIVERGENT: %d open defect(s), %d actionable root-cause(s) triaged",
            item.group_name,
            item.n_open_defects,
            len(actionable),
        )
        # The worker does not claim the lock for a DIVERGENT group: there is no monitor run to protect, and
        # the eventual fix takes its own lock when it re-monitors under the new code. Triage is read-only.
        return AdvanceResult(
            group_name=item.group_name,
            phase=item.phase,
            claimed=False,
            advanced=False,  # triaged, not certified — the fix is the next, human/agent step
            detail=detail,
            triage=causes,
        )

    # CLEAN phases (UNVERIFIED / MONITORING_STALE / CERTIFIED_PENDING_TRUST): run the full lifecycle. It
    # claims the lock itself (a MONITORING_STALE group's dead lock is reclaimed by the claim's timeout
    # branch), monitors to certify, and releases — exactly the "advance one phase" the worker wants.
    result = within_day_run.run_group_lifecycle(
        feature_root,
        item.group_name,
        agent_id,
        mode=mode,
        day=day,
        poll_seconds=poll_seconds,
        stable_cycles_required=stable_cycles_required,
        window_minutes=window_minutes,
        sample_size=sample_size,
        materialize_backfill=materialize_backfill,
        dry_run_cert=dry_run_cert,
        dry_run_lock=dry_run_lock,
        max_cycles=max_cycles,
    )
    if not result.claimed:
        # Lost the claim race (taken between the queue read and the claim) — skip; the loop picks the next.
        return AdvanceResult(
            group_name=item.group_name,
            phase=item.phase,
            claimed=False,
            advanced=False,
            detail="claim lost to another worker — skipping",
        )
    if result.did_certify:
        detail = (
            f"CERTIFIED (cert_day={result.certified.cert_day if result.certified else '?'}); "
            f"version-reset {len(result.reset_features)} feature(s); {result.queued_jobs} fix(es) queued"
        )
    else:
        detail = "monitored without a certify (no streak / max-cycles) — re-queued for the next pass"
    return AdvanceResult(
        group_name=item.group_name,
        phase=item.phase,
        claimed=True,
        advanced=result.did_certify,
        detail=detail,
    )


def run_worker(
    agent_id: str,
    *,
    feature_root: str = "/store",
    mode: str = "live",
    day: dt.date | None = None,
    once: bool = False,
    max_iterations: int | None = None,
    idle_sleep_seconds: int = IDLE_SLEEP_SECONDS,
    poll_seconds: int = within_day_monitor.DEFAULT_POLL_SECONDS,
    stable_cycles_required: int = within_day_monitor.DEFAULT_STABLE_CYCLES,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    materialize_backfill: bool = False,
    max_cycles: int | None = None,
    dry_run_cert: bool = True,
    dry_run_lock: bool = True,
) -> list[AdvanceResult]:
    """Run ONE feature-worker: pick the highest-priority claimable group, advance it one phase, repeat. With
    ``once`` it advances a single group and returns (the cron-respawn unit). Otherwise it loops until the
    queue is empty (idle-sleep then re-poll) or ``max_iterations`` is reached. Returns every iteration's
    AdvanceResult for the caller's report."""
    results: list[AdvanceResult] = []
    iteration = 0
    while True:
        if max_iterations is not None and iteration >= max_iterations:
            logger.info("WORKER %s hit max_iterations=%d — stopping", agent_id, max_iterations)
            break
        item = feature_queue.next_group(dry_run=dry_run_cert)
        if item is None:
            logger.info("WORKER %s: queue empty (all groups trusted or live-locked)", agent_id)
            if once or max_iterations is not None:
                break
            time.sleep(idle_sleep_seconds)
            continue

        logger.info(
            "WORKER %s claimed-next group=%s phase=%s (priority=%d, %d/%d trusted, %d open defects)",
            agent_id,
            item.group_name,
            item.phase,
            int(item.priority),
            item.n_trusted,
            item.n_features,
            item.n_open_defects,
        )
        result = advance_group(
            item,
            agent_id,
            feature_root=feature_root,
            mode=mode,
            day=day,
            poll_seconds=poll_seconds,
            stable_cycles_required=stable_cycles_required,
            window_minutes=window_minutes,
            sample_size=sample_size,
            materialize_backfill=materialize_backfill,
            max_cycles=max_cycles,
            dry_run_cert=dry_run_cert,
            dry_run_lock=dry_run_lock,
        )
        results.append(result)
        iteration += 1
        if once:
            break
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-root", default="/store")
    parser.add_argument("--agent-id", default=None, help="worker id for the assignment lock (auto if unset)")
    parser.add_argument("--mode", choices=["live", "replay"], default="live")
    parser.add_argument("--day", default=None, help="YYYY-MM-DD (replay day, or live cert day)")
    parser.add_argument("--once", action="store_true", help="advance ONE group and exit (cron-respawn unit)")
    parser.add_argument(
        "--max-iterations", type=int, default=None, help="advance at most N groups, then stop"
    )
    parser.add_argument("--idle-sleep", type=int, default=IDLE_SLEEP_SECONDS)
    parser.add_argument("--poll-seconds", type=int, default=within_day_monitor.DEFAULT_POLL_SECONDS)
    parser.add_argument("--stable-cycles", type=int, default=within_day_monitor.DEFAULT_STABLE_CYCLES)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument(
        "--materialize-backfill",
        action="store_true",
        help="LIVE-INTRADAY: materialize the settled window before each compare (else swept-day only)",
    )
    parser.add_argument(
        "--write-cert", action="store_true", help="LIVE: write cert/trust rows (default dry)"
    )
    parser.add_argument("--write-lock", action="store_true", help="LIVE: take the assignment lock in the DB")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    agent_id = args.agent_id or make_agent_id()
    day = dt.date.fromisoformat(args.day) if args.day else None
    results = run_worker(
        agent_id,
        feature_root=args.feature_root,
        mode=args.mode,
        day=day,
        once=args.once,
        max_iterations=args.max_iterations,
        idle_sleep_seconds=args.idle_sleep,
        poll_seconds=args.poll_seconds,
        stable_cycles_required=args.stable_cycles,
        window_minutes=args.window_minutes,
        sample_size=args.sample_size,
        materialize_backfill=args.materialize_backfill,
        max_cycles=args.max_cycles,
        dry_run_cert=not args.write_cert,
        dry_run_lock=not args.write_lock,
    )
    n_advanced = sum(1 for result in results if result.advanced)
    logger.info(
        "=== WORKER %s DONE: %d group(s) handled, %d advanced a phase ===",
        agent_id,
        len(results),
        n_advanced,
    )


if __name__ == "__main__":
    main()
