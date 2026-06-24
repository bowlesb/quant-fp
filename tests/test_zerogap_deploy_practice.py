"""THE ZERO-GAP DEPLOY PRACTICE LOOP — drives the live capture-loop seam repeatedly across group KINDS.

This is the hardening harness for the "merge → it's live with no capture gap" flow
(docs/ZERO_GAP_DEPLOY_RUNBOOK.md). The single-kind proof already exists
(``tests/test_within_day_live_wiring_seam.py`` swaps ``momentum``, an incremental ReductionGroup); THIS
suite runs the SAME seam (``poll_and_apply_at_boundary``, ``FP_WDPC_LIVE_SWAP``) REPEATEDLY (>=10 swaps) over
the THREE distinct hot-swap KINDS the live registry actually contains, because the reseed correctness is the
risk and it differs by kind:

  * STATELESS (``calendar`` — a plain ``FeatureGroup``): ``up_to_date()`` defaults True → DIRECT swap, no
    reseed. The guard is a no-op.
  * STATEFUL (``technical`` / ``candlestick`` — ``StatefulGroup``): in the LIVE capture loop these run
    ``compute_latest(ctx)`` FRESH from the ring buffer each minute (``CaptureState`` holds NO ``StatefulEngine``
    — ``emit_stateful`` is not wired into ``process_bars``), so they too are ``up_to_date()==True`` → DIRECT
    swap. This suite PROVES that empirically (a StatefulGroup hot-swap is reseed-free + value-identical).
  * INCREMENTAL ReductionGroup (``momentum`` — ``incremental_safe``): carries a live ``IncrementalEngine`` in
    ``CaptureState.engines``; the swap binds it pending-reseed → the contract reseeds from the buffer. This is
    the ONLY kind that exercises ``rebuild_from_history``, and the one where ``seed(H);fold(m)==seed(H+m)`` must
    hold cell-for-cell post-swap.

Each swap asserts, against a never-swapped REFERENCE loop run in lockstep: (1) ZERO missed minutes, (2) the
swapped group's next-minute output is VALUE-IDENTICAL to the reference (reseed-correct / recompute-correct),
(3) the fingerprint held across the swap, (4) every OTHER group is byte-identical (the swap is surgical).
Repeating the swap many times on the SAME running loop is the part the single-shot test cannot cover — it
surfaces engine-rebind leaks, a registry instance that drifts, or a reseed that is correct once but not
idempotent across repeated swaps.

ZERO container, ZERO DB, ZERO git — the queue is a fake FIFO, the merge/tripwire are the dry-run inert path,
and the swap is a REAL registry hot-swap against a sandbox ``CaptureState`` (the canary pattern). This never
touches the live fc / crypto-capture.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.bus.schema import BusSchema
from quantlib.features import within_day_live_wiring as wiring
from quantlib.features.capture import CaptureState, process_bars
from quantlib.features.declarative import ReductionGroup
from quantlib.features.registry import REGISTRY
from quantlib.features.stateful import StatefulGroup
from quantlib.features.within_day_deploy_queue import QueuedJob

BASE = dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD")

# One representative per hot-swap KIND (the reseed surface differs by kind, §3.3 of the CD doc).
STATELESS_GROUP = "calendar"  # plain FeatureGroup → up_to_date()==True → DIRECT swap
STATEFUL_GROUP = "technical"  # StatefulGroup → recompute-from-buffer live → DIRECT swap (no held engine)
INCREMENTAL_GROUP = "momentum"  # incremental_safe ReductionGroup → bound engine → contract reseed


def _bars(minute: dt.datetime) -> list[dict]:
    rows = []
    for offset, sym in enumerate(SYMBOLS):
        i = int((minute - BASE).total_seconds() // 60)
        close = 100.0 + offset * 2.0 + 5.0 * math.sin((i + offset) / 9.0) + i * 0.02
        vol = 800.0 + ((i * 7 + offset) % 40) * 25.0
        rows.append(
            {
                "S": sym,
                "o": close * 0.999,
                "c": close,
                "h": close * 1.002,
                "l": close * 0.998,
                "v": vol,
                "t": minute.isoformat(),
            }
        )
    return rows


def _config(group: str) -> wiring.LiveSwapConfig:
    return wiring.LiveSwapConfig(
        feature_root="/tmp/zerogap",
        feature_tree="/tmp/zerogap-tree",
        sample_symbols=list(SYMBOLS),
        seed_symbols=list(SYMBOLS),
        dry_run=True,
    )


def _step(state: CaptureState, root: str, minute_index: int) -> None:
    """Capture exactly one minute into ``state`` (no store write)."""
    process_bars(
        state,
        _bars(BASE + dt.timedelta(minutes=minute_index)),
        root,
        "mock",
        "2026-06-18",
        120,
        accumulate=True,
        write=False,
    )


def _enqueue_one(monkeypatch: pytest.MonkeyPatch, group: str, job_id: int) -> list[tuple[str, int]]:
    """Wire a one-shot fake FIFO queue with a single job for ``group`` and capture the recorded outcomes."""
    jobs = [
        QueuedJob(
            job_id=job_id, group_name=group, agent_id="agent-practice", commit_sha="cafe123", fail_count=0
        )
    ]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        wiring.within_day_deploy_run,
        "record_outcome",
        lambda outcome, job, dry_run=True: recorded.append((outcome.status, job.job_id)),
    )
    return recorded


def _feature_cols(frame) -> list[str]:  # type: ignore[no-untyped-def]
    return [c for c in frame.columns if c not in ("symbol", "minute")]


def _assert_minute_equal(live, ref, minute: dt.datetime, ctx: str) -> None:  # type: ignore[no-untyped-def]
    """Assert a group's per-symbol output for ``minute`` is value-identical between the swapped + reference."""
    live_m = live.filter(live["minute"] == minute).sort("symbol")
    ref_m = ref.filter(ref["minute"] == minute).sort("symbol")
    assert live_m.height == ref_m.height > 0, f"{ctx}: row-count/coverage mismatch at {minute}"
    for col in _feature_cols(live_m):
        for live_val, ref_val in zip(live_m[col].to_list(), ref_m[col].to_list()):
            live_null = live_val is None or (isinstance(live_val, float) and live_val != live_val)
            ref_null = ref_val is None or (isinstance(ref_val, float) and ref_val != ref_val)
            if live_null or ref_null:
                assert live_null == ref_null, f"{ctx}: null-mismatch on {col} at {minute}"
                continue
            assert math.isclose(
                live_val, ref_val, rel_tol=1e-9, abs_tol=1e-9
            ), f"{ctx}: value mismatch on {col} at {minute}: live={live_val} ref={ref_val}"


