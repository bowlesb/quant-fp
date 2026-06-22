"""WDPC continuous-deployment — the QUEUE-WIRED APPLIER ORCHESTRATION (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md
§5.2). This is the connective tissue that runs the FIFO loop end-to-end: it dequeues from the DB queue
(:mod:`within_day_deploy_queue`), runs ONE job through the pure pipeline (:func:`within_day_applier.apply_job`),
records the queue outcome, and — on a successful apply — fires the VERSION-RESET (:mod:`within_day_version`)
so the swapped group's trust re-earns under its NEW content hash.

It composes the already-built pieces; it adds NO new policy:
  * the FIFO + state machine + backoff live in :mod:`within_day_deploy_queue`
  * the scope-guard + serialized one-at-a-time pipeline live in :mod:`within_day_applier`
  * the "is the deployed version the one trust was earned on?" + the trust reset live in :mod:`within_day_version`

⭐ THE LIVE HOT-SWAP IS A LEAD-GATED SEAM, NOT IMPLEMENTED HERE. The four prod actions
(do_merge / do_swap / confirm_tripwire / rollback_swap) are INJECTED as callbacks; this module wires the
queue + the version reset around them but NEVER touches live git / fc / the registry itself. The default
:func:`dry_run_actions` is a fully-inert action set (logs the intent, swaps nothing, confirms True) so the
whole loop is exercisable offline. The LIVE actions — auto-merge to main + ``apply_in_running_loop`` against
the real fc CaptureState at a minute boundary (§3) — are the Lead's gated wiring, documented in
:func:`live_action_seam` below. Until the Lead wires them, ``dry_run=True`` keeps every DB write inert too.
"""

from __future__ import annotations

import logging
from typing import Callable

from quantlib.features import within_day_deploy_queue as queue
from quantlib.features import within_day_version
from quantlib.features.hot_swap import HotSwapResult
from quantlib.features.within_day_applier import (ApplyOutcome, DeployJob,
                                                  apply_job)
from quantlib.features.within_day_deploy_queue import QueuedJob
from quantlib.features.within_day_scope_guard import GateEvidence

logger = logging.getLogger("within_day_deploy_run")


def _noop_swap(group_name: str) -> HotSwapResult:
    """The inert dry-run swap: reports a no-reseed result, touches NOTHING. The LIVE swap
    (``within_day_applier.apply_in_running_loop`` against the real fc CaptureState) is the Lead-gated seam."""
    logger.info("DRY-RUN swap group=%s (no live fc hot-swap)", group_name)
    return HotSwapResult(
        group_name=group_name,
        swapped=False,
        reseeded=False,
        fingerprint_before=0,
        fingerprint_after=0,
        note="dry-run: no live swap",
    )


def _noop_merge(job: DeployJob) -> None:
    logger.info("DRY-RUN merge job=%d group=%s commit=%s (no git)", job.job_id, job.group_name, job.commit_sha)


def _confirm_true(group_name: str) -> bool:
    logger.info("DRY-RUN tripwire-confirm group=%s → True (no live bus read)", group_name)
    return True


def _noop_rollback(group_name: str) -> None:
    logger.info("DRY-RUN rollback group=%s (no live fc revert)", group_name)


def dry_run_actions() -> dict[str, Callable]:
    """The fully-inert action set: merge/swap/confirm/rollback that touch no live state. Pass these (the
    default) to exercise the whole queue loop offline; the Lead replaces them with the live actions (the
    :func:`live_action_seam` callbacks) to activate real-time deploy."""
    return {
        "do_merge": _noop_merge,
        "do_swap": _noop_swap,
        "confirm_tripwire": _confirm_true,
        "rollback_swap": _noop_rollback,
    }


