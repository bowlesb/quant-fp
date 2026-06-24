"""ZERO-GAP PROOF for the WDPC live capture-loop seam (quantlib/features/within_day_live_wiring.py).

The canary (tests/test_within_day_live_wiring.py) already proves ``apply_in_running_loop`` swaps a group
mid-loop. THIS suite proves the SEAM that the live capture loop calls — ``poll_and_apply_at_boundary`` —
end-to-end against a RUNNING in-RAM ``process_bars`` loop:

  * the seam is a PURE NO-OP unless ``FP_WDPC_LIVE_SWAP=1`` (the arm gate);
  * armed, it claims a FIFO job, runs the full pipeline (scope-guard → merge → hot-swap → tripwire → applied)
    with the four LIVE callbacks bound to the running CaptureState, and records the queue outcome;
  * the swap happens AT THE MINUTE BOUNDARY with NO MISSED MINUTE (the loop captures every minute, the swap
    sits between two of them) and the swapped group's NEXT-minute output is reseed-correct (value-identical to
    a never-swapped reference — the RunningState contract restored parity);
  * a tripwire FAILURE rolls the swap back (the group reverts) and the queue records 'rolled_back';
  * a scope-guard FAILURE escalates with no merge / no swap.

ZERO live container, ZERO DB, ZERO git: the queue is a fake FIFO list, the merge/tripwire are injected, and
the swap is a REAL registry hot-swap against a sandbox CaptureState (the same one the canary uses). This is
the proof that gates flipping ``FP_WDPC_LIVE_SWAP=1`` on the crypto canary.
"""

from __future__ import annotations

import datetime as dt
import math
import tempfile

import pytest

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.bus.schema import BusSchema
from quantlib.features import within_day_live_wiring as wiring
from quantlib.features.capture import CaptureState, process_bars
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_applier import ApplyOutcome, DeployJob
from quantlib.features.within_day_deploy_queue import QueuedJob

BASE = dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)
SYMBOLS = ("AAA", "BBB", "CCC")
GROUP = "momentum"  # incremental_safe → its engine carries state → the swap exercises the reseed path


def _bars(minute: dt.datetime) -> list[dict]:
    rows = []
    for offset, sym in enumerate(SYMBOLS):
        i = int((minute - BASE).total_seconds() // 60)
        close = 100.0 + offset * 2.0 + 5.0 * math.sin((i + offset) / 9.0) + i * 0.02
        vol = 800.0 + ((i * 7 + offset) % 40) * 25.0
        rows.append(
            {"S": sym, "o": close * 0.999, "c": close, "h": close * 1.002,
             "l": close * 0.998, "v": vol, "t": minute.isoformat()}
        )
    return rows


def _config(dry_run: bool = True) -> wiring.LiveSwapConfig:
    return wiring.LiveSwapConfig(
        feature_root="/tmp/zerogap",
        feature_tree="/tmp/zerogap-tree",
        sample_symbols=list(SYMBOLS),
        seed_symbols=list(SYMBOLS),
        dry_run=dry_run,
    )


def _warm(state: CaptureState, root: str, n: int = 8) -> None:
    for mi in range(n):
        process_bars(state, _bars(BASE + dt.timedelta(minutes=mi)), root, "mock",
                     "2026-06-18", 120, accumulate=True, write=False)


# ---- the arm gate: pure no-op unless FP_WDPC_LIVE_SWAP=1 -----------------------------------------


def test_seam_is_noop_when_unarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wiring.LIVE_SWAP_ENV, raising=False)
    state = CaptureState()
    # claim_next must NEVER be called when unarmed (no DB touch).
    monkeypatch.setattr(
        wiring, "claim_next", lambda **_k: (_ for _ in ()).throw(AssertionError("claimed while unarmed"))
    )
    assert wiring.poll_and_apply_at_boundary(state, _config()) == []


def test_live_swap_enabled_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    assert wiring.live_swap_enabled() is True
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "0")
    assert wiring.live_swap_enabled() is False


# ---- ⭐ ZERO-GAP: a job hot-swaps mid-loop with NO missed minute + reseed-correct parity ----------


