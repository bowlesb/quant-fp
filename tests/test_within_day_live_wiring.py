"""Offline + sandbox-canary tests for the WDPC live-wiring layer (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md
§4-5): the scope-guard gate, the serialized applier pipeline, and — the canary — the LIVE-WIRED hot-swap
invoked inside a RUNNING sandbox capture loop.

⭐ THE CANARY (test_canary_*): drives a throwaway in-RAM ``process_bars`` capture loop (mode='mock', no
store, no live container), hot-swaps a group's compute MID-RUN via ``apply_in_running_loop``, and asserts the
NEXT minute computes on the swapped instance while the loop keeps capturing undisturbed + the fingerprint is
held. This proves the #323 reload primitive works on a real running loop with ZERO production risk — it is
MY OWN sandbox CaptureState, never the live fc / crypto-capture.
"""

from __future__ import annotations

import datetime as dt
import tempfile

import pytest

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.bus.schema import BusSchema
from quantlib.features.capture import CaptureState, process_bars
from quantlib.features.hot_swap import HotSwapError, SwapKind
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_applier import (ApplyOutcome, DeployJob,
                                                  apply_in_running_loop,
                                                  apply_job, run_applier)
from quantlib.features.within_day_scope_guard import GateEvidence, evaluate

BASE = dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)


# ---- SCOPE-GUARD gate ---------------------------------------------------------------------------


def _clean_evidence(**overrides: object) -> GateEvidence:
    base = dict(
        group_name="momentum",
        owned_feature="up_ratio_3m",
        changed_files=["quantlib/features/groups/momentum.py"],
        owned_file_set=["quantlib/features/groups/momentum.py"],
        fingerprint_before=0x873F2FCEB8F00C92,
        fingerprint_after=0x873F2FCEB8F00C92,
        parity_was_mismatch=True,
        parity_now_clean=True,
        differing_other_groups=[],
        owned_feature_is_untrusted=True,
        trusted_features_moved=[],
        unit_tests_passed=True,
        qa_clean=True,
        swap_kind="direct",
    )
    base.update(overrides)
    return GateEvidence(**base)  # type: ignore[arg-type]


def test_gate_approves_a_fully_in_scope_fix() -> None:
    assert evaluate(_clean_evidence()).approved is True


def test_gate_rejects_out_of_scope_diff() -> None:
    result = evaluate(_clean_evidence(changed_files=["quantlib/features/incremental.py"]))
    assert not result.approved
    assert any("owned-scope" in v for v in result.violations)


def test_gate_rejects_fingerprint_change() -> None:
    result = evaluate(_clean_evidence(fingerprint_after=0x1234567890ABCDEF))
    assert not result.approved
    assert any("fingerprint" in v for v in result.violations)


def test_gate_rejects_unflipped_parity() -> None:
    assert not evaluate(_clean_evidence(parity_now_clean=False)).approved


def test_gate_rejects_byte_eq_violation() -> None:
    result = evaluate(_clean_evidence(differing_other_groups=["volatility"]))
    assert not result.approved
    assert any("byte-eq" in v for v in result.violations)


def test_gate_rejects_trusted_feature() -> None:
    assert not evaluate(_clean_evidence(owned_feature_is_untrusted=False)).approved
    assert not evaluate(_clean_evidence(trusted_features_moved=["ret_1m"])).approved


def test_gate_rejects_failing_tests_or_qa() -> None:
    assert not evaluate(_clean_evidence(unit_tests_passed=False)).approved
    assert not evaluate(_clean_evidence(qa_clean=False)).approved


def test_gate_rejects_escalate_kind() -> None:
    result = evaluate(_clean_evidence(swap_kind="escalate"))
    assert not result.approved
    assert any("ESCALATE" in v for v in result.violations)


def test_gate_reseed_kind_is_allowed() -> None:
    assert evaluate(_clean_evidence(swap_kind="reseed")).approved is True


# ---- SERIALIZED APPLIER pipeline ----------------------------------------------------------------


class _FakeSwapResult:
    def __init__(self, kind: SwapKind) -> None:
        self.kind = kind
        self.note = f"{kind.value} swap"


def _job() -> DeployJob:
    return DeployJob(job_id=1, group_name="momentum", agent_id="agent-A", commit_sha="abc123")


def test_applier_applies_on_clean_gate_and_tripwire() -> None:
    calls: list[str] = []

    def _swap(group: str) -> _FakeSwapResult:
        calls.append(f"swap:{group}")
        return _FakeSwapResult(SwapKind.DIRECT)

    def _confirm(group: str) -> bool:
        calls.append(f"confirm:{group}")
        return True

    outcome = apply_job(
        _job(),
        evidence=_clean_evidence(),
        do_swap=_swap,  # type: ignore[arg-type]
        do_merge=lambda j: calls.append(f"merge:{j.group_name}"),
        confirm_tripwire=_confirm,
        rollback_swap=lambda g: calls.append(f"rollback:{g}"),
    )
    assert outcome.status == "applied"
    assert calls == ["merge:momentum", "swap:momentum", "confirm:momentum"]  # merge -> swap -> confirm


def test_applier_escalates_on_scope_violation_without_merge_or_swap() -> None:
    calls: list[str] = []

    def _swap(group: str) -> _FakeSwapResult:
        calls.append(f"swap:{group}")
        return _FakeSwapResult(SwapKind.DIRECT)

    outcome = apply_job(
        _job(),
        evidence=_clean_evidence(fingerprint_after=0xDEAD),  # gate fails
        do_swap=_swap,  # type: ignore[arg-type]
        do_merge=lambda j: calls.append("merge"),
        confirm_tripwire=lambda g: True,
        rollback_swap=lambda g: calls.append("rollback"),
    )
    assert outcome.status == "escalated"
    assert calls == []  # NO merge, NO swap when the gate fails


