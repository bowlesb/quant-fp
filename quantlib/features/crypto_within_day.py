"""Within-Day Parity Certifier — THE CRYPTO CANARY (the live in-action proof, 24/7, no equity hours).

Ben's lifecycle ask needs an IN-ACTION proof BEFORE Monday's equity open. Crypto-capture runs 24/7
(``FP_BUS=1``, the ``/store/crypto`` volume), so a crypto group is the same-box canary for the whole
within-day cert loop — a real live-emit-vs-batch-recompute certification cycle running NOW, not idle-waiting
for the equity session (the same reasoning as the crypto parity REHEARSAL of the nightly sweep).

This module is the crypto seam of the within-day monitor — it reuses the equity streak/decision logic
(:func:`within_day_monitor.evaluate_summary`) and the cert/trust write split, replacing ONLY the two
equity-specific seams the crypto sweep already taught us to swap (docs/CRYPTO_E2E.md):

  * **The compare window.** Crypto trades 24/7, so there is no ``rth_mask`` and no NYSE settle gate; we
    compare over a contiguous band of recently-settled UTC minutes (a small lag for the minute to finish),
    over ALL minutes (no RTH filter) — :func:`compare_crypto_window`, the 24/7 analogue of phase-1
    :func:`within_day_parity.compare_window`.
  * **The backfill source.** Crypto has NO ``/store/raw`` tape, so the backfill side is RECOMPUTED from the
    persisted ``minute_agg`` (+ ``trades``) inputs the live feed delivered (``crypto_input_store``), via the
    IDENTICAL batch ``_write_all`` path the equity backfill uses (:func:`materialize_crypto_window`). This is
    the crypto backfill-source SHIM the monitor's injectable ``materialize_fn`` calls — a genuine live-emit
    vs batch-recompute parity test, no raw download.

Everything else is the equity loop unchanged: the per-cycle clean/dirty decision, the consecutive-clean
streak, the cert stamp on a sustained streak. The cert row goes to the SAME source-agnostic
``within_day_parity_cert`` table (the within-day operational checkpoint); the binary-trust GRANT routes to
``crypto_feature_trust`` (asset_class isolation — a crypto cert can never grant equity trust, docs/CRYPTO_E2E.md
§1). ``dry_run`` defaults everywhere — the live grant is the Lead's gated step.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import replace

import polars as pl

from quantlib.features import crypto_trust, store, trust_binary, within_day_assignment
from quantlib.features.compare import cell_verdict
from quantlib.features.crypto_validation_sweep import materialize_crypto_backfill
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_monitor import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_STABLE_CYCLES,
    evaluate_summary,
)
from quantlib.features.within_day_parity import DEFAULT_SAMPLE_SIZE
from quantlib.features.within_day_trust import CertResult, plan_writes, write_certifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("crypto_within_day")

DEFAULT_CRYPTO_WINDOW_MINUTES = 30
DEFAULT_CRYPTO_SETTLE_LAG_MIN = 2.0  # crypto needs only a small lag for the latest minute to finish writing


def crypto_settled_window(
    now_utc: dt.datetime, settle_lag_min: float, window_minutes: int
) -> tuple[dt.datetime, dt.datetime]:
    """The recently-settled UTC band to compare on for crypto: a ``window_minutes`` band ending
    ``settle_lag_min`` before now. No RTH, no NYSE calendar — crypto trades every minute, so the only hold-
    back is the small lag for the latest minute's writes to land."""
    end = now_utc - dt.timedelta(minutes=settle_lag_min)
    start = end - dt.timedelta(minutes=window_minutes)
    return start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0)


def crypto_replay_windows(
    band_end: dt.datetime, window_minutes: int, n_windows: int
) -> list[tuple[dt.datetime, dt.datetime]]:
    """A series of ``n_windows`` contiguous settled windows walked back from ``band_end`` — the crypto
    analogue of the equity ``_replay_windows`` for stepping a PAST day's already-settled band (so a past-day
    cert cycle runs the SAME compare on real cells without waiting wall-clock minutes). Each is
    ``window_minutes`` long; the set covers a contiguous band ending at ``band_end``."""
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    end = band_end
    for _ in range(n_windows):
        start = end - dt.timedelta(minutes=window_minutes)
        windows.append((start, end))
        end = start
    return list(reversed(windows))