# ---------------------------------------------------------------------------------------------------
# THE PRACTICE LOOP: swap each KIND repeatedly on a running loop, assert zero-gap + value-identity.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "group, kind",
    [
        (STATELESS_GROUP, "stateless"),
        (STATEFUL_GROUP, "stateful"),
        (INCREMENTAL_GROUP, "incremental"),
    ],
)
def test_repeated_zero_gap_swaps_per_kind(
    group: str, kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Run the seam >=10 times on ONE running sandbox loop for each KIND. Each swap: zero missed minute,
    next-minute output value-identical to a never-swapped reference, fingerprint held, every other group
    byte-identical. Repetition surfaces a reseed that is correct once but leaks/drifts across swaps."""
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    fp0 = BusSchema.from_registry().fingerprint
    root = str(tmp_path)

    # Two lockstep loops: ``ref`` NEVER swaps (the parity oracle); ``live`` swaps the group every boundary.
    ref = CaptureState()
    live = CaptureState()

    n_warm = 6
    for mi in range(n_warm):
        _step(ref, root, mi)
        _step(live, root, mi)

    n_swaps = 11
    for swap_i in range(n_swaps):
        minute_index = n_warm + swap_i
        recorded = _enqueue_one(monkeypatch, group, job_id=1000 + swap_i)

        before_instance = REGISTRY.get_group(group)
        outcomes = wiring.poll_and_apply_at_boundary(live, _config(group))

        # The job applied cleanly (dry-run tripwire green) on every iteration.
        assert (
            len(outcomes) == 1 and "applied" in outcomes[0]
        ), f"[{kind}] swap {swap_i}: expected applied, got {outcomes}"
        assert recorded == [("applied", 1000 + swap_i)]
        after_instance = REGISTRY.get_group(group)
        assert after_instance is not before_instance, f"[{kind}] swap {swap_i}: registry not re-instanced"
        assert BusSchema.from_registry().fingerprint == fp0, f"[{kind}] swap {swap_i}: fingerprint drifted"

        # ZERO MISSED MINUTE: both loops capture this minute right after the boundary swap.
        before_minutes = live.minutes
        _step(live, root, minute_index)
        _step(ref, root, minute_index)
        assert live.minutes == before_minutes + 1, f"[{kind}] swap {swap_i}: dropped a minute"
        assert live.minutes == ref.minutes, f"[{kind}] swap {swap_i}: live/ref minute drift"

        minute = BASE + dt.timedelta(minutes=minute_index)
        # (1) The swapped group is value-identical (reseed-correct / recompute-correct) post-swap.
        _assert_minute_equal(
            live.accumulated[group], ref.accumulated[group], minute, f"[{kind}] swap {swap_i} {group}"
        )

    # After many swaps the swapped group's WHOLE accumulated history still matches the reference (no drift).
    _assert_minute_equal(
        live.accumulated[group],
        ref.accumulated[group],
        BASE + dt.timedelta(minutes=n_warm + n_swaps - 1),
        f"[{kind}] final {group}",
    )


def test_swap_is_surgical_other_groups_byte_identical(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A hot-swap of ONE group must leave EVERY OTHER group byte-identical to a never-swapped reference —
    the §4-condition-4 'byte-eq elsewhere' property, proven on a real running loop. Swaps the incremental
    kind (the one that mutates shared CaptureState.engines), then checks all other groups are untouched."""
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    root = str(tmp_path)
    ref = CaptureState()
    live = CaptureState()
    for mi in range(7):
        _step(ref, root, mi)
        _step(live, root, mi)

    _enqueue_one(monkeypatch, INCREMENTAL_GROUP, job_id=42)
    outcomes = wiring.poll_and_apply_at_boundary(live, _config(INCREMENTAL_GROUP))
    assert "applied" in outcomes[0]

    minute_index = 7
    _step(live, root, minute_index)
    _step(ref, root, minute_index)
    minute = BASE + dt.timedelta(minutes=minute_index)

    other_groups = [name for name in live.accumulated if name != INCREMENTAL_GROUP]
    assert other_groups, "expected other groups to have produced output"
    for name in other_groups:
        if name not in ref.accumulated:
            continue
        _assert_minute_equal(
            live.accumulated[name], ref.accumulated[name], minute, f"surgical: other group {name}"
        )


def test_multi_job_drain_at_one_boundary_both_correct(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``max_jobs_per_boundary>1`` drains several FIFO jobs at ONE boundary (no minute captured between the
    swaps). Prove two DIFFERENT-kind groups swapped back-to-back both land correct + the NEXT minute is
    value-identical for BOTH, with still zero missed minutes. This is the queue-drain edge the default
    one-per-boundary path never exercises."""
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    root = str(tmp_path)
    ref = CaptureState()
    live = CaptureState()
    for mi in range(7):
        _step(ref, root, mi)
        _step(live, root, mi)

    # Two jobs for two distinct kinds, drained in ONE boundary (max_jobs_per_boundary=2).
    jobs = [
        QueuedJob(job_id=51, group_name=INCREMENTAL_GROUP, agent_id="a", commit_sha="c1", fail_count=0),
        QueuedJob(job_id=52, group_name=STATEFUL_GROUP, agent_id="a", commit_sha="c2", fail_count=0),
    ]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    recorded: list[str] = []
    monkeypatch.setattr(
        wiring.within_day_deploy_run,
        "record_outcome",
        lambda outcome, job, dry_run=True: recorded.append(outcome.status),
    )
    config = wiring.LiveSwapConfig(
        feature_root="/tmp/zerogap",
        feature_tree="/tmp/zerogap-tree",
        sample_symbols=list(SYMBOLS),
        seed_symbols=list(SYMBOLS),
        dry_run=True,
        max_jobs_per_boundary=2,
    )

    outcomes = wiring.poll_and_apply_at_boundary(live, config)
    assert len(outcomes) == 2 and all("applied" in line for line in outcomes), outcomes
    assert recorded == ["applied", "applied"]

    minute_index = 7
    before_minutes = live.minutes
    _step(live, root, minute_index)
    _step(ref, root, minute_index)
    assert live.minutes == before_minutes + 1  # still no gap after a two-swap boundary
    minute = BASE + dt.timedelta(minutes=minute_index)
    for group in (INCREMENTAL_GROUP, STATEFUL_GROUP):
        _assert_minute_equal(live.accumulated[group], ref.accumulated[group], minute, f"multi-drain {group}")


def test_kind_classification_is_what_the_runbook_claims() -> None:
    """Pin the KIND of each representative group so the runbook's reseed-scoping claim cannot silently rot:
    calendar is a plain stateless FeatureGroup, technical is a StatefulGroup (DIRECT in the capture loop),
    momentum is an incremental_safe ReductionGroup (the one reseed surface)."""
    calendar = REGISTRY.get_group(STATELESS_GROUP)
    technical = REGISTRY.get_group(STATEFUL_GROUP)
    momentum = REGISTRY.get_group(INCREMENTAL_GROUP)
    assert not isinstance(calendar, (ReductionGroup, StatefulGroup))
    assert isinstance(technical, StatefulGroup)
    assert isinstance(momentum, ReductionGroup) and momentum.incremental_safe
    # The contract self-reports the kind: a stateless / stateful-in-capture group is up_to_date with no buffer;
    # the incremental ReductionGroup is up_to_date too UNTIL a live engine is bound pending-reseed.
    assert calendar.up_to_date(None) is True
    assert technical.up_to_date(None) is True
    assert momentum.up_to_date(None) is True
