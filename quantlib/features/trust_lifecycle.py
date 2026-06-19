"""Contamination-aware trust STATE MACHINE + the parity-defect backlog (docs/PARITY_LIFECYCLE.md).

This is the "investigate" half of the QUARANTINE-and-investigate policy. The nightly sweep
(``validation_sweep``) writes the cell-for-cell comparison the same way ``validate`` always has, then
this module:

  1. grades each feature over CLEAN (feature, symbol, day) comparisons ONLY (``cleanliness``), so a
     capture-contaminated day never condemns a correct windowed feature;
  2. derives a per-feature lifecycle STATE — PENDING / VALIDATED / DIVERGENT / RETIRED — from the clean
     history;
  3. for every DIVERGENT feature, UPSERTS an OPEN row into ``feature_parity_defect`` with the exemplar
     diverging cells (the queue the modelling-agent works).

State machine (per feature, version):
  • PENDING    — fewer than ``MIN_CLEAN_DAYS`` clean days of comparison yet (not enough signal to trust).
  • VALIDATED  — >= ``MIN_CLEAN_DAYS`` clean days AND parity held on every clean day (clean_value_rate
                 at/above ``CLEAN_PASS_RATE``). The feature's live compute reproduces backfill.
  • DIVERGENT  — parity FAILED on at least one CLEAN day. A real ``compute_latest != compute`` bug; the
                 feature is QUARANTINED (kept, marked untrusted) and a defect is filed.
  • RETIRED    — terminal, set MANUALLY (last resort). The lifecycle never auto-retires; a RETIRED row is
                 preserved (we never recompute it back to a live state).

A clean (feature, symbol, day) comparison "passes" iff its match rate over compared cells is >=
``CLEAN_PASS_RATE``. DIVERGENT wins over PENDING: a single clean-day failure files a defect even before
``MIN_CLEAN_DAYS`` is reached (we don't wait to flag a known-broken feature).

Pure functions over polars frames + a thin psycopg writer (mirrors ``validation_db``); the row builders
are unit-tested without a DB.
"""

from __future__ import annotations

import json

import polars as pl
import psycopg

from quantlib.features.validation_db import DB_KWARGS, finite_or_none

MIN_CLEAN_DAYS = 2  # clean days of parity needed to move PENDING -> VALIDATED
CLEAN_PASS_RATE = 0.999  # a clean (feature,symbol,day) "passes" parity at >= this match rate (B-grade floor)
MAX_EXEMPLARS = 10  # diverging cells stored per defect (evidence, not the full audit trail)
# Consecutive CLEAN settled sweeps a defect must grade recurrence-free before it AUTO-CLOSES. Mirrors the
# binary-trust clean-day convention (MIN_CLEAN_DAYS=2 to TRUST a feature) — symmetric: 2 clean recurrence-free
# sweeps to CLEAR its defect. Conservative enough that one fluke clean day can't close a real defect, while a
# genuinely-fixed feature self-heals quickly instead of rotting trust% (the manual-clear problem).
AUTO_CLOSE_STREAK = 2

STATE_PENDING = "PENDING"
STATE_VALIDATED = "VALIDATED"
STATE_DIVERGENT = "DIVERGENT"
STATE_RETIRED = "RETIRED"

DEFECT_STATUS_OPEN = "open"
DEFECT_STATUS_AUTO_CLOSED = "auto_closed"  # auto-resolved after AUTO_CLOSE_STREAK clean sweeps (vs manual 'fixed')


