"""Offline tests for the WDPC Phase-3 first build: the per-group hot-swap mechanism + the watch tripwire's
change detector + the in-sandbox reconfirm fix-proof. ALL offline — a sandbox Registry / synthetic frames /
a mock engine; NOTHING touches the live fc container, the bus, or the store.

The hot-swap is KIND-AGNOSTIC (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §3.3 — the single self-healing
contract, no SwapKind classifier): swap the code, then ``if not group.up_to_date(buffer):
group.rebuild_from_history(buffer)``. The three former kinds collapse into what the group's OWN contract does:
DIRECT = a stateless group stays ``up_to_date()==True`` post-swap (no reseed); RESEED = a group carrying state
(a bound live engine) reports ``False`` → ``rebuild_from_history`` reseeds; ESCALATE = a fingerprint-affecting
swap, or a not-up-to-date group with no buffer to reseed from, raises ``HotSwapError``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.bus.schema import BusSchema
from quantlib.features import within_day_reconfirm as rc
from quantlib.features.hot_swap import HotSwapError, hot_swap_group
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_watch import _changed

BASE = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)


def _minute_agg(symbols: tuple[str, ...], n: int) -> pl.DataFrame:
    rows = []
    for offset, symbol in enumerate(symbols):
        for i in range(n):
            close = 100.0 + offset * 2.0 + 5.0 * math.sin((i + offset) / 9.0) + i * 0.02
            rows.append(
                {
                    "symbol": symbol,
                    "minute": BASE + timedelta(minutes=i),
                    "close": close,
                    "volume": 800.0 + ((i * 7 + offset) % 40) * 25.0,
                }
            )
    return pl.DataFrame(rows)


# ---- HOT-SWAP: DIRECT kind ----------------------------------------------------------------------


def test_direct_swap_replaces_instance_and_keeps_fingerprint() -> None:
    fp_before = BusSchema.from_registry().fingerprint
    old_instance = REGISTRY.get_group("momentum")
    result = hot_swap_group("momentum", engines=None)  # no engine ⇒ stateless ⇒ up_to_date, no reseed
    new_instance = REGISTRY.get_group("momentum")

    assert result.swapped and not result.reseeded  # the DIRECT case: contract guard was a no-op
    assert new_instance is not old_instance  # a FRESH instance is installed (the swap happened)
    assert new_instance.name == "momentum"
    assert result.fingerprint_before == result.fingerprint_after == fp_before
    # the GLOBAL fingerprint is unchanged ⇒ publisher/codec/schema untouched
    assert BusSchema.from_registry().fingerprint == fp_before


def test_direct_swap_next_compute_uses_new_instance() -> None:
    # After the swap the registry returns the new instance; the live loop re-fetches from REGISTRY each
    # minute (capture.py:369), so the next minute's compute runs on the swapped instance.
    frame = _minute_agg(("AAA", "BBB"), 200)
    hot_swap_group("momentum", engines=None)
    swapped = REGISTRY.get_group("momentum")
    from quantlib.features.base import BatchContext

    out = swapped.compute_latest(BatchContext(frames={swapped.reduce_input: frame}))
    assert out.height > 0  # the swapped instance computes (same logic, fresh object)


def test_stateless_group_is_up_to_date_post_swap() -> None:
    # The DIRECT case via the contract: a reduction with NO live engine recomputes from the ring each minute,
    # so post-swap it is up_to_date and the guard reseeds nothing.
    frame = _minute_agg(("AAA", "BBB"), 50)
    assert REGISTRY.get_group("momentum").up_to_date(frame) is True
    result = hot_swap_group("momentum", engines={})  # empty engines ⇒ no bound engine ⇒ stateless
    assert not result.reseeded
    assert REGISTRY.get_group("momentum").up_to_date(frame) is True


# ---- HOT-SWAP: RESEED kind ----------------------------------------------------------------------


class _MockEngine:
    """A stand-in for a live IncrementalEngine that records a seed() call (offline reseed proof)."""

    def __init__(self) -> None:
        self.seeded_with: pl.DataFrame | None = None
        self.seed_symbols: list[str] | None = None

    def seed(self, buffer_frame: pl.DataFrame, symbols: list[str] | None = None, **_: object) -> None:
        self.seeded_with = buffer_frame
        self.seed_symbols = symbols


def test_reseed_swap_seeds_the_engine_via_the_contract() -> None:
    # The RESEED case via the contract: a LIVE engine carries this group's input → it is bound to the swapped
    # instance → up_to_date() reports False → rebuild_from_history reseeds the engine. No SwapKind classifier.
    reduce_input = REGISTRY.get_group("momentum").reduce_input
    engine = _MockEngine()
    engines = {reduce_input: engine}
    frame = _minute_agg(("AAA", "BBB"), 50)
    result = hot_swap_group(
        "momentum", engines=engines, buffer_frame=frame, seed_symbols=["AAA", "BBB"]  # type: ignore[arg-type]
    )
    assert result.swapped and result.reseeded
    assert engine.seeded_with is not None  # the engine WAS reseeded from the buffer
    assert engine.seed_symbols == ["AAA", "BBB"]
    assert result.fingerprint_before == result.fingerprint_after
    # After the contract reseed the swapped group is up to date again.
    assert REGISTRY.get_group("momentum").up_to_date(frame) is True


# ---- HOT-SWAP: ESCALATE kind --------------------------------------------------------------------


def test_reseed_needed_without_buffer_escalates() -> None:
    group = REGISTRY.get_group("momentum")
    engines = {group.reduce_input: _MockEngine()}  # a bound engine ⇒ not up_to_date post-swap...
    with pytest.raises(HotSwapError, match="ESCALATE"):
        hot_swap_group("momentum", engines=engines, buffer_frame=None)  # type: ignore[arg-type]  # ...no buffer


def test_swap_absent_group_escalates() -> None:
    with pytest.raises((HotSwapError, KeyError)):
        hot_swap_group("a_group_that_does_not_exist", engines=None)


def test_swap_group_refuses_feature_set_change() -> None:
    # A swap that changes the declared feature set is fingerprint-affecting ⇒ swap_group raises ⇒ ESCALATE.
    from quantlib.features.base import FeatureSpec
    from quantlib.features.declarative import ReductionGroup
    from quantlib.features.registry import RegistrationError

    incumbent = REGISTRY.get_group("momentum")

    class _MomentumWithExtraFeature(type(incumbent)):  # type: ignore[misc]
        def declare(self) -> list[FeatureSpec]:
            base = list(incumbent.declare())
            base.append(
                FeatureSpec(
                    name="momentum_bogus_extra",
                    dtype="Float64",
                    description="an extra feature that changes the set and thus the fingerprint",
                )
            )
            return base

    with pytest.raises(RegistrationError, match="feature set changed"):
        REGISTRY.swap_group(_MomentumWithExtraFeature)
    assert issubclass(_MomentumWithExtraFeature, ReductionGroup)  # sanity: it IS a reduction group


# ---- SHARED CAPTURE STATE is untouched by a swap ------------------------------------------------


def test_swap_does_not_touch_other_groups_or_engines() -> None:
    other_before = REGISTRY.get_group("volatility")
    engine = _MockEngine()
    engines = {REGISTRY.get_group("momentum").reduce_input: engine}
    # swap momentum WITHOUT a buffer is an escalate; swap with a buffer reseeds ONLY momentum's engine entry.
    frame = _minute_agg(("AAA", "BBB"), 50)
    hot_swap_group("momentum", engines=engines, buffer_frame=frame)  # type: ignore[arg-type]
    # the OTHER group's instance is unchanged (the swap touched only momentum).
    assert REGISTRY.get_group("volatility") is other_before


# ---- WATCH: the change detector -----------------------------------------------------------------


def test_changed_detects_value_changes_and_ignores_noise() -> None:
    assert _changed(1.0, 2.0) is True
    assert _changed(1.0, 1.0) is False
    assert _changed(float("nan"), float("nan")) is False  # two NaNs = unchanged
    assert _changed(float("nan"), 1.0) is True  # NaN -> finite IS a change (a swap fixed a NaN)
    assert _changed(1.0, float("nan")) is True
    assert _changed(1.0, 1.0 + 1e-15) is False  # within tolerance = unchanged


# ---- RECONFIRM: the in-sandbox fix-proof --------------------------------------------------------


def test_reconfirm_clean_when_latest_matches_rolling() -> None:
    # On a well-conditioned warm window, a group's compute_latest == compute on the latest minute ⇒ clean.
    frame = _minute_agg(("AAA", "BBB", "CCC"), 220)  # > the longest window so windows are warm
    group = REGISTRY.get_group("momentum")
    result = rc.reconfirm_group(group, frame)
    assert result.n_compared > 0
    assert result.clean, f"expected clean reconfirm, mismatched={result.mismatched_features}"
    assert result.n_mismatch == 0


def test_byte_eq_elsewhere_same_registry_is_empty() -> None:
    # With the SAME registry before/after (no fix applied), every other group is byte-identical ⇒ no diffs.
    frame = _minute_agg(("AAA", "BBB"), 200)
    differing = rc.byte_eq_elsewhere("momentum", frame, REGISTRY, REGISTRY)
    assert differing == []
