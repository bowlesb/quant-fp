"""Consolidated per-minute emit for the batch "rest" groups (a SCHEDULING change, not a math change).

Each batch group's ``compute_latest`` builds its OWN polars frame per minute (filter to the latest
minute, select the key/value columns, run its expressions) — for ~30 groups that per-group frame-build
+ join dominates the full-flow latency, not the arithmetic. These two emitters pay that overhead ONCE
per minute for a family of groups that share an index, then slice the wide result back into the
per-group output frames. The math is byte-identical by construction: each group exposes its column
expressions (``exprs()``) and the consolidated pass applies the SAME expressions on a shared frame.

  * POINT-IN-TIME groups (calendar / calendar_events / sector / asset_flags / round_levels) are pure
    functions of the minute (or a static per-symbol reference attribute). They share the latest
    minute's ``(symbol, minute, close)`` index plus a single ``reference`` join, computed once.
  * DAILY-BROADCAST groups (multi_day_returns / multi_day_vwap / prior_day) each read a per-symbol
    daily-cache value (fixed intraday) and broadcast it across the minute. The three per-(symbol, date)
    daily frames are merged and the latest minute is broadcast-JOINED to them ONCE, not three times.

Both emitters return ``{group_name: frame}`` keyed by (symbol, minute) — the exact shape the per-group
``compute_latest`` returns, so the caller writes them identically.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.groups.asset_flags import AssetFlagsGroup
from quantlib.features.groups.calendar import CalendarGroup
from quantlib.features.groups.calendar_events import CalendarEventsGroup
from quantlib.features.groups.multi_day import MultiDayReturnGroup
from quantlib.features.groups.multi_day_vwap import MultiDayVwapGroup
from quantlib.features.groups.prior_day import PriorDayGroup
from quantlib.features.groups.round_levels import RoundLevelsGroup
from quantlib.features.groups.sector import SectorOneHotGroup

POINT_IN_TIME_GROUPS: tuple[str, ...] = (
    "calendar",
    "calendar_events",
    "sector",
    "asset_flags",
    "round_levels",
)
DAILY_BROADCAST_GROUPS: tuple[str, ...] = (
    "multi_day_returns",
    "multi_day_vwap",
    "prior_day",
)

# Merged per-(symbol, date) daily frame, cached on the daily-snapshot identity. The daily snapshot is
# fixed for the whole trading day, so merging the three groups' daily frames is done ONCE per session,
# not per minute — the per-minute cost is then a single broadcast-join of the latest minute against it.
_MERGED_DAILY_CACHE: tuple[int, frozenset[str], pl.DataFrame, dict[str, list[str]]] | None = None


def emit_point_in_time(
    groups: list[FeatureGroup], ctx: BatchContext
) -> dict[str, pl.DataFrame]:
    """Compute the point-in-time groups in ONE shared pass over the latest minute's (symbol, minute,
    close) index (+ a single reference join), then slice the wide frame into per-group output frames.

    ``groups`` is the subset of ``POINT_IN_TIME_GROUPS`` that are runnable this minute (inputs present);
    any group not present is simply not emitted. The shared frame carries every column the groups'
    expressions read: ``close`` (round_levels), the normalized ``_norm`` sector column (sector), and the
    raw asset flag columns (asset_flags). calendar / calendar_events read only ``minute``."""
    by_name = {group.name: group for group in groups}
    minute_agg = ctx.frame("minute_agg")
    latest = minute_agg["minute"].max()
    shared = minute_agg.filter(pl.col("minute") == latest).select(["symbol", "minute", "close"])

    sector_group = by_name.get("sector")
    if isinstance(sector_group, SectorOneHotGroup):
        shared = shared.join(sector_group.reference_norm(ctx), on="symbol", how="left")
    flags_group = by_name.get("asset_flags")
    if isinstance(flags_group, AssetFlagsGroup):
        shared = shared.join(flags_group.reference_flags(ctx), on="symbol", how="left")

    all_exprs: list[pl.Expr] = []
    for group in groups:
        assert isinstance(
            group, (CalendarGroup, CalendarEventsGroup, SectorOneHotGroup, AssetFlagsGroup, RoundLevelsGroup)
        )
        all_exprs.extend(group.exprs())
    wide = shared.with_columns(all_exprs)

    outputs: dict[str, pl.DataFrame] = {}
    for group in groups:
        names = group.feature_names
        outputs[group.name] = wide.select(["symbol", "minute", *names])
    return outputs


def emit_daily_broadcast(
    groups: list[FeatureGroup], ctx: BatchContext
) -> dict[str, pl.DataFrame]:
    """Compute the daily-broadcast groups with ONE broadcast-join per minute instead of three.

    Each group's per-(symbol, date) daily frame is fixed intraday (cached on the daily-snapshot
    identity), so the only per-minute work is the broadcast of those daily values onto the latest
    minute's rows. The three daily frames are merged on (symbol, date) and the latest minute is joined
    to the merged frame ONCE; prior_day's close-relative distances (which depend on the minute's close)
    are then applied on that single joined frame. The result is sliced back per group, byte-identical to
    each group's ``compute_latest``."""
    by_name = {group.name: group for group in groups}
    merged, broadcast_names = _merged_daily(by_name, ctx)

    minute_agg = ctx.frame("minute_agg")
    latest = minute_agg["minute"].max()
    minutes = minute_agg.filter(pl.col("minute") == latest).select(["symbol", "minute", "close"]).with_columns(
        pl.col("minute").dt.date().alias("date")
    )
    joined = minutes.join(merged, on=["symbol", "date"], how="left")

    outputs: dict[str, pl.DataFrame] = {}
    for name in ("multi_day_returns", "multi_day_vwap"):
        if name in broadcast_names:
            outputs[name] = joined.select(["symbol", "minute", *broadcast_names[name]])
    if "prior_day" in by_name:
        prior_group = by_name["prior_day"]
        assert isinstance(prior_group, PriorDayGroup)
        names = prior_group.feature_names
        outputs["prior_day"] = joined.with_columns(prior_group.broadcast_exprs()).select(["symbol", "minute", *names])
    return outputs