def clean_feature_day(cell: pl.DataFrame, clean_symbols: list[str], day: str) -> pl.DataFrame:
    """Per-(feature, day) CLEAN comparison rollup: aggregate the per-(feature, symbol) cell rollup over
    only the CLEAN symbols, and decide whether the feature PASSED parity on that clean day.

    ``cell`` is ``validate``'s Layer-2 frame (feature, symbol, tier, n_match, n_mismatch, ...). Restricting
    to ``clean_symbols`` is what makes the grade contamination-aware. A feature with zero clean compared
    cells on the day contributes NOTHING (it is simply not a clean observation), never a failure.
    """
    if cell.height == 0 or not clean_symbols:
        return pl.DataFrame(
            schema={
                "feature": pl.String,
                "day": pl.String,
                "clean_compared": pl.Int64,
                "clean_match": pl.Int64,
                "clean_value_rate": pl.Float64,
                "passed": pl.Boolean,
            }
        )
    scoped = cell.filter(pl.col("symbol").is_in(clean_symbols))
    agg = scoped.group_by("feature").agg(
        (pl.col("n_match") + pl.col("n_mismatch")).sum().alias("clean_compared"),
        pl.col("n_match").sum().alias("clean_match"),
    )
    value_rate = (
        pl.when(pl.col("clean_compared") > 0).then(pl.col("clean_match") / pl.col("clean_compared")).otherwise(None)
    )
    passed = (pl.col("clean_compared") > 0) & (pl.col("clean_value_rate") >= CLEAN_PASS_RATE)
    return (
        agg.with_columns(pl.lit(day).alias("day"), value_rate.alias("clean_value_rate"))
        .with_columns(passed.alias("passed"))
        .filter(pl.col("clean_compared") > 0)
        .select("feature", "day", "clean_compared", "clean_match", "clean_value_rate", "passed")
    )


def lifecycle_state(clean_history: pl.DataFrame, retired: set[str]) -> pl.DataFrame:
    """Derive the per-feature lifecycle state from the accumulated CLEAN-day history.

    ``clean_history`` is the union of ``clean_feature_day`` rows across all swept days (feature, day,
    clean_compared, clean_match, clean_value_rate, passed). ``retired`` is the set of feature names an
    operator has manually RETIRED (terminal — never recomputed back to a live state).

    Returns one row per feature: (feature, clean_days, clean_days_passed, clean_value_rate,
    lifecycle_state).
    """
    if clean_history.height == 0:
        return pl.DataFrame(
            schema={
                "feature": pl.String,
                "clean_days": pl.Int64,
                "clean_days_passed": pl.Int64,
                "clean_value_rate": pl.Float64,
                "lifecycle_state": pl.String,
            }
        )
    rolled = clean_history.group_by("feature").agg(
        pl.col("day").n_unique().alias("clean_days"),
        pl.col("passed").sum().alias("clean_days_passed"),
        pl.col("clean_compared").sum().alias("lifetime_clean_compared"),
        pl.col("clean_match").sum().alias("lifetime_clean_match"),
    )
    value_rate = (
        pl.when(pl.col("lifetime_clean_compared") > 0)
        .then(pl.col("lifetime_clean_match") / pl.col("lifetime_clean_compared"))
        .otherwise(None)
    )
    rolled = rolled.with_columns(value_rate.alias("clean_value_rate"))
    failed_any = pl.col("clean_days_passed") < pl.col("clean_days")
    enough_days = pl.col("clean_days") >= MIN_CLEAN_DAYS
    state = (
        pl.when(pl.col("feature").is_in(list(retired)))
        .then(pl.lit(STATE_RETIRED))
        .when(failed_any)
        .then(pl.lit(STATE_DIVERGENT))
        .when(enough_days)
        .then(pl.lit(STATE_VALIDATED))
        .otherwise(pl.lit(STATE_PENDING))
    )
    return (
        rolled.with_columns(state.alias("lifecycle_state"))
        .select("feature", "clean_days", "clean_days_passed", "clean_value_rate", "lifecycle_state")
        .sort("feature")
    )


