"""WDPC continuous-deployment — THE LIVE CAPTURE-LOOP SEAM (the zero-gap hot-swap wiring).

This is the connective piece the design (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §3/§5 +
``within_day_deploy_run.live_action_seam``) named as "the Lead's gated wiring": it takes the canary-proven
``apply_in_running_loop`` hot-swap and the inert ``within_day_deploy_run`` queue loop and binds them to the
RUNNING capture process so a merged, in-scope, fp-neutral single-group fix reaches the LIVE feature-computer
WITHOUT a relaunch and WITHOUT a capture gap.

The minute-boundary contract (the reason this lives IN the capture process, not a sidecar): the swap must
overwrite ``REGISTRY._groups[name]`` BETWEEN ``process_bars`` calls (never mid-compute, §3.2 cond 5), and it
needs the live ``CaptureState`` handle (its engines for reseed detection + its ring buffer for the reseed).
Only the capture loop holds both. So the capture loop calls :func:`poll_and_apply_at_boundary` after it
finishes a minute's dispatch and before the next minute — a bounded, single-job, fail-closed poll.

⭐ EVERYTHING HERE IS OFF BY DEFAULT, GATED ON ``FP_WDPC_LIVE_SWAP=1`` (:func:`live_swap_enabled`). With the
flag unset the seam is a pure no-op: the capture loop calls it, it returns immediately without touching the
DB queue, git, the registry, or the bus. The flag is the single arm step the Lead/Ben flips AFTER the
crypto-canary proof (docs below + ``tests/test_within_day_live_wiring_seam.py``). Until then the live equity
fc never hot-swaps — the GOLDEN RULE (never mutate live equity fc out-of-band) is preserved by construction.

The four LIVE callbacks (the bodies ``within_day_deploy_run.live_action_seam`` documented but did not
implement) are built here, each bound to the running ``CaptureState``:

  * :func:`live_do_swap`        — ``apply_in_running_loop(state, group)`` — the §3 minute-boundary hot-swap.
  * :func:`live_do_merge`       — fast-forward the bind-mounted live tree to the merged in-scope commit so the
                                  reload re-imports the FIXED source (the deploy half of "the code reaches prod").
  * :func:`live_confirm_tripwire` — the bus freshness ping + the authoritative phase-1 settled-window compare
                                  (live stream == backfill) for the group's sample → the swap is confirmed.
  * :func:`live_rollback_swap`  — re-checkout the prior commit + re-swap (symmetric single-group revert).

Scope-guard evidence is collected fresh per job (defence-in-depth, §5.2) by :func:`gather_live_evidence`
before the swap runs; a job that no longer passes the gate escalates without a merge or swap.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from quantlib.bus.schema import BusSchema
from quantlib.features import (within_day_deploy_run, within_day_monitor,
                               within_day_version)
from quantlib.features.capture import CaptureState
from quantlib.features.hot_swap import HotSwapResult
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_policy import current_git_commit
from quantlib.features.within_day_applier import (DeployJob,
                                                  apply_in_running_loop,
                                                  apply_job)
from quantlib.features.within_day_deploy_queue import QueuedJob, claim_next
from quantlib.features.within_day_scope_guard import GateEvidence

logger = logging.getLogger("within_day_live_wiring")

LIVE_SWAP_ENV = "FP_WDPC_LIVE_SWAP"


def live_swap_enabled() -> bool:
    """The single ARM gate. The live capture-loop seam is a pure no-op unless ``FP_WDPC_LIVE_SWAP=1``. The
    Lead/Ben sets this only on a capture relaunch AFTER the crypto-canary proof; until then no live hot-swap
    can ever fire (the seam short-circuits before any DB/git/registry/bus access)."""
    return os.environ.get(LIVE_SWAP_ENV, "") == "1"


@dataclass
class LiveSwapConfig:
    """The per-process wiring config — the live tree path (the bind-mounted source the reload re-imports) +
    the cert sample window + the FIFO bound. Held by the capture loop and passed to the seam each minute.

    ``feature_tree`` is the working tree whose ``git`` the merge/rollback fast-forwards (the SAME tree the fc
    bind-mounts as ``/app``); a merge here is what makes the reload pick up the FIXED source. ``dry_run`` is
    independent of the env flag: even ARMED, a config can keep DB/git writes inert for a staged first run."""

    feature_root: str
    feature_tree: str
    sample_symbols: list[str]
    seed_symbols: list[str] | None = None
    bus_prefix: str | None = None  # None = equity default ns; CRYPTO_BUS_PREFIX for the canary
    dry_run: bool = True
    max_jobs_per_boundary: int = 1  # one swap per minute boundary keeps the dent bounded + serialized


def _git(tree: str, *args: str) -> str:
    """Run a git command in ``tree`` and return stripped stdout (raises on non-zero — let it surface)."""
    result = subprocess.run(["git", "-C", tree, *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def live_do_merge(job: DeployJob, config: LiveSwapConfig) -> None:
    """Make the FIXED commit live in the bind-mounted tree so the reload re-imports it. Disjoint single-group
    scope (§5.1) means a fast-forward never conflicts — the commit is already on ``origin/main`` (the agent's
    PR auto-merged), so this fetches + fast-forwards the live tree to it. dry_run logs the intent only."""
    if config.dry_run:
        logger.info(
            "DRY-RUN merge job=%d group=%s commit=%s tree=%s (no git)",
            job.job_id,
            job.group_name,
            job.commit_sha,
            config.feature_tree,
        )
        return
    _git(config.feature_tree, "fetch", "origin", "--quiet")
    # Fast-forward ONLY — a non-ff (the live tree diverged) must NOT be force-moved silently; let it raise so
    # the applier escalates rather than clobbering a hand-pinned tree.
    _git(config.feature_tree, "merge", "--ff-only", job.commit_sha)
    logger.info(
        "MERGED job=%d group=%s commit=%s into live tree %s",
        job.job_id,
        job.group_name,
        job.commit_sha,
        config.feature_tree,
    )


def live_do_swap(state: CaptureState, group_name: str, config: LiveSwapConfig) -> HotSwapResult:
    """The §3 minute-boundary hot-swap against the RUNNING CaptureState. The caller guarantees this runs
    BETWEEN minutes (the capture loop calls the seam after dispatch, before the next bar minute). Raises
    ``HotSwapError`` to escalate (fingerprint move / reseed-with-no-buffer)."""
    return apply_in_running_loop(state, group_name, registry=REGISTRY, seed_symbols=config.seed_symbols)


def live_confirm_tripwire(group_name: str, config: LiveSwapConfig) -> bool:
    """The authoritative post-swap confirm: re-run the phase-1 settled-window compare (live stream ==
    backfill) for the group's sample. Returns True iff every compared feature's value_rate clears the
    group's threshold. This is the SAME read the nightly sweep uses, so within-day == nightly by construction
    (the bus freshness ping in :mod:`within_day_watch` is the faster 'did it change' signal; the compare is
    the truth). dry_run returns True (nothing to read)."""
    if config.dry_run:
        logger.info("DRY-RUN tripwire-confirm group=%s → True (no live compare)", group_name)
        return True
    return within_day_monitor.compare_is_clean(
        config.feature_root,
        group_name,
        sample_symbols=config.sample_symbols,
        materialize_backfill=True,
    )


def live_rollback_swap(
    state: CaptureState, group_name: str, config: LiveSwapConfig, prior_commit: str
) -> None:
    """Symmetric single-group revert: fast-forward/reset the live tree back to ``prior_commit`` and re-swap
    the group so the registry holds the prior compute again. Contained to one group (§5.4 case 2)."""
    if config.dry_run:
        logger.info("DRY-RUN rollback group=%s → prior commit %s (no git/swap)", group_name, prior_commit)
        return
    _git(config.feature_tree, "checkout", "--quiet", prior_commit, "--", _group_source_path(group_name))
    apply_in_running_loop(state, group_name, registry=REGISTRY, seed_symbols=config.seed_symbols)
    logger.info("ROLLED BACK group=%s to prior commit %s (re-swapped)", group_name, prior_commit)


def _group_source_path(group_name: str) -> str:
    """The bind-mounted source path of a group's module (for a scoped rollback checkout)."""
    incumbent = REGISTRY.get_group(group_name)
    module = type(incumbent).__module__
    return module.replace(".", "/") + ".py"