def materialize_crypto_window(
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
    """The crypto backfill-source SHIM (the monitor's injectable ``materialize_fn`` for crypto): recompute the
    ``source=backfill`` side for ``cert_day`` from the persisted crypto INPUTS (no ``/store/raw``), via the
    same batch path the crypto sweep uses. ``raw_root`` / ``ensure_inputs_first`` / ``symbols`` are accepted to
    match the equity ``MaterializeFn`` signature but unused (crypto has no raw tape and recomputes the whole
    day's persisted inputs). Returns the symbol count recomputed; 0 in ``dry_run`` or when no inputs exist.

    NOTE: ``materialize_crypto_backfill`` recomputes ALL crypto groups for the day (the inputs feed every
    group at once). That is the right unit — the recompute is keyed by the day's persisted inputs, not by a
    single group — and writes the whole ``source=backfill`` side the per-group compare then reads."""
    if dry_run:
        logger.info(
            "DRY-RUN materialize_crypto_window: would recompute crypto backfill for day=%s from persisted "
            "inputs under %s (no store write)",
            cert_day.isoformat(),
            feature_root,
        )
        return 0
    count = materialize_crypto_backfill(feature_root, cert_day.isoformat())
    logger.info(
        "materialize_crypto_window: recomputed crypto backfill group-set for day=%s over %d symbols "
        "(group=%s is one of them)",
        cert_day.isoformat(),
        count,
        group_name,
    )
    return count


def compare_crypto_window(
    feature_root: str,
    group_name: str,
    day: dt.date,
    symbols: list[str],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> pl.DataFrame:
    """Per-feature match summary for a crypto group over the settled UTC window — the 24/7 analogue of
    :func:`within_day_parity.compare_window`. Reuses the SAME ``cell_verdict`` per-type tolerances, but with
    NO ``rth_mask`` (crypto trades every minute) and the crypto tolerance map. Read-only."""
    group = REGISTRY.get_group(group_name)
    tolerance_of = trust_binary.cell_tolerance_map()
    specs = {
        spec.name: replace(spec, tolerance=tolerance_of.get(spec.name, spec.tolerance))
        for spec in group.declare()
    }
    feature_names = list(specs.keys())

    day_start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
    day_end = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
    live = store.get_features(feature_names, symbols, day_start, day_end, feature_root, source="stream")
    backfill = store.get_features(
        feature_names, symbols, day_start, day_end, feature_root, source="backfill"
    )
    if backfill.height == 0:
        logger.warning("no crypto BACKFILL cells for group=%s day=%s — not recomputed yet", group_name, day)
        return pl.DataFrame()
    if live.height == 0:
        logger.warning("no crypto LIVE (stream) cells for group=%s day=%s — capture gap?", group_name, day)
        return pl.DataFrame()

    joined = live.join(backfill, on=["symbol", "minute"], how="full", suffix="_bk", coalesce=True).filter(
        (pl.col("minute") >= window_start) & (pl.col("minute") <= window_end)
    )
    if joined.height == 0:
        logger.warning(
            "no crypto cells in settled window [%s,%s] for group=%s", window_start, window_end, group_name
        )
        return pl.DataFrame()

    rows: list[dict[str, object]] = []
    for feature, spec in specs.items():
        if feature not in joined.columns or f"{feature}_bk" not in joined.columns:
            continue
        verdicts = joined.select(cell_verdict(spec, feature, joined.schema).alias("v"))
        tally = dict(
            zip(*[verdicts.group_by("v").len().to_dict(as_series=False)[key] for key in ("v", "len")])
        )
        n_match = int(tally.get("match", 0))
        n_mismatch = int(tally.get("mismatch", 0))
        n_compared = n_match + n_mismatch
        rows.append(
            {
                "feature": feature,
                "tolerance": spec.tolerance,
                "n_compared": n_compared,
                "n_match": n_match,
                "n_mismatch": n_mismatch,
                "n_extra_live": int(tally.get("extra_live", 0)),
                "n_missing_live": int(tally.get("missing_live", 0)),
                "value_rate": (n_match / n_compared) if n_compared > 0 else None,
            }
        )
    return pl.DataFrame(rows)


def _grant_crypto_trust(results: list[CertResult], cert_day: dt.date, *, dry_run: bool) -> dict[str, int]:
    """Grant the certified crypto features binary trust in ``crypto_feature_trust`` (NOT the equity ledger).
    Builds the clean-feature evidence the crypto grant path expects from the cert results. dry_run reports
    the plan without writing."""
    certified = [r.feature for r in results if r.status == "certified"]
    if not certified:
        return {"earned_trusted": 0}
    if dry_run:
        logger.info(
            "DRY-RUN crypto trust grant: would promote %d feature(s) to crypto_feature_trust (reason="
            "within_day_parity): %s",
            len(certified),
            certified,
        )
        return {"earned_trusted": 0}
    clean_today = pl.DataFrame(
        {
            "feature": certified,
            "version": [
                REGISTRY.get_group(r.group_name).version for r in results if r.status == "certified"
            ],
            "value_rate": [r.value_rate for r in results if r.status == "certified"],
            "passed": [True] * len(certified),
        }
    )
    return crypto_trust.write_crypto_grants(certified, clean_today, cert_day.isoformat())


def monitor_crypto(
    feature_root: str,
    group_name: str,
    agent_id: str,
    *,
    day: dt.date | None = None,
    mode: str = "live",
    band_end: dt.datetime | None = None,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    stable_cycles_required: int = DEFAULT_STABLE_CYCLES,
    window_minutes: int = DEFAULT_CRYPTO_WINDOW_MINUTES,
    settle_lag_min: float = DEFAULT_CRYPTO_SETTLE_LAG_MIN,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    materialize_backfill: bool = True,
    dry_run_cert: bool = True,
    dry_run_lock: bool = True,
    claim_lock: bool = True,
    max_cycles: int | None = None,
) -> CertResult | None:
    """The CRYPTO within-day monitor — own a crypto group through a real within-day cert cycle on LIVE 24/7
    crypto data. Same streak logic as the equity monitor; crypto compare window + crypto backfill recompute.

    ``mode='live'`` (default) compares a recently-settled band ending ``settle_lag_min`` before wall-clock
    now, one cycle per ``poll_seconds`` — the real-time path. ``mode='replay'`` walks contiguous windows back
    from ``band_end`` across a PAST, already-settled day's band (the equity ``_replay_windows`` analogue) so a
    past-day cert cycle runs the SAME compare on real cells without waiting wall-clock minutes — the in-action
    proof on already-captured data. On a sustained clean streak it stamps ``within_day_parity_cert`` (the
    source-agnostic checkpoint) and grants binary trust to ``crypto_feature_trust``. ``materialize_backfill``
    (default True; the backfill side is never pre-materialized live) recomputes it each cycle. dry_run default.
    """
    REGISTRY.get_group(group_name)
    now_utc = dt.datetime.now(dt.timezone.utc)
    cert_day = day or now_utc.date()

    if claim_lock and not within_day_assignment.claim(group_name, agent_id, dry_run=dry_run_lock):
        logger.warning("could not claim crypto group=%s (held by another agent) — aborting", group_name)
        return None
    logger.info(
        "CRYPTO MONITOR start group=%s agent=%s mode=%s day=%s settle_lag=%.1fmin need=%d clean cycles",
        group_name,
        agent_id,
        mode,
        cert_day,
        settle_lag_min,
        stable_cycles_required,
    )

    replay_band_end = band_end or dt.datetime.combine(cert_day, dt.time(23, 59), tzinfo=dt.timezone.utc)
    replay_windows = (
        crypto_replay_windows(replay_band_end, window_minutes, stable_cycles_required)
        if mode == "replay"
        else None
    )
    stable = 0
    cycle = 0
    try:
        while True:
            cycle += 1
            within_day_assignment.heartbeat(group_name, agent_id, dry_run=dry_run_lock)
            symbols = store.stream_symbols_on(feature_root, cert_day.isoformat())
            symbols = sorted(symbols)[:sample_size]
            if mode == "replay":
                assert replay_windows is not None  # mode=='replay' builds it above
                window_start, window_end = replay_windows[(cycle - 1) % len(replay_windows)]
            else:
                window_start, window_end = crypto_settled_window(
                    dt.datetime.now(dt.timezone.utc), settle_lag_min, window_minutes
                )
            if materialize_backfill and symbols:
                materialize_crypto_window(
                    feature_root,
                    group_name,
                    cert_day,
                    symbols,
                    raw_root="",
                    ensure_inputs_first=False,
                    agent_id=agent_id,
                    dry_run=dry_run_cert,
                )
            summary = (
                compare_crypto_window(feature_root, group_name, cert_day, symbols, window_start, window_end)
                if symbols
                else pl.DataFrame()
            )
            clean, results = evaluate_summary(
                summary, group_name, cert_day, stable + 1, window_minutes, settle_lag_min
            )
            if summary.height == 0:
                logger.info("crypto cycle %d: no comparable cells — streak held at %d", cycle, stable)
            elif clean:
                stable += 1
                worst = min((r.value_rate or 0.0) for r in results)
                logger.info(
                    "crypto cycle %d: CLEAN (%d features, worst value_rate=%.5f) — streak %d/%d",
                    cycle,
                    len(results),
                    worst,
                    stable,
                    stable_cycles_required,
                )
            else:
                failed = [r.feature for r in results if r.status != "certified"]
                logger.warning(
                    "crypto cycle %d: MISMATCH on %s — streak RESET (was %d)", cycle, failed, stable
                )
                stable = 0

            if stable >= stable_cycles_required and results:
                logger.info("crypto group=%s CERTIFIED after %d clean cycles", group_name, stable)
                certified = [CertResult(**{**r.__dict__, "stable_cycles": stable}) for r in results]
                write_certifications(certified, dry_run=dry_run_cert)
                _grant_crypto_trust(certified, cert_day, dry_run=dry_run_cert)
                within_day_assignment.release(group_name, agent_id, dry_run=dry_run_lock)
                return certified[0]

            if max_cycles is not None and cycle >= max_cycles:
                logger.info(
                    "crypto group=%s reached max_cycles=%d without certifying (streak %d/%d)",
                    group_name,
                    max_cycles,
                    stable,
                    stable_cycles_required,
                )
                return None
            if mode == "live":
                time.sleep(poll_seconds)
    finally:
        within_day_assignment.heartbeat(group_name, agent_id, dry_run=dry_run_lock)


def plan_crypto_cert(results: list[CertResult]) -> tuple[int, int]:
    """PURE: (cert_row_count, certified_feature_count) the crypto cert write WOULD produce — for a dry-run
    peek + the test. Reuses the equity ``plan_writes`` for the cert rows (source-agnostic cert table)."""
    cert_rows, _grants, _checks = plan_writes(results, trusted_already=None)
    certified = sum(1 for r in results if r.status == "certified")
    return len(cert_rows), certified