def defect_rows(
    states: pl.DataFrame,
    clean_history: pl.DataFrame,
    exceptions: pl.DataFrame,
    group_of: dict[str, str],
    version_of: dict[str, str],
) -> list[tuple]:
    """Build the parity-defect backlog rows for every DIVERGENT feature.

    For each DIVERGENT feature: first/last CLEAN day it failed, how many clean days failed, the worst
    relative error, and up to ``MAX_EXEMPLARS`` exemplar diverging cells pulled from ``exceptions``
    (feature_validation_exception). ``exceptions`` carries (feature, symbol, minute|ts, stream_value,
    backfill_value, rel_err) — the rare diverging cells the sweep recorded.
    """
    divergent = states.filter(pl.col("lifecycle_state") == STATE_DIVERGENT)["feature"].to_list()
    if not divergent:
        return []
    failed = clean_history.filter(~pl.col("passed"))
    ts_col = "minute" if "minute" in exceptions.columns else "ts"
    rows: list[tuple] = []
    for feature in divergent:
        feat_failed = failed.filter(pl.col("feature") == feature)
        if feat_failed.height == 0:
            continue
        first_seen = feat_failed["day"].min()
        last_seen = feat_failed["day"].max()
        clean_days_failed = feat_failed["day"].n_unique()
        feat_exc = (
            exceptions.filter(pl.col("feature") == feature).sort("rel_err", descending=True)
            if exceptions.height
            else exceptions
        )
        worst_rel_err = finite_or_none(feat_exc["rel_err"].max()) if feat_exc.height else None
        # NULL non-finite cell values (Infinity/-Infinity/NaN) before json.dumps — json.dumps(inf) emits
        # the bare token "Infinity", which the exemplars jsonb column rejects (it is invalid JSON). The
        # non-finite value is still RECORDED as a diverging exemplar, just with a null value.
        exemplars = [
            {
                "symbol": row["symbol"],
                "ts": str(row[ts_col]),
                "stream_value": finite_or_none(row["stream_value"]),
                "backfill_value": finite_or_none(row["backfill_value"]),
                "rel_err": finite_or_none(row["rel_err"]),
            }
            for row in feat_exc.head(MAX_EXEMPLARS).to_dicts()
        ]
        rows.append(
            (
                feature,
                version_of[feature],
                group_of[feature],
                first_seen,
                last_seen,
                clean_days_failed,
                worst_rel_err,
                json.dumps(exemplars),
            )
        )
    return rows


def auto_close_updates(
    open_defects: list[tuple[str, str, int, str | None]],
    graded_clean: set[str],
    recurred: set[str],
    day: str,
    streak_target: int = AUTO_CLOSE_STREAK,
) -> list[tuple[str, str, int, str, str]]:
    """Decide the AUTO-CLOSE streak transition for each currently-OPEN defect from ONE clean settled sweep.

    Pure: no DB. ``open_defects`` is ``(feature, version, clean_streak, last_streak_day)`` for every defect
    currently status='open' (``last_streak_day`` is the clean day that last advanced the streak, or None).
    ``graded_clean`` is the set of features the sweep GRADED CLEAN this ``day`` (present in the clean-day
    history and recurrence-free — i.e. NOT in ``recurred``). ``recurred`` is the set of features that
    re-failed parity on a clean symbol-day (the day's DIVERGENT set). Returns the rows to write back:
    ``(feature, version, new_clean_streak, new_status, day)``.

    Rules (conservative — only a CLEAN-GRADED, recurrence-free, NOT-already-counted observation moves it):
      • recurred today                         -> handled by the defect UPSERT (streak reset to 0, re-opened);
                                                  NOT returned here (no double-write).
      • last_streak_day == day                 -> this clean day ALREADY advanced the streak; re-running the
                                                  same sweep is a no-op (per-DAY idempotency, not per-invocation).
      • graded clean & recurrence-free today   -> streak += 1; at ``streak_target`` flip 'open'->'auto_closed'.
      • NOT graded this day (contaminated /     -> NO change. The feature wasn't observed on a clean day, so the
        skipped / fragmented-xsec / absent)       streak neither advances nor resets. This is what prevents a
                                                  contaminated or skipped day from counting as a clean recurrence-
                                                  free day.
    """
    updates: list[tuple[str, str, int, str, str]] = []
    for feature, version, current_streak, last_streak_day in open_defects:
        if feature in recurred:
            continue  # the defect upsert already reset+reopened this one; don't double-write
        if feature not in graded_clean:
            continue  # not observed on a clean day -> streak untouched (the contamination guard)
        if last_streak_day is not None and str(last_streak_day) == day:
            continue  # this exact clean day already counted -> re-run is idempotent
        new_streak = current_streak + 1
        new_status = DEFECT_STATUS_AUTO_CLOSED if new_streak >= streak_target else DEFECT_STATUS_OPEN
        updates.append((feature, version, new_streak, new_status, day))
    return updates


