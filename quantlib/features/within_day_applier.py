"""WDPC continuous-deployment — the FIFO deploy QUEUE + the ONE SERIALIZED APPLIER
(docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §5.2) + the LIVE-WIRED per-group hot-swap inside a running
capture loop (§3, the live-wiring of the #323 primitive).

The applier dequeues FIFO and runs ONE job at a time:
  dequeue -> scope-guard -> auto-merge -> hot_swap (in the running engine) -> tripwire-confirm -> next;
  on a tripwire failure, ROLL BACK that one group's swap (re-import the prior commit) + escalate.
Serialization is the whole safety story: one swap at a time, each confirmed before the next, so a bad swap
is contained to one group and reverted before anything else deploys.

The prod-specific ACTIONS (auto-merge to main, the production fc engine) are INJECTED as callbacks so this
module is fully testable offline + the LIVE actions are the Lead's gated wiring — this module NEVER touches
the live fc/git itself. ``apply_in_running_loop`` is the live-wired hot-swap: it invokes ``hot_swap_group``
against a RUNNING CaptureState's engines + buffer at a minute boundary (proven on a throwaway sandbox capture
in the canary test; NEVER the real fc here).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import polars as pl

from quantlib.features.capture import CaptureState
from quantlib.features.hot_swap import (HotSwapError, HotSwapResult,
                                        hot_swap_group)
from quantlib.features.registry import REGISTRY, Registry
from quantlib.features.within_day_scope_guard import (GateEvidence, GateResult,
                                                      evaluate)

logger = logging.getLogger("within_day_applier")


@dataclass
class DeployJob:
    job_id: int
    group_name: str
    agent_id: str
    commit_sha: str


@dataclass
class ApplyOutcome:
    job_id: int
    group_name: str
    status: str  # 'applied' | 'rolled_back' | 'escalated'
    detail: str


def apply_in_running_loop(
    state: CaptureState,
    group_name: str,
    *,
    registry: Registry = REGISTRY,
    seed_symbols: list[str] | None = None,
) -> HotSwapResult:
    """LIVE-WIRED hot-swap: invoke ``hot_swap_group`` against a RUNNING CaptureState — its incremental engines
    (for reseed detection) + its current ring buffer (for the reseed). The caller invokes this at a MINUTE
    BOUNDARY (between ``process_bars`` calls), never mid-compute. Raises ``HotSwapError`` to escalate (a
    RESEED kind with no buffer, a fingerprint change, etc.). Proven on a throwaway sandbox capture (canary
    test); the LIVE fc invocation is the Lead's gated step."""
    buffer_frame: pl.DataFrame | None = state.buffer
    return hot_swap_group(
        group_name,
        registry=registry,
        engines=state.engines,
        buffer_frame=buffer_frame,
        seed_symbols=seed_symbols,
    )


def apply_job(
    job: DeployJob,
    *,
    evidence: GateEvidence,
    do_swap: Callable[[str], HotSwapResult],
    do_merge: Callable[[DeployJob], None],
    confirm_tripwire: Callable[[str], bool],
    rollback_swap: Callable[[str], None],
) -> ApplyOutcome:
    """Apply ONE dequeued job through the serialized pipeline. ACTIONS are injected (do_merge = the auto-merge
    to main; do_swap = the live-wired hot-swap; confirm_tripwire = the bus freshness confirm; rollback_swap =
    re-import the prior commit) so this is pure orchestration — testable offline, with the LIVE actions wired
    by the Lead, never here.

    Pipeline: scope-guard -> auto-merge -> hot_swap -> tripwire-confirm -> applied; on a guard fail ESCALATE
    (no merge/swap); on a tripwire fail ROLL BACK the swap + escalate."""
    gate: GateResult = evaluate(evidence)
    if not gate.approved:
        logger.info(
            "job %d group=%s ESCALATE (scope-guard): %s", job.job_id, job.group_name, gate.violations
        )
        return ApplyOutcome(job.job_id, job.group_name, "escalated", "; ".join(gate.violations))

    do_merge(job)  # auto-merge the in-scope commit (disjoint scope -> never conflicts)

    try:
        result = do_swap(job.group_name)
    except HotSwapError as error:
        logger.warning("job %d group=%s ESCALATE (hot-swap refused): %s", job.job_id, job.group_name, error)
        return ApplyOutcome(job.job_id, job.group_name, "escalated", f"hot-swap refused: {error}")

    if confirm_tripwire(job.group_name):
        logger.info("job %d group=%s APPLIED (%s)", job.job_id, job.group_name, result.kind.value)
        return ApplyOutcome(job.job_id, job.group_name, "applied", result.note)

    # Tripwire failed: production live still != backfill. Roll back THIS group's swap, escalate.
    rollback_swap(job.group_name)
    logger.warning("job %d group=%s ROLLED BACK (tripwire failed post-swap)", job.job_id, job.group_name)
    return ApplyOutcome(job.job_id, job.group_name, "rolled_back", "tripwire failed post-swap; reverted")


def run_applier(
    dequeue: Callable[[], DeployJob | None],
    apply_one: Callable[[DeployJob], ApplyOutcome],
    *,
    max_jobs: int | None = None,
) -> list[ApplyOutcome]:
    """The ONE serialized applier loop: dequeue FIFO, apply one at a time, until the queue is empty (or
    ``max_jobs`` applied). Serialized by construction — never two swaps in flight. Returns the outcomes."""
    outcomes: list[ApplyOutcome] = []
    while max_jobs is None or len(outcomes) < max_jobs:
        job = dequeue()
        if job is None:
            break
        outcomes.append(apply_one(job))
    return outcomes