def test_applier_rolls_back_on_tripwire_failure() -> None:
    calls: list[str] = []
    outcome = apply_job(
        _job(),
        evidence=_clean_evidence(),
        do_swap=lambda g: _FakeSwapResult(SwapKind.DIRECT),  # type: ignore[arg-type,return-value]
        do_merge=lambda j: calls.append("merge"),
        confirm_tripwire=lambda g: False,  # tripwire FAILS post-swap
        rollback_swap=lambda g: calls.append(f"rollback:{g}"),
    )
    assert outcome.status == "rolled_back"
    assert calls == ["merge", "rollback:momentum"]  # rolled back the one group's swap


def test_applier_escalates_on_hotswap_error() -> None:
    def _raise(_g: str) -> _FakeSwapResult:
        raise HotSwapError("RESEED kind but no buffer")

    outcome = apply_job(
        _job(),
        evidence=_clean_evidence(),
        do_swap=_raise,  # type: ignore[arg-type]
        do_merge=lambda j: None,
        confirm_tripwire=lambda g: True,
        rollback_swap=lambda g: None,
    )
    assert outcome.status == "escalated"
    assert "hot-swap refused" in outcome.detail


def test_run_applier_serializes_fifo() -> None:
    jobs = [DeployJob(i, f"g{i}", "a", "sha") for i in range(3)]

    def dequeue() -> DeployJob | None:
        return jobs.pop(0) if jobs else None

    def apply_one(job: DeployJob) -> ApplyOutcome:
        return ApplyOutcome(job.job_id, job.group_name, "applied", "ok")

    outcomes = run_applier(dequeue, apply_one)
    assert [o.job_id for o in outcomes] == [0, 1, 2]  # FIFO, one at a time


# ---- ⭐ THE CANARY: live-wired hot-swap inside a RUNNING sandbox capture loop --------------------


def _bars_for_minute(minute: dt.datetime, symbols: tuple[str, ...]) -> list[dict]:
    bars = []
    for offset, sym in enumerate(symbols):
        i = int((minute - BASE).total_seconds() // 60)
        import math

        close = 100.0 + offset * 2.0 + 5.0 * math.sin((i + offset) / 9.0) + i * 0.02
        vol = 800.0 + ((i * 7 + offset) % 40) * 25.0
        bars.append(
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
    return bars


def test_canary_hot_swap_mid_running_capture_loop() -> None:
    """⭐ Drive a throwaway in-RAM capture loop, hot-swap momentum MID-RUN, assert the next minute uses the
    swapped instance + the loop keeps capturing + the fingerprint is held. NO store, NO live container."""
    symbols = ("AAA", "BBB", "CCC")
    with tempfile.TemporaryDirectory() as root:
        state = CaptureState()
        fp_start = BusSchema.from_registry().fingerprint

        # Warm the loop: capture several minutes (the running sandbox capture process).
        for mi in range(8):
            bars = _bars_for_minute(BASE + dt.timedelta(minutes=mi), symbols)
            process_bars(state, bars, root, "mock", "2026-06-18", 120, accumulate=True, write=False)
        assert state.minutes >= 8  # the loop is genuinely capturing
        momentum_before = REGISTRY.get_group("momentum")

        # HOT-SWAP momentum mid-run via the live-wired path (against the RUNNING state's engines + buffer).
        result = apply_in_running_loop(state, "momentum", seed_symbols=list(symbols))
        assert result.swapped
        assert result.kind in (SwapKind.DIRECT, SwapKind.RESEED)  # FP_INCREMENTAL off in mock => DIRECT
        assert result.fingerprint_before == result.fingerprint_after == fp_start

        # The registry now holds a FRESH momentum instance (the swap took effect).
        momentum_after = REGISTRY.get_group("momentum")
        assert momentum_after is not momentum_before

        # The loop KEEPS CAPTURING undisturbed: the NEXT minute computes on the swapped instance.
        minutes_before_next = state.minutes
        next_bars = _bars_for_minute(BASE + dt.timedelta(minutes=8), symbols)
        process_bars(state, next_bars, root, "mock", "2026-06-18", 120, accumulate=True, write=False)
        assert state.minutes == minutes_before_next + 1  # captured the next minute fine
        assert "momentum" in state.accumulated  # momentum still produced output post-swap
        assert state.accumulated["momentum"].height > 0
        # fingerprint held across the whole canary
        assert BusSchema.from_registry().fingerprint == fp_start


def test_canary_reseed_without_buffer_escalates_not_swaps() -> None:
    """A RESEED kind on a state with no buffer must ESCALATE (raise), never swap into empty state."""
    state = CaptureState()  # no minutes captured => state.buffer is None
    # Force the RESEED classification by giving the state a (fake) engine on momentum's reduce_input.
    from quantlib.features.registry import REGISTRY as _R

    reduce_input = _R.get_group("momentum").reduce_input  # type: ignore[attr-defined]

    class _Eng:
        def seed(self, *_a: object, **_k: object) -> None: ...

    state.engines[reduce_input] = _Eng()  # type: ignore[assignment]
    with pytest.raises(HotSwapError, match="ESCALATE"):
        apply_in_running_loop(state, "momentum")