def process_one(
    job: QueuedJob,
    *,
    evidence: GateEvidence,
    actions: dict[str, Callable],
    dry_run: bool = True,
) -> ApplyOutcome:
    """Run ONE claimed queue job through the pure pipeline, then record the queue outcome + (on apply) reset
    the group's trust to re-earn under the new content hash.

    The mapping from the pipeline ApplyOutcome.status to the queue terminal state:
      * 'applied'     → mark_applied  + reset_trust_on_content_change (the swapped code changed → re-earn)
      * 'rolled_back' → mark_rolled_back (the swap was reverted; trust untouched — the old code is back)
      * 'escalated'   → mark_escalated  (scope-guard refused / hot-swap refused)
    Serialization + FIFO are the caller's (``run_queue``) concern; this handles the single job."""
    deploy_job = DeployJob(
        job_id=job.job_id, group_name=job.group_name, agent_id=job.agent_id, commit_sha=job.commit_sha
    )
    outcome = apply_job(
        deploy_job,
        evidence=evidence,
        do_swap=actions["do_swap"],
        do_merge=actions["do_merge"],
        confirm_tripwire=actions["confirm_tripwire"],
        rollback_swap=actions["rollback_swap"],
    )

    if outcome.status == "applied":
        queue.mark_applied(job.job_id, outcome.detail, dry_run=dry_run)
        _reset_trust_after_swap(job.group_name, dry_run=dry_run)
    elif outcome.status == "rolled_back":
        queue.mark_rolled_back(job.job_id, outcome.detail, dry_run=dry_run)
    elif outcome.status == "escalated":
        queue.mark_escalated(job.job_id, outcome.detail, dry_run=dry_run)
    else:
        raise ValueError(f"unexpected ApplyOutcome.status {outcome.status!r}")
    return outcome


def _reset_trust_after_swap(group_name: str, *, dry_run: bool) -> None:
    """After a successful hot-swap the group's compute SOURCE changed → its content hash changed → any prior
    trust grant was earned on different code. Reset it so the within-day monitor re-certifies the new version
    (the version-awareness reset, :mod:`within_day_version`). A no-op if the group isn't registered or had no
    trusted feature."""
    group = within_day_version.group_by_name(group_name)
    if group is None:
        logger.warning("post-apply trust reset skipped: group %s not registered", group_name)
        return
    reset = within_day_version.reset_trust_on_content_change(group, dry_run=dry_run)
    logger.info("post-apply version reset group=%s features=%s", group_name, reset)


def run_queue(
    evidence_for: Callable[[QueuedJob], GateEvidence],
    *,
    actions: dict[str, Callable] | None = None,
    max_jobs: int | None = None,
    dry_run: bool = True,
) -> list[ApplyOutcome]:
    """The ONE serialized applier loop, wired to the DB queue: claim FIFO → process one → repeat until the
    queue is empty (or ``max_jobs`` processed). ``evidence_for`` supplies the already-gathered scope-guard
    evidence for a claimed job (the caller collects it — re-running the in-sandbox reconfirm + the byte-eq
    check, §4 — defence-in-depth before the swap). ``actions`` defaults to the inert :func:`dry_run_actions`.

    Serialized by construction: one claim, one process, before the next — never two swaps in flight (§5.2).
    With ``dry_run=True`` (default) ``claim_next`` returns None (no DB), so the loop is a no-op; tests inject
    a fake queue. The Lead flips ``dry_run=False`` AND injects the live actions to activate real-time deploy."""
    resolved_actions = actions or dry_run_actions()
    outcomes: list[ApplyOutcome] = []
    while max_jobs is None or len(outcomes) < max_jobs:
        job = queue.claim_next(dry_run=dry_run)
        if job is None:
            break
        outcomes.append(
            process_one(job, evidence=evidence_for(job), actions=resolved_actions, dry_run=dry_run)
        )
    return outcomes


def live_action_seam() -> str:
    """⭐ THE LEAD-GATED LIVE HOT-SWAP SEAM (documented, NOT implemented here).

    To activate real-time deploy the Lead injects FOUR live callbacks into :func:`run_queue` (replacing
    :func:`dry_run_actions`) and flips ``dry_run=False``:

      * ``do_merge(job)``        — auto-merge the in-scope commit to main (disjoint scope → never conflicts).
      * ``do_swap(group_name)``  — call ``within_day_applier.apply_in_running_loop(state, group_name)`` against
                                   the RUNNING fc CaptureState, AT A MINUTE BOUNDARY (between process_bars
                                   calls, never mid-compute). This is the §3 hot-swap; it touches live fc, so
                                   it is the Lead/Ben-gated step. It already exists + is canary-proven on a
                                   throwaway sandbox capture; it is NOT invoked from this module.
      * ``confirm_tripwire(group_name)`` — read the live bus the next minute the group emits + re-run the
                                   phase-1 compare → live == backfill?
      * ``rollback_swap(group_name)`` — re-import the prior commit (symmetric single-group hot-swap) to revert.

    The version-reset (:func:`_reset_trust_after_swap`) fires automatically on a successful apply — the
    deployed content hash changed, so trust re-earns. The Lead need only supply the four live actions; the
    queue + reset wiring here is unchanged."""
    return (
        "Inject live do_merge/do_swap(apply_in_running_loop)/confirm_tripwire/rollback_swap into run_queue "
        "and set dry_run=False to activate the §3 minute-boundary hot-swap. Lead/Ben-gated; not wired here."
    )