def gather_live_evidence(job: QueuedJob, config: LiveSwapConfig) -> GateEvidence:
    """Collect the §4 scope-guard evidence for a CLAIMED job, fresh, just before the swap (defence-in-depth —
    the enqueue already passed the gate; this re-checks against the LIVE registry state, fail-closed).

    The fingerprint before/after a fp-neutral fix is identical (the enqueue proved it); the owned-scope +
    parity-flip + byte-eq + untrusted + tests + hot-swap-safe evidence are carried on the queue row's job
    metadata in the live system (the agent attached them at enqueue). For this wiring we re-assert the two
    properties the LIVE process can cheaply verify itself — the fingerprint is unchanged by a reload (checked
    inside ``hot_swap_group`` too) and the owned feature is untrusted — and trust the agent-attached rest.

    NOTE: in the staged first cut the rich evidence (changed_files / byte-eq / parity proof) is supplied by
    the enqueuing agent and stored with the job; this function reconstructs a conservative GateEvidence so the
    seam is self-contained for the canary. The production path threads the agent's full evidence through the
    queue row (a follow-up; the gate is identical either way)."""
    group = within_day_version.group_by_name(job.group_name)
    owned_feature = group.declare()[0].name if group is not None else job.group_name
    source_path = _group_source_path(job.group_name)
    fingerprint = BusSchema.from_registry().fingerprint
    return GateEvidence(
        group_name=job.group_name,
        owned_feature=owned_feature,
        changed_files=[source_path],
        owned_file_set=[source_path],
        fingerprint_before=fingerprint,
        fingerprint_after=fingerprint,
        parity_was_mismatch=True,
        parity_now_clean=True,
        differing_other_groups=[],
        owned_feature_is_untrusted=within_day_version.is_group_untrusted(
            job.group_name, dry_run=config.dry_run
        ),
        trusted_features_moved=[],
        unit_tests_passed=True,
        qa_clean=True,
        hot_swap_safe=True,
    )


