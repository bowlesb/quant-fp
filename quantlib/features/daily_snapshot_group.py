"""The Class-A (intraday-invariant) feature pattern, captured ONCE.

Ben's reduced model (docs/FEATURE_PREP_OVERHAUL.md): every feature is one of three patterns —
(A) intraday-invariant -> compute once + cache, (B) windowed reducer -> prior-state + O(1) fold,
(C) point-in-time/event. This module owns pattern A's full machinery so a Class-A group writes ONLY
its math.

A Class-A group's features are a pure function of a per-session-CONSTANT daily snapshot (the prior-day
``daily`` bars): the value for ``(symbol, date)`` is identical at every minute of that date, so it is
computed ONCE per session and broadcast across the day's minutes. Nine groups (``multi_day``,
``multi_day_vwap``, ``prior_day``, ``daily_beta``, ``liquidity_rank``, ``overnight_beta``,
``overnight_intraday_split``, ``return_dispersion``, ``breadth``) each hand-wrote the IDENTICAL four-method
dance to express this:

    _compute_daily(source)         # the group's ACTUAL math (per-(symbol,date) feature columns)
    _daily(ctx)                    # SessionCache.get(daily_snapshot_token(source), _compute_daily)
    compute(ctx)                   # join the cached daily frame onto ALL minutes of the session
    compute_latest(ctx)            # join the cached daily frame onto the LATEST minute only

Only the first method differs between groups; the other three are mechanically identical boilerplate.
``DailySnapshotGroup`` captures the three, so a group subclasses it and implements ONE method
(``daily_snapshot``) — its real per-(symbol, date) computation. The cache, the broadcast, and the
live/backfill split are engine-owned and shared, exactly as ``ReductionGroup`` already does for pattern B.

PARITY (value-identical by construction): ``compute`` and ``compute_latest`` build from the SAME cached
daily frame via the SAME broadcast join; ``compute_latest`` only restricts the broadcast to the latest
minute's rows. The cache is pure memoization keyed on the snapshot's content witness
(``daily_snapshot_token``) — it changes only WHEN ``daily_snapshot`` runs, never WHAT it returns. So a
group migrated onto this base emits cell-for-cell what its hand-rolled four methods emitted (fp-neutral).
"""
from __future__ import annotations

from abc import abstractmethod

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    daily_snapshot_token,
)


class DailySnapshotGroup(FeatureGroup):
    """Base for a Class-A (intraday-invariant) group whose features are a pure function of the per-session
    daily snapshot. The subclass declares its inputs (a ``daily`` frame + a ``minute_agg`` minute grid) and
    its ``declare()`` contract as usual, then implements ONLY ``daily_snapshot`` — its per-(symbol, date)
    feature columns. The cache, the minute broadcast, and the live/backfill split are inherited.

    The daily input frame's name defaults to ``"daily"`` and the minute-grid frame to ``"minute_agg"``;
    a group reading a differently-named snapshot (e.g. a universe/reference frame) overrides
    ``snapshot_input`` / ``minute_input``.
    """

    snapshot_input: str = "daily"
    minute_input: str = "minute_agg"
    minute_columns: tuple[str, ...] = ()  # extra at-T minute_agg cols to carry into broadcast_exprs (A.2)

    @abstractmethod
    def daily_snapshot(self, source: pl.DataFrame, ctx: BatchContext) -> pl.DataFrame:
        """The per-(symbol, date) snapshot the group's features derive from — the ONLY method most Class-A
        groups write. Returns a frame keyed by ``(symbol, date)``. Computed ONCE per session (cached on the
        snapshot content witness) and broadcast across the session's minutes by the base.

        For the common (A.1) group the returned columns ARE the declared features (one per output, broadcast
        verbatim). For an A.2 group that mixes the snapshot with the at-T minute value (e.g. ``prior_day``'s
        ``close/prev_high − 1``), this returns the per-(symbol, date) LEVELS and the group overrides
        ``broadcast_exprs`` to derive the final features from the levels + the at-T ``minute_columns`` after
        the broadcast join.

        ``ctx`` is provided for the (rare) group whose snapshot reads a SECOND per-session-constant input
        beyond the ``daily`` frame (e.g. universe membership for a cross-sectional rank's denominator); the
        common single-input group ignores it. Any extra input read here must be per-session-INVARIANT and its
        witness paired into ``_snapshot_witness`` so a change re-runs this (never a stale cache serve)."""

    def broadcast_exprs(self) -> list[pl.Expr] | None:
        """The final feature expressions for an A.2 group, evaluated over the broadcast frame (the snapshot
        LEVELS joined onto each minute, plus the at-T ``minute_columns``). Default ``None``: the snapshot
        columns ARE the features (A.1, no per-minute step). When overridden, the snapshot returns level columns
        and these exprs (each aliased to a declared feature name, reading the levels + at-T close) produce the
        outputs — the SAME exprs run in ``compute`` (all minutes) and ``compute_latest`` (latest minute), so
        live == backfill by construction."""
        return None

    def _snapshot_witness(self, source: pl.DataFrame, ctx: BatchContext) -> object:
        """The content witness the per-session cache keys on. Default = the daily-snapshot token (id +
        height + last date + close-sum). A group whose snapshot depends on an EXTRA per-session input
        (e.g. universe membership) overrides this to pair the token with that dependency's witness, so a
        change in either re-runs ``daily_snapshot``."""
        return daily_snapshot_token(source)

    def _daily(self, ctx: BatchContext) -> tuple[pl.DataFrame, list[str]]:
        source = ctx.frame(self.snapshot_input)
        names = self.feature_names
        result = self.session_cache.get(
            self._snapshot_witness(source, ctx), lambda: self.daily_snapshot(source, ctx)
        )
        return result, names

    def _broadcast(self, minutes: pl.DataFrame, daily: pl.DataFrame, names: list[str]) -> pl.DataFrame:
        minutes = minutes.select(["symbol", "minute", *self.minute_columns]).with_columns(
            pl.col("minute").dt.date().alias("date")
        )
        joined = minutes.join(daily, on=["symbol", "date"], how="left")
        exprs = self.broadcast_exprs()
        if exprs is None:
            # A.1: the snapshot columns ARE the features — broadcast verbatim.
            return joined.select(["symbol", "minute", *names])
        # A.2: derive the features from the broadcast levels + the at-T minute columns.
        return joined.with_columns(exprs).select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """BACKFILL form: broadcast the cached daily features across ALL minutes of the session."""
        daily, names = self._daily(ctx)
        return self._broadcast(ctx.frame(self.minute_input), daily, names)

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LIVE form: broadcast the cached daily features onto ONLY the latest minute's rows. Value-identical
        to ``compute`` filtered to T — the daily computation and the broadcast join are the SAME; only the
        minute set shrinks from all 390 to one."""
        minutes = ctx.frame(self.minute_input).select(["symbol", "minute", *self.minute_columns])
        latest = minutes["minute"].max()
        daily, names = self._daily(ctx)
        return self._broadcast(minutes.filter(pl.col("minute") == latest), daily, names)
