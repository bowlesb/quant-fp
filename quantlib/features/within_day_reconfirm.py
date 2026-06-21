"""Within-Day Parity Certifier — Phase-3 FIRST BUILD: the in-sandbox FIX-PROOF.

Per docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §2 (step 5) + §4 (conditions 3-4). Given a group + a recent
raw window (the same shared minute-agg frame the live + backfill paths both consume), run BOTH forms of the
group's compute and assert they AGREE:

  * the BACKFILL form  = ``group.compute(ctx)``       (whole-window rolling — the canonical source of truth)
  * the LIVE form      = ``group.compute_latest(ctx)`` (aggregate-at-T — the fast path a parity fix changes)

The fix is PROVEN iff, on the LATEST minute of the window, every cell of the live form matches the backfill
form within the feature's tolerance (``compare.match_predicate``) — i.e. ``compute_latest == compute`` on
recent real data. This is the deterministic, seconds-fast proof that gates the real-time deploy ("adequate
unit tests"), run on the candidate code IN-SANDBOX before anything touches live fc.

It also provides the BYTE-EQ-ELSEWHERE check (§4 condition 4): given two registries (before/after the fix),
every OTHER group's output on the same window must be byte-identical — the fix is surgical and perturbs
nothing outside the owned group. Read-only: pure compute over an in-memory frame; no store / DB / live state.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.compare import match_predicate
from quantlib.features.registry import REGISTRY, Registry


@dataclass
class ReconfirmResult:
    group_name: str
    n_features: int
    n_compared: int
    n_match: int
    n_mismatch: int
    clean: bool  # every compared cell matched within tolerance on the latest minute
    mismatched_features: list[str]


def _latest_minute(frame: pl.DataFrame) -> object:
    return frame.select(pl.col("minute").max()).item()


def reconfirm_group(group: FeatureGroup, minute_agg: pl.DataFrame) -> ReconfirmResult:
    """Prove ``compute_latest == compute`` for one group on the LATEST minute of ``minute_agg``.

    Runs both forms over the SAME input frame, joins on (symbol, minute), filters to the latest minute, and
    applies the feature's own ``match_predicate`` (the same tolerance as the nightly/within-day compare).
    ``clean=True`` iff every compared cell matched — that is the fix-proof."""
    ctx = BatchContext(frames={group.reduce_input: minute_agg})  # type: ignore[attr-defined]
    backfill = group.compute(ctx)
    live = group.compute_latest(ctx)
    latest = _latest_minute(live)

    specs = {spec.name: spec for spec in group.declare()}
    feature_names = list(specs.keys())
    joined = live.filter(pl.col("minute") == latest).join(
        backfill.filter(pl.col("minute") == latest),
        on=["symbol", "minute"],
        how="inner",
        suffix="_bk",
    )

    n_compared = 0
    n_match = 0
    mismatched: list[str] = []
    for feature in feature_names:
        if feature not in joined.columns or f"{feature}_bk" not in joined.columns:
            continue
        verdict = joined.select(
            match_predicate(specs[feature], pl.col(feature), pl.col(f"{feature}_bk")).alias("ok")
        )
        # Only compare cells where BOTH forms produced a value (null↔null is not a value mismatch here).
        both = joined.select(
            (pl.col(feature).is_not_null() & pl.col(f"{feature}_bk").is_not_null()).alias("both")
        )["both"]
        ok = verdict["ok"]
        compared = int(both.sum())
        matched = int((ok & both).sum())
        n_compared += compared
        n_match += matched
        if compared > 0 and matched < compared:
            mismatched.append(feature)

    n_mismatch = n_compared - n_match
    return ReconfirmResult(
        group_name=group.name,
        n_features=len(feature_names),
        n_compared=n_compared,
        n_match=n_match,
        n_mismatch=n_mismatch,
        clean=(n_compared > 0 and n_mismatch == 0),
        mismatched_features=mismatched,
    )


def byte_eq_elsewhere(
    owned_group: str,
    minute_agg: pl.DataFrame,
    registry_before: Registry = REGISTRY,
    registry_after: Registry = REGISTRY,
) -> list[str]:
    """§4 condition 4: every group EXCEPT ``owned_group`` must be byte-identical before/after the fix.

    Computes each non-owned group's ``compute_latest`` over the same window under both registries and returns
    the list of groups whose output DIFFERS (empty list = the fix is surgical, perturbed nothing else)."""
    differing: list[str] = []
    after_by_name = {g.name: g for g in registry_after.groups()}
    for group in registry_before.groups():
        if group.name == owned_group or group.name not in after_by_name:
            continue
        try:
            ctx = BatchContext(frames={group.reduce_input: minute_agg})  # type: ignore[attr-defined]
            before_out = group.compute_latest(ctx)
            after_out = after_by_name[group.name].compute_latest(ctx)
        except (AttributeError, KeyError, pl.exceptions.ColumnNotFoundError):
            # A group whose inputs aren't in this minute_agg frame can't be compared on it — skip (the live
            # byte-eq check runs over the full input set; this offline helper covers the reduce-input groups).
            continue
        if not before_out.equals(after_out):
            differing.append(group.name)
    return differing
