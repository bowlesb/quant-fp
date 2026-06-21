"""Within-Day Parity Certifier — Phase-3 FIRST BUILD: the per-group HOT-SWAP mechanism (offline-testable).

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

The three kind-classes (§3.3):
  * DIRECT       — batch ReductionGroup (FP_INCREMENTAL off, the live default) / stateless declarative /
                   Class-A SessionCache group: re-import → swap. No reseed (recomputes from the shared ring).
  * SWAP+RESEED  — a group that carries cross-minute state in a live IncrementalEngine: re-import → swap →
                   ``IncrementalEngine.seed(ring.materialize())`` rebuilds the running sums from the buffer.
  * ESCALATE     — the swap would change the fingerprint (feature set / version), OR a reseed-requiring group
                   has no buffer to reseed from: REFUSE the swap and raise, so the caller escalates to the
                   Lead (a coordinated/relaunch deploy) rather than silently corrupting state.

Safety conditions (all enforced here, fail-closed): fingerprint UNCHANGED before/after; the swap is applied
in ONE atomic registry overwrite (the caller invokes it at a minute boundary, never mid-compute); a
reseed-requiring kind without a buffer ESCALATES rather than swapping.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from enum import Enum

import polars as pl

from quantlib.bus.schema import BusSchema
from quantlib.features.base import FeatureGroup
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.registry import REGISTRY, RegistrationError, Registry


class SwapKind(str, Enum):
    DIRECT = "direct"  # stateless per-minute; swap takes effect next minute, no reseed
    RESEED = "reseed"  # carries live incremental state; swap + IncrementalEngine.seed(buffer)
    ESCALATE = "escalate"  # fingerprint-affecting or unseedable → refuse the swap, escalate to Lead


class HotSwapError(Exception):
    """Raised when a hot-swap is refused (fingerprint would change, group absent, or unseedable)."""


@dataclass
class HotSwapResult:
    group_name: str
    kind: SwapKind
    swapped: bool
    reseeded: bool
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


def classify_swap_kind(
    group: FeatureGroup,
    engines: dict[str, IncrementalEngine] | None,
) -> SwapKind:
    """Which kind-class this group's swap is, given the live engines (None offline / FP_INCREMENTAL off).

    A ReductionGroup whose ``reduce_input`` has a LIVE IncrementalEngine carries running state → RESEED. Any
    other group (batch with no live engine, stateless declarative, Class-A cache) recomputes per minute from
    the shared buffer → DIRECT. (A StatefulGroup is folded through its own engine; in this first build it is
    treated as RESEED only if a live engine carries its input, else DIRECT — its per-instance accumulators
    rebuild from the next minute's compute, the warm-up seam documented in §3.3.)"""
    if engines and isinstance(group, ReductionGroup):
        if group.reduce_input in engines:
            return SwapKind.RESEED
    return SwapKind.DIRECT


def hot_swap_group(
    group_name: str,
    *,
    registry: Registry = REGISTRY,
    engines: dict[str, IncrementalEngine] | None = None,
    buffer_frame: pl.DataFrame | None = None,
    seed_symbols: list[str] | None = None,
) -> HotSwapResult:
    """Hot-swap ONE group's compute logic in ``registry``, reseeding its live incremental state if it carries
    any. Returns a HotSwapResult describing the kind + what happened. Raises ``HotSwapError`` to ESCALATE.

    Args:
      registry: the registry to swap in (the live REGISTRY in prod; a sandbox Registry in tests).
      engines: the live ``CaptureState.engines`` (None offline / when FP_INCREMENTAL is off) — used to detect
               carried incremental state + to reseed it.
      buffer_frame: the current raw-bar buffer (``CaptureState.ring.materialize()``) — REQUIRED to reseed a
                    RESEED-kind group; if absent for a RESEED kind, the swap ESCALATES (raises) rather than
                    swapping into empty state.
      seed_symbols: the fixed session symbol set to pin the reseeded index (the shard universe), or None.

    Order: classify kind on the incumbent → (RESEED: require a buffer, else ESCALATE before any swap) →
    fingerprint BEFORE → re-import (the reload routes ``@register`` through ``swap_group`` in place) →
    fingerprint AFTER → assert UNCHANGED (else revert + escalate) → reseed if RESEED."""
    incumbent = registry.get_group(group_name)
    incumbent_cls = type(incumbent)
    kind = classify_swap_kind(incumbent, engines)

    # ESCALATE BEFORE swapping: a RESEED kind with no buffer must not swap into empty state.
    if kind is SwapKind.RESEED and buffer_frame is None:
        raise HotSwapError(
            f"hot_swap '{group_name}': RESEED kind (carries live incremental state) but no buffer_frame "
            f"to reseed from — ESCALATE to a coordinated/relaunch deploy, do not swap into empty state"
        )

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

    reseeded = False
    if kind is SwapKind.RESEED and buffer_frame is not None and engines is not None:
        swapped_group = registry.get_group(group_name)
        reduce_input = swapped_group.reduce_input  # type: ignore[attr-defined]  # RESEED ⇒ ReductionGroup
        engines[reduce_input].seed(buffer_frame, seed_symbols)
        reseeded = True

    note = {
        SwapKind.DIRECT: "swapped; stateless per-minute, no reseed (recomputes from the shared buffer)",
        SwapKind.RESEED: "swapped + reseeded the incremental engine from the current buffer",
    }[kind]
    return HotSwapResult(
        group_name=group_name,
        kind=kind,
        swapped=True,
        reseeded=reseeded,
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
        note=note,
    )