_UPSERT_TRUST_LIFECYCLE = """
UPDATE feature_trust SET
  lifecycle_state=%s, clean_days=%s, clean_days_passed=%s, clean_value_rate=%s,
  lifecycle_updated_at=now()
WHERE feature=%s AND version=%s
"""

_UPSERT_DEFECT = """
INSERT INTO feature_parity_defect
  (feature,version,feature_group,status,first_seen_day,last_seen_day,clean_days_failed,worst_rel_err,exemplars)
VALUES (%s,%s,%s,'open',%s,%s,%s,%s,%s)
ON CONFLICT (feature,version) DO UPDATE SET
  feature_group=EXCLUDED.feature_group,
  first_seen_day=LEAST(feature_parity_defect.first_seen_day, EXCLUDED.first_seen_day),
  last_seen_day=GREATEST(feature_parity_defect.last_seen_day, EXCLUDED.last_seen_day),
  clean_days_failed=EXCLUDED.clean_days_failed,
  worst_rel_err=EXCLUDED.worst_rel_err,
  exemplars=EXCLUDED.exemplars,
  status=CASE WHEN feature_parity_defect.status IN ('fixed','wontfix','auto_closed')
              THEN 'open' ELSE feature_parity_defect.status END,
  clean_streak=0,
  last_streak_day=NULL,
  updated_at=now()
"""

_UPSERT_CLEANLINESS = """
INSERT INTO stream_symbol_day_cleanliness
  (symbol,day,n_stream_minutes,n_backfill_minutes,coverage_frac,max_gap_minutes,is_clean,reason)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (symbol,day) DO UPDATE SET
  n_stream_minutes=EXCLUDED.n_stream_minutes, n_backfill_minutes=EXCLUDED.n_backfill_minutes,
  coverage_frac=EXCLUDED.coverage_frac, max_gap_minutes=EXCLUDED.max_gap_minutes,
  is_clean=EXCLUDED.is_clean, reason=EXCLUDED.reason, recorded_at=now()
"""


def _trust_lifecycle_rows(states: pl.DataFrame, version_of: dict[str, str]) -> list[tuple]:
    """UPDATE-tuples for feature_trust's lifecycle columns (state first, then the WHERE keys)."""
    rows: list[tuple] = []
    for row in states.to_dicts():
        feature = row["feature"]
        rows.append(
            (
                row["lifecycle_state"],
                row["clean_days"],
                row["clean_days_passed"],
                row["clean_value_rate"],
                feature,
                version_of[feature],
            )
        )
    return rows


def _cleanliness_rows(cleanliness: pl.DataFrame, day: str) -> list[tuple]:
    if cleanliness.height == 0:
        return []
    return [
        (
            row["symbol"],
            day,
            int(row["n_stream_minutes"]),
            int(row["n_backfill_minutes"]),
            row["coverage_frac"],
            int(row["max_gap_minutes"]),
            bool(row["is_clean"]),
            row["reason"],
        )
        for row in cleanliness.to_dicts()
    ]


