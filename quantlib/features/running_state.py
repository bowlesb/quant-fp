"""The running-state contract for held-state (B-fold) features — Ben's canonical cold-start pattern.

Every running-state object the live path carries between minutes exposes ONE staleness guard and ONE lazy
rebuild. The compute/emit path checks ``up_to_date(buffer)`` BEFORE folding each minute; if the state is NOT
up to date it calls ``rebuild_from_history(buffer)`` first (the expensive one-time reseed — fine that the first
time is slower), then folds. Once up to date, every minute is the O(1) fold.

WHY THIS RESOLVES THE TWO HARD PARTS (cold-start + backfill parity) — by construction, not by bespoke wiring:

- **Backfill parity for free.** ``rebuild_from_history`` seeds from the SAME historical window the backfill
  recomputes over, so the instant ``up_to_date`` flips true the live held-state EQUALS the backfill state. And
  because the guard precedes EVERY compute, a stale state can never silently emit a wrong value — it reseeds, or
  it is already correct.
- **Morning / session boundary** becomes a single decision: *what window does the rebuild seed from* (carry the
  state across the overnight gap, or reset and reseed from the session's bars). Encode that in
  ``rebuild_from_history`` / ``up_to_date``; the warm-up minutes before the rebuild completes are the usual
  RTH-excluded warmup nulls, now with an EXPLICIT mechanism (``up_to_date()`` is False until seeded).
- **Hot-swap reseed is subsumed.** After a real-time code swap the new state reports ``up_to_date() == False`` and
  lazily reseeds before the next minute — no separate eager-reseed step.

Staleness triggers (the cases ``up_to_date`` must return False on): first-ever deploy (cold), morning relaunch
(cold), a hot-swap (code changed → state invalid for the new logic), a stream gap (missed minutes), and a
rewound/replayed buffer. A concrete state declares which of these apply to it (e.g. a session-reset feature also
goes stale at the session boundary).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import polars as pl


@runtime_checkable
class RunningState(Protocol):
    """The minimal contract every held-state object implements. The compute guard is always:

        if not state.up_to_date(buffer):
            state.rebuild_from_history(buffer)   # lazy, one-time-expensive reseed from the SAME history backfill uses
        # ... now fold the unabsorbed tail of ``buffer`` and emit (O(1)/minute) ...

    Keeping it to these two methods (plus the group's own fold/emit) is the whole abstraction — no per-group
    warm-start wiring, no eager reseed scheduling. ``buffer`` is the trailing-window frame the live path already
    materializes each minute (the historical window); both methods read it."""

    def up_to_date(self, buffer: pl.DataFrame | None) -> bool:
        """True iff the carried state can fold ``buffer``'s newest minute(s) directly and emit a value EQUAL to
        the backfill recompute. False on any staleness trigger (cold / session boundary / gap / hot-swap /
        rewind) — which makes the guard reseed before emitting, so a stale state never emits a wrong value.
        """
        ...

    def rebuild_from_history(self, buffer: pl.DataFrame | None) -> None:
        """Reseed the state from the historical window in ``buffer`` (the SAME bars backfill recomputes over), so
        that immediately after, ``up_to_date(buffer)`` is True and the state == the backfill state by
        construction. The one-time-expensive refresh; called lazily by the compute guard, never eagerly."""
        ...