def test_seam_hot_swaps_mid_loop_zero_gap_reseed_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    fp0 = BusSchema.from_registry().fingerprint

    # A one-shot fake FIFO queue: one job for the owned group, then empty.
    jobs = [QueuedJob(job_id=7, group_name=GROUP, agent_id="agent-Z", commit_sha="cafe123", fail_count=0)]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    # The merge + tripwire + outcome-record are injected as inert (dry_run config keeps them logging-only),
    # but force the tripwire-confirm GREEN via the config's dry_run path (live_confirm_tripwire returns True).
    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        wiring.within_day_deploy_run, "record_outcome",
        lambda outcome, job, dry_run=True: recorded.append((outcome.status, job.job_id)),
    )

    with tempfile.TemporaryDirectory() as root:
        # REFERENCE loop: a separate state that NEVER swaps — its minute-9 momentum output is the parity oracle.
        ref = CaptureState()
        _warm(ref, root, 9)  # minutes 0..8 → ref has computed minute 8

        # LIVE loop: warm 0..7, then at the boundary the seam swaps momentum, then capture minute 8.
        state = CaptureState()
        _warm(state, root, 8)
        assert state.minutes == 8
        before = REGISTRY.get_group(GROUP)

        outcomes = wiring.poll_and_apply_at_boundary(state, _config(dry_run=True))

        # The job applied (clean gate + green dry-run tripwire), recorded as 'applied', queue drained.
        assert len(outcomes) == 1 and "applied" in outcomes[0]
        assert recorded == [("applied", 7)]
        after = REGISTRY.get_group(GROUP)
        assert after is not before  # a FRESH instance is registered (the swap took effect)
        assert BusSchema.from_registry().fingerprint == fp0  # fp held across the swap

        # NO MISSED MINUTE: the loop captures minute 8 right after the boundary swap.
        minutes_before = state.minutes
        process_bars(state, _bars(BASE + dt.timedelta(minutes=8)), root, "mock",
                     "2026-06-18", 120, accumulate=True, write=False)
        assert state.minutes == minutes_before + 1  # captured, no gap

        # RESEED-CORRECT PARITY: the swapped loop's minute-8 momentum == the never-swapped reference's, because
        # the RunningState contract reseeded the engine from the same buffer (seed(H);fold(m) == seed(H+m)).
        minute8 = BASE + dt.timedelta(minutes=8)
        live_m8 = state.accumulated[GROUP].filter(
            state.accumulated[GROUP]["minute"] == minute8
        ).sort("symbol")
        ref_m8 = ref.accumulated[GROUP].filter(
            ref.accumulated[GROUP]["minute"] == minute8
        ).sort("symbol")
        assert live_m8.height == ref_m8.height > 0
        feature_cols = [c for c in live_m8.columns if c not in ("symbol", "minute")]
        for col in feature_cols:
            for live_val, ref_val in zip(live_m8[col].to_list(), ref_m8[col].to_list()):
                if live_val is None or ref_val is None or (live_val != live_val):
                    assert (live_val is None) == (ref_val is None) and (live_val != live_val) == (
                        ref_val != ref_val
                    )
                    continue
                assert math.isclose(live_val, ref_val, rel_tol=1e-9, abs_tol=1e-9), (
                    f"reseed parity broke on {col}: live={live_val} ref={ref_val}"
                )


# ---- rollback + escalate paths through the seam --------------------------------------------------


def test_seam_rolls_back_on_tripwire_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    jobs = [QueuedJob(job_id=8, group_name=GROUP, agent_id="a", commit_sha="dead", fail_count=0)]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    recorded: list[str] = []
    monkeypatch.setattr(
        wiring.within_day_deploy_run, "record_outcome",
        lambda outcome, job, dry_run=True: recorded.append(outcome.status),
    )
    rolled: list[str] = []
    # Force the tripwire RED + capture the rollback (the dry_run swap/merge stay inert; we override the two
    # callbacks the seam builds by monkeypatching the module-level builders).
    monkeypatch.setattr(wiring, "live_confirm_tripwire", lambda group, config: False)
    monkeypatch.setattr(
        wiring, "live_rollback_swap", lambda state, group, config, prior: rolled.append(group)
    )

    with tempfile.TemporaryDirectory() as root:
        state = CaptureState()
        _warm(state, root, 8)
        outcomes = wiring.poll_and_apply_at_boundary(state, _config(dry_run=True))

    assert len(outcomes) == 1 and "rolled_back" in outcomes[0]
    assert recorded == ["rolled_back"]
    assert rolled == [GROUP]  # the one group's swap was reverted


def test_seam_escalates_on_scope_violation_no_merge_no_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    jobs = [QueuedJob(job_id=9, group_name=GROUP, agent_id="a", commit_sha="beef", fail_count=0)]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    recorded: list[str] = []
    monkeypatch.setattr(
        wiring.within_day_deploy_run, "record_outcome",
        lambda outcome, job, dry_run=True: recorded.append(outcome.status),
    )
    # Make the gate FAIL by returning evidence with a fingerprint move (cannot ride the silent swap path).
    real_gather = wiring.gather_live_evidence

    def _bad_evidence(job: QueuedJob, config: wiring.LiveSwapConfig):  # type: ignore[no-untyped-def]
        ev = real_gather(job, config)
        ev.fingerprint_after = ev.fingerprint_before ^ 0x1  # forge a fingerprint change
        return ev

    monkeypatch.setattr(wiring, "gather_live_evidence", _bad_evidence)
    swapped: list[str] = []
    monkeypatch.setattr(wiring, "live_do_swap", lambda state, group, config: swapped.append(group))
    merged: list[str] = []
    monkeypatch.setattr(wiring, "live_do_merge", lambda job, config: merged.append(job.group_name))

    with tempfile.TemporaryDirectory() as root:
        state = CaptureState()
        _warm(state, root, 8)
        before = REGISTRY.get_group(GROUP)
        outcomes = wiring.poll_and_apply_at_boundary(state, _config(dry_run=True))
        after = REGISTRY.get_group(GROUP)

    assert len(outcomes) == 1 and "escalated" in outcomes[0]
    assert recorded == ["escalated"]
    assert swapped == [] and merged == []  # NO swap, NO merge on a gate fail
    assert after is before  # the registry was NOT mutated


def test_seam_drains_empty_queue_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: None)  # empty queue
    state = CaptureState()
    assert wiring.poll_and_apply_at_boundary(state, _config()) == []