def _merged_daily(
    by_name: dict[str, FeatureGroup], ctx: BatchContext
) -> tuple[pl.DataFrame, dict[str, list[str]]]:
    """The three daily-broadcast groups' per-(symbol, date) daily frames merged into ONE frame on
    (symbol, date), cached on the daily-snapshot identity (fixed all day). All three derive from the
    SAME ``daily`` source frame, so they carry identical (symbol, date) keys — a left join from the
    first frame is therefore complete (and far cheaper than a full outer merge over the whole daily
    history every minute). Returns the merged frame and, for the pure-broadcast groups, their column
    names; prior_day's columns are the raw level columns its ``exprs()`` consumes (applied per minute)."""
    global _MERGED_DAILY_CACHE
    source = ctx.frame("daily")
    present = frozenset(name for name in DAILY_BROADCAST_GROUPS if name in by_name)
    cached = _MERGED_DAILY_CACHE
    if cached is not None and cached[0] == id(source) and cached[1] == present:
        return cached[2], cached[3]

    merged: pl.DataFrame | None = None
    broadcast_names: dict[str, list[str]] = {}
    returns_group = by_name.get("multi_day_returns")
    if isinstance(returns_group, MultiDayReturnGroup):
        daily, names = returns_group._daily(ctx)
        broadcast_names["multi_day_returns"] = names
        merged = daily if merged is None else merged.join(daily, on=["symbol", "date"], how="left")
    vwap_group = by_name.get("multi_day_vwap")
    if isinstance(vwap_group, MultiDayVwapGroup):
        daily, names = vwap_group._daily(ctx)
        broadcast_names["multi_day_vwap"] = names
        merged = daily if merged is None else merged.join(daily, on=["symbol", "date"], how="left")
    pd_group = by_name.get("prior_day")
    if isinstance(pd_group, PriorDayGroup):
        # prior_day is an A.2 DailySnapshotGroup: its cached snapshot frame holds the per-(symbol, date)
        # LEVELS (gap / prior H/L/C / pivots), and the close-relative features are applied per minute via
        # ``broadcast_exprs`` after the broadcast join. ``_daily`` returns the cached levels frame.
        levels, _ = pd_group._daily(ctx)
        merged = levels if merged is None else merged.join(levels, on=["symbol", "date"], how="left")

    assert merged is not None
    _MERGED_DAILY_CACHE = (id(source), present, merged, broadcast_names)
    return merged, broadcast_names