def retired_features() -> set[str]:
    """Features an operator has manually RETIRED (terminal) — read so the recompute never resurrects them."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT feature FROM feature_trust WHERE lifecycle_state = %s", (STATE_RETIRED,))
        return {row[0] for row in cur.fetchall()}


def write_lifecycle(
    states: pl.DataFrame,
    defects: list[tuple],
    cleanliness: pl.DataFrame,
    version_of: dict[str, str],
    day: str,
) -> None:
    """Persist the lifecycle: update feature_trust's lifecycle columns, upsert the defect backlog, and
    record the day's per-symbol cleanliness. Idempotent — re-running a day refreshes in place."""
    trust_rows = _trust_lifecycle_rows(states, version_of)
    clean_rows = _cleanliness_rows(cleanliness, day)
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        if trust_rows:
            cur.executemany(_UPSERT_TRUST_LIFECYCLE, trust_rows)
        if defects:
            cur.executemany(_UPSERT_DEFECT, defects)
        if clean_rows:
            cur.executemany(_UPSERT_CLEANLINESS, clean_rows)
        conn.commit()


_SELECT_OPEN_DEFECTS = (
    "SELECT feature, version, clean_streak, last_streak_day FROM feature_parity_defect WHERE status = 'open'"
)

_APPLY_AUTO_CLOSE = """
UPDATE feature_parity_defect SET clean_streak=%s, status=%s, last_streak_day=%s, updated_at=now()
WHERE feature=%s AND version=%s AND status='open'
"""


def apply_auto_close(graded_clean: set[str], recurred: set[str], day: str) -> dict[str, int]:
    """Advance the AUTO-CLOSE streak for the OPEN defects this CLEAN settled sweep graded recurrence-free,
    flipping any that reached ``AUTO_CLOSE_STREAK`` to 'auto_closed'. Call AFTER ``write_lifecycle`` so the
    open set already reflects today's recurrences (the defect upsert reset their streak + re-opened them).

    Idempotent PER DAY: the streak counts distinct clean DAYS, not sweep invocations. Re-running the same
    ``day`` is a no-op (``last_streak_day == day`` short-circuits in ``auto_close_updates``), so an operator
    re-sweeping a day can never double-advance a streak. ``graded_clean`` MUST already exclude ``recurred``
    (the caller passes the clean-graded, recurrence-free set); both are passed so the pure
    ``auto_close_updates`` enforces the guard explicitly.

    Returns a small summary {advanced, auto_closed} for logging. A day that graded nothing clean (too
    contaminated / fragmented) yields no open defects in ``graded_clean`` -> no streak moves at all.
    """
    if not graded_clean:
        return {"advanced": 0, "auto_closed": 0}
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_SELECT_OPEN_DEFECTS)
        open_defects = [
            (feature, version, int(streak), str(last_day) if last_day is not None else None)
            for feature, version, streak, last_day in cur.fetchall()
        ]
        updates = auto_close_updates(open_defects, graded_clean, recurred, day)
        if updates:
            cur.executemany(
                _APPLY_AUTO_CLOSE,
                [
                    (streak, status, streak_day, feature, version)
                    for feature, version, streak, status, streak_day in updates
                ],
            )
        conn.commit()
    auto_closed = sum(1 for _f, _v, _streak, status, _day in updates if status == DEFECT_STATUS_AUTO_CLOSED)
    return {"advanced": len(updates), "auto_closed": auto_closed}


def trusted_feature_names(min_state: str = STATE_VALIDATED) -> set[str]:
    """The set of feature names downstream consumers (bus, ML, strategies) may TRUST — currently
    VALIDATED. A consumer intersects its requested features with this set and excludes/flags the rest
    (PENDING = not proven yet; DIVERGENT/RETIRED = quarantined). An empty/missing table yields the empty
    set (nothing trusted until proven)."""
    allowed = {STATE_VALIDATED} if min_state == STATE_VALIDATED else {STATE_VALIDATED, STATE_PENDING}
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT feature FROM feature_trust WHERE lifecycle_state = ANY(%s)", (list(allowed),))
        return {row[0] for row in cur.fetchall()}


def feature_trust_grades() -> dict[str, str]:
    """Current lifecycle grade per feature, for a consumer that wants to FLAG (not exclude) untrusted
    features. Maps feature -> lifecycle_state. Features with no lifecycle row are absent (treat as
    PENDING by the caller)."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT feature, lifecycle_state FROM feature_trust WHERE lifecycle_state IS NOT NULL")
        return {feature: state for feature, state in cur.fetchall()}
