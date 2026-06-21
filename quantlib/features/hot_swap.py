"""Within-Day Parity Certifier — Phase-3: the per-group HOT-SWAP mechanism (offline-testable).

Per docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §3. Replaces ONE feature group's compute LOGIC in a running
engine BETWEEN minutes, without touching shared capture state, so a parity fix for an UNTRUSTED group can be
deployed in real time. This module is the MECHANISM; it is OFFLINE-TESTED here (a sandbox CaptureState) and
does NOT touch the live fc container — live activation is a separate, Lead-sequenced step.

Grounding (the live compute loop): ``capture.py:process_bars`` re-fetches the group list FRESH from
``REGISTRY`` each minute and looks groups up BY NAME, so overwriting ``REGISTRY._groups[name]`` with a fresh
instance makes the NEXT minute call the new compute. The shared ``CaptureState`` (raw-bar ring buffer,
incremental engines, bus publisher + schema) is SEPARATE from the per-group compute objects and is NOT
touched by the swap; the fingerprint is UNCHANGED (same group:name:version + feature set) so the publisher /
codec / schema are untouched — only that one group's compute swaps.

⭐ THE SINGLE SELF-HEALING RULE (the consolidation — quantlib/features/running_state.py + FeatureGroup): the
applier is KIND-AGNOSTIC. It swaps the code, then runs the ONE contract guard against the new instance:

    if not group.up_to_date(buffer):
        group.rebuild_from_history(buffer)   # lazy reseed from the SAME history backfill recomputes over

There is NO DIRECT/RESEED/ESCALATE classification. The former kinds collapse into what the group's OWN contract
does internally: a stateless group (batch reduction with FP_INCREMENTAL off / declarative / Class-A cache) is
``up_to_date()==True`` → the guard is a no-op (the old DIRECT); a group that carries cross-minute state (swing's
leg-state, an armed incremental engine bound for the swap) reports ``False`` → ``rebuild_from_history`` reseeds
(the old RESEED); a change that can't be restored to parity makes ``rebuild_from_history`` RAISE → the applier
catches it and escalates (the old ESCALATE). The fingerprint is asserted UNCHANGED before/after regardless.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

import polars as pl

from quantlib.bus.schema import BusSchema
from quantlib.features.base import FeatureGroup
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.registry import REGISTRY, RegistrationError, Registry


class HotSwapError(Exception):
    """Raised when a hot-swap is refused (fingerprint would change, group absent, or the contract reseed
    cannot restore parity — the irreducible/ESCALATE case)."""


@dataclass
class HotSwapResult:
    group_name: str
    swapped: bool
    reseeded: bool  # True iff the contract's rebuild_from_history ran (the group reported not-up-to-date)
    fingerprint_before: int
    fingerprint_after: int
    note: str


def reimport_group_class(group_name: str, registry: Registry = REGISTRY) -> type[FeatureGroup]:
    """Re-import the module that defines ``group_name``'s group and return the (re-loaded) group class.

    Uses the incumbent instance's ``__module__`` to find the module, ``importlib.reload`` to re-execute it
    (picking up edited source on the bind-mounted tree), and returns the class whose instance ``.name`` ==
    ``group_name`` from the reloaded module. Raises if the reloaded module no longer defines that group."""
    incumbent = registry.get_group(group_name)
    module = importlib.import_module(type(incumbent).__module__)
    reloaded = importlib.reload(module)
    for attr in vars(reloaded).values():
        if isinstance(attr, type) and issubclass(attr, FeatureGroup) and attr is not FeatureGroup:
            try:
                instance = attr()
            except TypeError:
                continue
            if getattr(instance, "name", None) == group_name:
                return attr
    raise HotSwapError(f"reloaded module {module.__name__} no longer defines group '{group_name}'")


def hot_swap_group(
    group_name: str,
    *,
    registry: Registry = REGISTRY,
    engines: dict[str, IncrementalEngine] | None = None,
    buffer_frame: pl.DataFrame | None = None,
    seed_symbols: list[str] | None = None,
) -> HotSwapResult:
    """Hot-swap ONE group's compute logic in ``registry``, then run the SINGLE self-healing contract guard to
    restore any carried state to parity. Returns a HotSwapResult; raises ``HotSwapError`` to ESCALATE.

    Args:
      registry: the registry to swap in (the live REGISTRY in prod; a sandbox Registry in tests).
      engines: the live ``CaptureState.engines`` (None offline / when FP_INCREMENTAL is off). When an engine
               carries the swapped group's ``reduce_input``, it IS the group's running state — it is bound to
               the group for the swap so the contract's ``rebuild_from_history`` reseeds it (the old RESEED kind,
               now expressed through the one contract).
      buffer_frame: the current raw-bar buffer (``CaptureState.ring.materialize()``) — the history the contract
                    reseeds from. A group that reports not-up-to-date with NO buffer to reseed from ESCALATES.
      seed_symbols: the fixed session symbol set to pin a reseeded engine index (the shard universe), or None.

    Order: fingerprint BEFORE → re-import (the reload routes ``@register`` through ``swap_group`` in place; a
    feature-set change raises → ESCALATE) → fingerprint AFTER → assert UNCHANGED (else revert + escalate) →
    bind the live engine (if any) to the new instance → THE GUARD: ``if not up_to_date: rebuild_from_history``.
    """
    incumbent = registry.get_group(group_name)
    incumbent_cls = type(incumbent)

    fingerprint_before = BusSchema.from_registry().fingerprint
    # Re-import the module: the reload re-runs @register, which (reload-aware) routes a same-feature-set
    # re-registration through swap_group — overwriting REGISTRY._groups[name] in place. A feature-set change
    # raises RegistrationError from swap_group (fingerprint-affecting), surfaced here as ESCALATE.
    try:
        reimport_group_class(group_name, registry)
    except RegistrationError as error:
        raise HotSwapError(f"hot_swap '{group_name}' refused: {error}") from error

    fingerprint_after = BusSchema.from_registry().fingerprint
    if fingerprint_after != fingerprint_before:
        # Revert to the incumbent and escalate — a fingerprint move must never ride the silent swap path.
        registry.swap_group(incumbent_cls)
        raise HotSwapError(
            f"hot_swap '{group_name}': fingerprint changed {fingerprint_before:#018x} -> "
            f"{fingerprint_after:#018x} — reverted; ESCALATE (coordinated deploy required)"
        )

    group = registry.get_group(group_name)
    # BIND the live engine (if one carries this group's input) to the new instance, so the group's OWN contract
    # (ReductionGroup.up_to_date / rebuild_from_history) treats the freshly-swapped engine-backed state as stale
    # and reseeds it — the running state lives in the engine, but the contract is the single surface.
    if engines is not None and isinstance(group, ReductionGroup) and group.reduce_input in engines:
        group.bind_live_engine(engines[group.reduce_input], seed_symbols)

    # ⭐ THE SINGLE GUARD — no kind classification. The group's contract self-reports + self-heals.
    reseeded = False
    if not group.up_to_date(buffer_frame):
        if buffer_frame is None:
            raise HotSwapError(
                f"hot_swap '{group_name}': carries cross-minute state but no buffer_frame to reseed from — "
                f"ESCALATE to a coordinated/relaunch deploy, do not swap into empty state"
            )
        group.rebuild_from_history(buffer_frame)
        reseeded = True

    note = (
        "swapped + contract-reseeded the carried state from the current buffer"
        if reseeded
        else "swapped; up-to-date (stateless / recomputes from the shared buffer), no reseed"
    )
    return HotSwapResult(
        group_name=group_name,
        swapped=True,
        reseeded=reseeded,
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
        note=note,
    )
