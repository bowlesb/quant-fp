"""Within-Day Parity Certifier — PHASE 3: the continuous real-time monitor loop.

This is the connective tissue between the already-built primitives (the piece that makes the certification
lifecycle actually RUN). A subagent owns ONE group via the assignment lock, then each cycle compares that
group's live==backfill on the freshly-settled window (phase-1 :func:`within_day_parity.compare_window`).
Stability is tracked PER FEATURE: a feature at/above its ``min_pass_rate`` this cycle advances its own clean
streak, a mismatch resets only that feature's streak. The instant a feature's streak holds for
``--stable-cycles`` consecutive clean cycles — the within-day "it's matched for a while" bar — that feature
stamps ``within_day_parity_cert`` (status='certified' = "intraday-OK, pending the full-day nightly sweep")
via phase-2 :func:`within_day_trust.write_certifications`, INDEPENDENTLY of any divergent sibling in the same
group. A parity-clean feature is no longer held hostage by an irreducibly-divergent long-window sibling: each
feature earns trust on its own evidence. The group is certified once every comparable feature has been
granted; the lock releases then (or at ``max_cycles``, keeping whatever features already granted). The
nightly sweep + ``MIN_CLEAN_DAYS=2`` carry each granted feature the rest of the way to binary trust — so a
within-day cert is the PROVISIONAL intraday stage, not the final grant.

Modes:
  * ``--mode live`` (default): the window ends ``settle_lag`` before wall-clock now; one cycle per
    ``--poll-seconds``. A cycle that finds no backfill cells counts as 'unsettled' (no streak progress),
    NOT a mismatch (the settled-window backfill must be materialized for a live compare).
  * ``--mode replay --day D``: step the SAME compare across a past, fully-settled day — one window per
    cycle, walked forward through the session — to demonstrate the whole lifecycle on REAL cells without
    waiting wall-clock minutes (identical code path, time-compressed). The pre-Monday in-action proof.

Resource discipline mirrors phases 1/2 (one group, a symbol sample, the settled window). The assignment lock
and the cert/trust writes default to ``dry_run`` — live granting is the Lead's gated step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time
from typing import Protocol

import polars as pl

from quantlib.features import within_day_assignment
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_binary import feature_policy_map
from quantlib.features.within_day_materialize import DEFAULT_RAW_ROOT, materialize_settled_window
from quantlib.features.within_day_parity import (
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_WINDOW_MINUTES,
    compare_window,
    sample_symbols,
    settle_lag_for_group,
    settled_window,
)
from quantlib.features.within_day_trust import CertResult, certify_result_from_summary, write_certifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("within_day_monitor")

DEFAULT_STABLE_CYCLES = (
    20  # consecutive clean cycles required to certify (≈ the within-day "matched a while")
)
DEFAULT_POLL_SECONDS = 60


class MaterializeFn(Protocol):
    """The injectable per-cycle backfill-materialization hook (the equity ``/store/raw`` recompute by default;
    the crypto canary injects a no-raw batch recompute). Returns the symbol count materialized."""

    def __call__(
        self,
        feature_root: str,
        group_name: str,
        cert_day: dt.date,
        symbols: list[str],
        *,
        raw_root: str,
        ensure_inputs_first: bool,
        agent_id: str,
        dry_run: bool,
    ) -> int: ...


def evaluate_summary(
    summary: pl.DataFrame,
    group_name: str,
    cert_day: dt.date,
    stable_cycles: int,
    window_minutes: int,
    settle_lag_min: float,
) -> tuple[bool, list[CertResult]]:
    """PURE: turn one phase-1 per-feature compare summary into (cycle_is_clean, per-feature CertResults).

    A cycle is CLEAN iff there is at least one comparable feature and EVERY feature is at/above its policy
    ``min_pass_rate`` (deterministic features must match exactly). ``stable_cycles`` is carried onto each
    CertResult as the evidence the certify call will persist. No DB, no store — unit-testable offline.

    A feature with ``n_compared == 0`` is NOT comparable this cycle (live cells exist but the backfill side
    has no overlapping settled cell yet, or vice-versa) — it is SKIPPED, neither certified nor counted as a
    defect. A coverage gap is not a divergence: it must not reset the stability streak. When NO feature is
    comparable the result is empty (the monitor holds the streak, same as ``summary.height == 0``)."""
    if summary.height == 0:
        return False, []
    policy_of = feature_policy_map()
    results: list[CertResult] = []
    clean = True
    for row in summary.iter_rows(named=True):
        feature = row["feature"]
        if feature not in policy_of:
            continue
        if int(row["n_compared"]) == 0:
            continue
        _version, pol = policy_of[feature]
        min_pass_rate = 1.0 if pol.deterministic else pol.min_pass_rate
        result = certify_result_from_summary(
            feature=feature,
            group_name=group_name,
            cert_day=cert_day,
            value_rate=row["value_rate"],
            n_compared=int(row["n_compared"]),
            n_clean_symbols=0,
            stable_cycles=stable_cycles,
            window_minutes=window_minutes,
            settle_lag_min=settle_lag_min,
            min_pass_rate=min_pass_rate,
        )
        results.append(result)
        if result.status != "certified":
            clean = False
    if not results:
        return False, []
    return clean, results


def compare_is_clean(
    feature_root: str,
    group_name: str,
    *,
    sample_symbols: list[str],
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    materialize_backfill: bool = False,
    raw_root: str = DEFAULT_RAW_ROOT,
    day: dt.date | None = None,
) -> bool:
    """ONE settled-window phase-1 compare → cycle-clean bool (no streak). The post-swap TRIPWIRE: the live
    hot-swap applier calls this to confirm the just-swapped group's live stream == backfill on the current
    settled window (the SAME read the monitor streak + nightly sweep use, so 'confirmed' means the same thing
    everywhere). Returns True iff there is at least one comparable feature and every feature clears its policy
    ``min_pass_rate`` (:func:`evaluate_summary`). ``materialize_backfill`` recomputes the settled-window
    backfill side on demand for the live-intraday case (the current day is not pre-materialized).

    Distinct from :func:`monitor`: no claim/lock, no cert write, no streak — a single point-in-time confirm
    the applier composes. Read-only except the optional on-demand materialize (its own dry_run)."""
    cert_day = day or dt.datetime.now(dt.timezone.utc).date()
    if not sample_symbols:
        return False
    lag = settle_lag_for_group(group_name)
    window_start, window_end = settled_window(dt.datetime.now(dt.timezone.utc), lag, window_minutes)
    if materialize_backfill:
        _default_materialize(
            feature_root,
            group_name,
            cert_day,
            sample_symbols,
            raw_root=raw_root,
            ensure_inputs_first=False,
            agent_id="wdpc-tripwire",
            dry_run=False,
        )
    summary = compare_window(
        feature_root, group_name, cert_day, sample_symbols, window_start, window_end
    )
    clean, results = evaluate_summary(summary, group_name, cert_day, 1, window_minutes, lag)
    return clean and bool(results)


def _replay_windows(
    day: dt.date, window_minutes: int, n_windows: int
) -> list[tuple[dt.datetime, dt.datetime]]:
    """A series of ``n_windows`` consecutive settled RTH windows walked forward through a past session,
    each ``window_minutes`` long, ending by ~15:30 ET — distinct real windows so the streak is meaningful."""
    band_end = dt.datetime.combine(day, dt.time(19, 30), tzinfo=dt.timezone.utc)
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    end = band_end
    for _ in range(n_windows):
        start = end - dt.timedelta(minutes=window_minutes)
        windows.append((start, end))
        end = start  # walk backward in time; the set covers a contiguous pre-15:30 band
    return list(reversed(windows))


def _default_materialize(
    feature_root: str,
    group_name: str,
    cert_day: dt.date,
    symbols: list[str],
    *,
    raw_root: str,
    ensure_inputs_first: bool,
    agent_id: str,
    dry_run: bool,
) -> int:
    """The default live-intraday backfill materialization hook: recompute the group's settled-window backfill
    side from ``/store/raw`` so the intraday compare has a backfill side (it is not pre-materialized on the
    current day). Injectable (``materialize_fn``) so the crypto canary swaps in its no-raw batch recompute.
    """
    return materialize_settled_window(
        feature_root,
        raw_root,
        group_name,
        cert_day,
        symbols,
        ensure_inputs_first=ensure_inputs_first,
        agent_id=agent_id,
        dry_run=dry_run,
    )


def monitor(
    feature_root: str,
    group_name: str,
    agent_id: str,
    *,
    mode: str = "live",
    day: dt.date | None = None,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    stable_cycles_required: int = DEFAULT_STABLE_CYCLES,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    materialize_backfill: bool = False,
    raw_root: str = DEFAULT_RAW_ROOT,
    ensure_inputs_first: bool = False,
    materialize_fn: MaterializeFn | None = None,
    dry_run_cert: bool = True,
    dry_run_lock: bool = True,
    claim_lock: bool = True,
    max_cycles: int | None = None,
) -> CertResult | None:
    """Run the per-group monitor until every comparable feature has granted (or ``max_cycles`` is hit).
    Grants each feature INDEPENDENTLY as its own clean streak ripens. Returns the first granted feature's
    CertResult (None if nothing granted). The subagent's whole within-day job is this one call.

    ``materialize_backfill`` opts into the LIVE-INTRADAY path: before each compare, materialize the group's
    settled-window backfill side on demand (the current day's backfill is NOT pre-materialized — only swept
    days are) via ``materialize_fn`` (default :func:`_default_materialize`, reading ``/store/raw``; the
    crypto canary injects its no-raw batch recompute). ``ensure_inputs_first`` patches raw holes (#74) before
    the materialize. ``claim_lock`` is False when an outer orchestrator (:mod:`within_day_run`) already holds
    the lock — the monitor then only heartbeats/releases, never re-claims (a second claim would fail)."""
    REGISTRY.get_group(group_name)  # fail fast on a bad group name
    now_utc = dt.datetime.now(dt.timezone.utc)
    cert_day = day or now_utc.date()
    lag = settle_lag_for_group(group_name)
    do_materialize = materialize_fn or _default_materialize

    if claim_lock and not within_day_assignment.claim(group_name, agent_id, dry_run=dry_run_lock):
        logger.warning("could not claim group=%s (held by another agent) — aborting", group_name)
        return None
    logger.info(
        "MONITOR start group=%s agent=%s mode=%s day=%s settle_lag=%.0fmin need=%d clean cycles "
        "materialize_backfill=%s",
        group_name,
        agent_id,
        mode,
        cert_day,
        lag,
        stable_cycles_required,
        materialize_backfill,
    )

    replay_windows = (
        _replay_windows(cert_day, window_minutes, stable_cycles_required) if mode == "replay" else None
    )
    # Per-FEATURE stability: each feature earns its grant on its OWN clean streak, independent of a divergent
    # sibling in the same group. A long-window feature that irreducibly diverges (live-warmup vs backfill
    # bar-revision) no longer holds its parity-clean siblings hostage — each feature still must independently
    # clear its policy min_pass_rate for ``stable_cycles_required`` consecutive cycles. The streak resets per
    # feature on that feature's own mismatch; granted features are written once and never re-written.
    feature_streaks: dict[str, int] = {}
    granted: dict[str, CertResult] = {}
    first_granted: CertResult | None = None
    cycle = 0
    try:
        while True:
            cycle += 1
            within_day_assignment.heartbeat(group_name, agent_id, dry_run=dry_run_lock)
            symbols = sample_symbols(feature_root, cert_day, sample_size)
            if mode == "replay":
                assert replay_windows is not None  # mode=='replay' builds it above
                window_start, window_end = replay_windows[(cycle - 1) % len(replay_windows)]
            else:
                window_start, window_end = settled_window(
                    dt.datetime.now(dt.timezone.utc), lag, window_minutes
                )
            if materialize_backfill and symbols:
                do_materialize(
                    feature_root,
                    group_name,
                    cert_day,
                    symbols,
                    raw_root=raw_root,
                    ensure_inputs_first=ensure_inputs_first,
                    agent_id=agent_id,
                    dry_run=dry_run_cert,
                )
            summary = (
                compare_window(feature_root, group_name, cert_day, symbols, window_start, window_end)
                if symbols
                else pl.DataFrame()
            )
            _clean, results = evaluate_summary(summary, group_name, cert_day, 1, window_minutes, lag)
            if not results:
                logger.info(
                    "cycle %d: no comparable cells (unsettled / capture gap / backfill not yet "
                    "materialized) — per-feature streaks held",
                    cycle,
                )
            else:
                ripe: list[CertResult] = []
                for result in results:
                    feature = result.feature
                    if feature in granted:
                        continue
                    if result.status == "certified":
                        streak = feature_streaks.get(feature, 0) + 1
                        feature_streaks[feature] = streak
                        if streak >= stable_cycles_required:
                            ripe.append(CertResult(**{**result.__dict__, "stable_cycles": streak}))
                    else:
                        feature_streaks[feature] = 0
                passing = sum(1 for streak in feature_streaks.values() if streak > 0)
                failing = [r.feature for r in results if r.status != "certified" and r.feature not in granted]
                logger.info(
                    "cycle %d: %d/%d comparable features on a clean streak; %d granted; %d still failing %s",
                    cycle,
                    passing,
                    len(results),
                    len(granted),
                    len(failing),
                    failing[:5],
                )
                if ripe:
                    write_certifications(ripe, dry_run=dry_run_cert)
                    for result in ripe:
                        granted[result.feature] = result
                        first_granted = first_granted or result
                    logger.info(
                        "group=%s GRANTED %d feature(s) at streak %d (%d total granted): %s",
                        group_name,
                        len(ripe),
                        stable_cycles_required,
                        len(granted),
                        [r.feature for r in ripe],
                    )

            # The group is fully certified once every comparable feature has been granted. ``results`` is the
            # current comparable set; if all of them are in ``granted`` (and at least one exists), we are done.
            if results and all(r.feature in granted for r in results):
                logger.info(
                    "group=%s CERTIFIED (all %d comparable features granted, intraday-OK pending full-day "
                    "sweep)",
                    group_name,
                    len(granted),
                )
                within_day_assignment.release(group_name, agent_id, dry_run=dry_run_lock)
                return first_granted

            if max_cycles is not None and cycle >= max_cycles:
                logger.info(
                    "group=%s reached max_cycles=%d — %d feature(s) granted, %d still un-certified",
                    group_name,
                    max_cycles,
                    len(granted),
                    sum(1 for streak in feature_streaks.values() if streak < stable_cycles_required),
                )
                if granted:
                    within_day_assignment.release(group_name, agent_id, dry_run=dry_run_lock)
                return first_granted
            if mode == "live":
                time.sleep(poll_seconds)
    finally:
        within_day_assignment.heartbeat(group_name, agent_id, dry_run=dry_run_lock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-root", default="/store")
    parser.add_argument("--group", required=True, help="the single feature group to monitor")
    parser.add_argument("--agent-id", required=True, help="the owning subagent's id (assignment lock)")
    parser.add_argument("--mode", choices=["live", "replay"], default="live")
    parser.add_argument("--day", default=None, help="YYYY-MM-DD (replay day, or live cert day)")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--stable-cycles", type=int, default=DEFAULT_STABLE_CYCLES)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument(
        "--raw-root", default=DEFAULT_RAW_ROOT, help="the /store/raw root for the materialize"
    )
    parser.add_argument(
        "--materialize-backfill",
        action="store_true",
        help="LIVE-INTRADAY: materialize the settled window from raw before each compare (else swept-day only)",
    )
    parser.add_argument(
        "--ensure-inputs-first",
        action="store_true",
        help="patch raw holes (#74 ensure_inputs) before each materialize (requires --materialize-backfill)",
    )
    parser.add_argument("--write-cert", action="store_true", help="LIVE: write cert rows (default dry-run)")
    parser.add_argument("--write-lock", action="store_true", help="LIVE: take the assignment lock in DB")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    day = dt.date.fromisoformat(args.day) if args.day else None
    monitor(
        args.feature_root,
        args.group,
        args.agent_id,
        mode=args.mode,
        day=day,
        poll_seconds=args.poll_seconds,
        stable_cycles_required=args.stable_cycles,
        window_minutes=args.window_minutes,
        sample_size=args.sample_size,
        materialize_backfill=args.materialize_backfill,
        raw_root=args.raw_root,
        ensure_inputs_first=args.ensure_inputs_first,
        dry_run_cert=not args.write_cert,
        dry_run_lock=not args.write_lock,
        max_cycles=args.max_cycles,
    )


if __name__ == "__main__":
    main()