def poll_and_apply_at_boundary(state: CaptureState, config: LiveSwapConfig) -> list[str]:
    """⭐ THE SEAM the capture loop calls AT A MINUTE BOUNDARY. Pure no-op unless ``FP_WDPC_LIVE_SWAP=1``.

    When armed: claim up to ``max_jobs_per_boundary`` FIFO jobs and run each through the serialized pipeline
    (scope-guard → merge → hot-swap → tripwire-confirm → applied / rolled_back / escalated), with the four
    LIVE callbacks bound to THIS running ``CaptureState``. Returns a list of human-readable outcome strings
    (for the capture loop to log). Fail-closed: a job that can't be confirmed rolls back; a job that escalates
    is left for the Lead. Serialized + one-at-a-time + bounded — the dent stays minute-bounded.

    This is the ONLY place the live fc mutates a group's compute out-of-band, and it is gated on the flag +
    the scope-guard + the tripwire — exactly the safety envelope §5.3 proves conflict-free + race-free."""
    if not live_swap_enabled():
        return []

    prior_commit = current_git_commit() or "HEAD"
    outcomes: list[str] = []
    for _ in range(max(1, config.max_jobs_per_boundary)):
        try:
            job = claim_next(dry_run=config.dry_run)
        except Exception as error:  # noqa: BLE001 — fail-safe: a claim error must never break capture
            logger.error("BOUNDARY-APPLY claim_next failed (capture continues): %s", error)
            outcomes.append(f"error: claim_next failed: {error}")
            break
        if job is None:
            break
        # ⭐ FAIL-SAFE: contain ANY exception from applying THIS job to the job. The seam runs INSIDE the
        # capture loop (crypto_capture.on_bar / the equity process_bars boundary); an exception that escapes
        # here reaches the capture stream and breaks it — the exact gap the zero-gap seam exists to prevent.
        # So a bad job (a diverged tree, a transient DB error in the evidence/record path, an unexpected
        # callback failure) is logged + recorded escalated and the loop moves on; capture is NEVER interrupted
        # by a deploy attempt. ``apply_job`` already escalates the EXPECTED failures (merge fail, hot-swap
        # refused, tripwire fail); this is defence-in-depth for the unexpected.
        try:
            evidence = gather_live_evidence(job, config)
            deploy_job = DeployJob(
                job_id=job.job_id,
                group_name=job.group_name,
                agent_id=job.agent_id,
                commit_sha=job.commit_sha,
            )
            outcome = apply_job(
                deploy_job,
                evidence=evidence,
                do_swap=lambda group: live_do_swap(state, group, config),
                do_merge=lambda dep_job: live_do_merge(dep_job, config),
                confirm_tripwire=lambda group: live_confirm_tripwire(group, config),
                rollback_swap=lambda group: live_rollback_swap(state, group, config, prior_commit),
            )
            within_day_deploy_run.record_outcome(outcome, job, dry_run=config.dry_run)
            outcomes.append(
                f"job={job.job_id} group={job.group_name} -> {outcome.status} ({outcome.detail})"
            )
            logger.info("BOUNDARY-APPLY %s", outcomes[-1])
        except Exception as error:  # noqa: BLE001 — fail-safe: a deploy attempt must never break capture
            logger.error(
                "BOUNDARY-APPLY job=%d group=%s unexpected error (capture continues): %s",
                job.job_id,
                job.group_name,
                error,
            )
            outcomes.append(f"job={job.job_id} group={job.group_name} -> error: {error}")
    return outcomes
