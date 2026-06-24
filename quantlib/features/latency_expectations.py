"""Agent-updatable latency EXPECTATIONS — re-measure each feature group's per-bet p50/p95/p99 compute_latest
cost and rewrite ``docs/feature_latency_expectations.json`` deterministically (sorted slowest-first).

This is the LIVING DATA companion to ``docs/FEATURE_LATENCY_EXPECTATIONS.md`` (the stable explanation/design)
and ``docs/latency_budget.yaml`` (the pytest regression GATE). Ben's ask: the per-group expectations are a
JSON view — agent-updatable, UI-viewable — with p50/p95/p99 per group, ordered by slowness, kept SEPARATE
from the prose that does not change as we optimize. It is the JSON half of a measurement LOOP: re-measure ->
write JSON -> the dashboard reads it -> iterate as we optimize.

TWO MEASUREMENTS, both reproducible, no live capture touched:
  * PER-GROUP (the rankable ``groups`` array): each group's ``compute_latest`` (the LIVE per-minute path)
    timed in ISOLATION at a reference shard, p50/p95/p99 over a distribution of reps. Distinct per group —
    the sim splits the shared reduction emit evenly across its groups, so it CANNOT rank reductions against
    each other; the isolated path gives each its own number. These rows OVER-count the B incremental-sum
    groups (timed standalone, not as their in-flow shared-emit share) — true + documented; use for ranking.
  * E2E (the header ``e2e_context.measured_at_sim_scale``): the in-flow bar->vector p50/p95/p99 from driving
    the REAL streaming path — the #315 sim harness ``run_profile_sim_raw`` (protocol-faithful msgpack mock
    -> a real StockDataStream -> the same shard workers -> the incremental fast path) at a bounded prod-like
    scale. The honest number a bet pays (gather + IPC + shared emit together); NOT the sum of the rows.

As a REALISM cross-check we also harvest a recent window of the live ``crypto-capture`` container's
per-minute ``compute_ms`` (genuine 24/7 capture on the shared compute core) into the JSON header — see
``harvest_crypto_crosscheck``; a live floor to sanity-check against, NOT a per-group source (tiny universe).

The curated per-group METADATA (kind / mechanism / incremental-readiness) is stable design context that does
NOT change minute-to-minute; it lives in ``GROUP_METADATA`` here. Feature counts come from the registry. The
MEASURED p50/p95/p99 is the part that changes as we optimize, so the loop reruns:

    # one-shot re-measure (after an optimization lands) + the scheduled recompute both call this:
    docker run --rm --cpus=8 -v "$PWD":/app -w /app --env-file .env fp-dev \\
        python -m quantlib.features.latency_expectations --update

``--update`` re-measures and rewrites the JSON in place (deterministic: sorted by p99 desc, stable key order,
``generated_at`` from ``SOURCE_DATE_EPOCH`` if set else now). ``--no-crypto`` skips the live cross-check
(e.g. when crypto-capture is down or docker is unreachable). Omit ``--update`` to print the table without
writing. ``ops/remeasure_latency.sh`` is the cpu-capped wrapper the cron + the post-optimize trigger run.
See docs/FEATURE_LATENCY_EXPECTATIONS.md for how to read it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from quantlib.features.base import FeatureGroup
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.profile import _live_call, build_frames, runs_incremental
from quantlib.features.profile_sim import run_profile_sim_raw
from quantlib.features.registry import REGISTRY

JSON_PATH = Path("docs/feature_latency_expectations.json")
SCHEMA_VERSION = 2

# Authoritative per-group feature counts from the registry (so a group add/remove is reflected without a
# hand-edit). Built once at import.
FEATURE_COUNTS: dict[str, int] = {group.name: len(group.feature_names) for group in REGISTRY.groups()}

# Per-group RANKING scale: each group's compute_latest (the LIVE per-minute path) timed in isolation at a
# single reference shard, over a distribution of reps for real p50/p95/p99. This is the DISTINCT per-group
# cost (which group is expensive) — the sim's at-T phase timing buckets all shared-emit reductions into one
# split-evenly number, so it cannot rank reductions against each other; the isolated path can. The numbers
# OVER-count the B incremental-sum groups (each is timed standalone, not as its in-flow shared-emit share)
# — true and documented; use for RELATIVE ranking + regression, the e2e block carries the in-flow truth.
REF_N_TICKERS = 312
REF_WINDOW_MIN = 245
REF_DAILY_DAYS = 200
REF_INCLUDE_TRADES = True
DEFAULT_REPS = 25
REFERENCE_SHARD = {
    "n_tickers": REF_N_TICKERS,
    "window_min": REF_WINDOW_MIN,
    "daily_days": REF_DAILY_DAYS,
    "include_trades": REF_INCLUDE_TRADES,
}

# Reproducible e2e scale for the REAL streaming path (the #315 sim harness) — the in-flow bar->vector truth
# recorded in the header. Bounded so the whole job runs in ~2-4 min without starving live capture (the cron
# caps cpus on top). At ~62 syms/shard the fixed overhead amortizes like the production 1000/16 layout.
SIM_N_SYMBOLS = 992
SIM_N_SHARDS = 16
SIM_MEASURE_MINUTES = 15
SIM_WARMUP_MINUTES = 8
SIM_WINDOW_MIN = 120
SIM_SCALE = {
    "n_symbols": SIM_N_SYMBOLS,
    "n_shards": SIM_N_SHARDS,
    "measure_minutes": SIM_MEASURE_MINUTES,
    "warmup_minutes": SIM_WARMUP_MINUTES,
    "window_min": SIM_WINDOW_MIN,
}

# Live crypto-capture is a genuine 24/7 capture running the SAME shared compute core on a real Alpaca
# feed; its per-minute compute_ms log line is real live latency. We harvest a recent window of it as a
# REALISM CROSS-CHECK (not the per-group source — crypto is a tiny 2-5 symbol universe with SPY-relative
# groups excluded, so it is fixed-overhead-dominated and not per-group-attributable for equity).
CRYPTO_CAPTURE_CONTAINER = "crypto-capture"
CRYPTO_LOG_TAIL = 400
_CRYPTO_COMPUTE_RE = re.compile(r"compute_ms=(\d+(?:\.\d+)?)")

# Stable per-group design metadata (Ben's A/B/Rust framing). The KIND/mechanism/incremental-readiness do
# NOT change minute-to-minute — only the measured p50/p95/p99 does. Mirrors the framing in
# docs/FEATURE_LATENCY_EXPECTATIONS.md. A group missing here still gets measured; it is reported with
# kind "unclassified" so a newly-added group is loud, not silently dropped.
GROUP_METADATA: dict[str, dict[str, str]] = {
    "price_volume": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum (WindowedSumState)",
        "incremental_ready": "parked",
    },
    "distribution": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "volume_leads_price": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "liquidity": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "return_dynamics": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "subminute_gap_fano": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest (per-minute tick group-by)",
        "incremental_ready": "n-a",
    },
    "momentum_consistency": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "price_returns": {
        "kind": "Rust-resident",
        "mechanism": "StatefulEngine (EMA/lag/extrema fold)",
        "incremental_ready": "n-a",
    },
    "clean_momentum": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "market_beta": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "parked",
    },
    "momentum_run": {
        "kind": "B latest-only",
        "mechanism": "own latest-only (skew+streak)",
        "incremental_ready": "n-a",
    },
    "market_turbulence": {
        "kind": "Gather",
        "mechanism": "universe gather (reader-phase)",
        "incremental_ready": "n-a",
        "note": "compute_latest builds the per-symbol measures for the latest minute ALONE (a few "
        "close[T-W] lookups + one trailing-RV std slice) instead of the full-buffer rolling derive "
        "compute() runs over every minute — measured ~30->~4ms p50 at REF scale, parity-true to "
        "declared tolerance (Lever B compute-once-for-T).",
    },
    "size_entropy": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest",
        "incremental_ready": "n-a",
    },
    "breadth": {"kind": "Gather", "mechanism": "universe gather (reader-phase)", "incremental_ready": "n-a"},
    "volume_exhaustion": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "trend_quality": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "swing": {
        "kind": "Rust-resident",
        "mechanism": "quant_tick.swing_fold kernel; live carries per-symbol leg-state (FP_SWING_STATEFUL) "
        "— default re-folds whole buffer",
        "incremental_ready": "stateful-gated",
        "note": "The Rust kernel folds O(1)/bar, but DEFAULT compute_latest re-invokes it over the WHOLE "
        "ring each minute. FP_SWING_STATEFUL=1 carries the leg-state and folds only the new minute "
        "(value-identical, fp-unchanged): measured 26.1->15.5ms p50 group-local; full O(1) needs "
        "delta-passing (docs/FEATURE_EFFICIENCY_AUDIT.md).",
    },
    "technical": {
        "kind": "Rust-resident",
        "mechanism": "StatefulEngine (EMA fold)",
        "incremental_ready": "n-a",
    },
    "momentum": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "quote_spread": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "ohlc_vol": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "sector_return": {
        "kind": "Gather",
        "mechanism": "universe gather (reader-phase)",
        "incremental_ready": "n-a",
    },
    "sector_beta": {
        "kind": "Gather",
        "mechanism": "universe gather (reader-phase)",
        "incremental_ready": "n-a",
    },
    "residual_analysis": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "parked",
    },
    "volatility": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "trade_flow": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "price_levels": {
        "kind": "Rust-resident",
        "mechanism": "StatefulEngine (ExtremaState fold)",
        "incremental_ready": "n-a",
    },
    "signed_trade_ratio": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "print_hhi": {"kind": "hand-written", "mechanism": "bespoke compute_latest", "incremental_ready": "n-a"},
    "return_dispersion": {
        "kind": "A cached/static",
        "mechanism": "SessionCache daily memo (#281)",
        "incremental_ready": "n-a",
    },
    "efficiency": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "cross_sectional_rank": {
        "kind": "Gather",
        "mechanism": "universe gather (reader-phase)",
        "incremental_ready": "n-a",
    },
    "volume": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum + centered-std (#307)",
        "incremental_ready": "ready",
    },
    "range_expansion": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "trade_freq_z": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "microstructure_burst": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest",
        "incremental_ready": "n-a",
    },
    "candlestick": {
        "kind": "Rust-resident",
        "mechanism": "StatefulEngine (LastKState fold)",
        "incremental_ready": "n-a",
    },
    "realized_range": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "inter_arrival": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest",
        "incremental_ready": "n-a",
    },
    "count_fano": {
        "kind": "B incremental-sum",
        "mechanism": "shared running-sum",
        "incremental_ready": "ready",
    },
    "market_context": {
        "kind": "Gather",
        "mechanism": "universe gather (reader-phase)",
        "incremental_ready": "n-a",
    },
    "intraday_seasonality": {
        "kind": "B latest-only",
        "mechanism": "own latest-only session agg (#286)",
        "incremental_ready": "n-a",
    },
    "calendar_events": {
        "kind": "A cached/static",
        "mechanism": "consolidated point-in-time pass",
        "incremental_ready": "n-a",
    },
    "runner_state": {
        "kind": "B latest-only",
        "mechanism": "shared session-cumulative pass (#285)",
        "incremental_ready": "n-a",
    },
    "gap_fill_state": {
        "kind": "B latest-only",
        "mechanism": "shared session-cumulative pass (#285)",
        "incremental_ready": "n-a",
    },
    "trade_size_dist": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest",
        "incremental_ready": "n-a",
    },
    "calendar": {
        "kind": "A cached/static",
        "mechanism": "consolidated point-in-time pass",
        "incremental_ready": "n-a",
    },
    "draw_range": {
        "kind": "B latest-only",
        "mechanism": "own latest-only window agg (#257)",
        "incremental_ready": "n-a",
    },
    "large_print_burst": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest",
        "incremental_ready": "n-a",
    },
    "dumper_state": {
        "kind": "B latest-only",
        "mechanism": "shared session-cumulative pass (#285)",
        "incremental_ready": "n-a",
    },
    "tick_runlength": {
        "kind": "hand-written",
        "mechanism": "bespoke compute_latest",
        "incremental_ready": "n-a",
    },
    "prior_day": {
        "kind": "A cached/static",
        "mechanism": "consolidated daily-broadcast pass",
        "incremental_ready": "n-a",
    },
    "sector": {
        "kind": "A cached/static",
        "mechanism": "consolidated point-in-time pass",
        "incremental_ready": "n-a",
    },
    "edgar_filing_frequency": {
        "kind": "A-hybrid (event-kind)",
        "mechanism": "SessionCache filings; intraday available_at<=minute gate",
        "incremental_ready": "n-a",
    },
    "news_sentiment": {
        "kind": "A-hybrid (event-kind)",
        "mechanism": "news snapshot; intraday available_at<=minute point-in-time gate",
        "incremental_ready": "n-a",
    },
    "liquidity_rank": {
        "kind": "A cached/static",
        "mechanism": "SessionCache daily memo (#281)",
        "incremental_ready": "n-a",
    },
    "multi_day_returns": {
        "kind": "A cached/static",
        "mechanism": "consolidated daily-broadcast pass",
        "incremental_ready": "n-a",
    },
    "multi_day_vwap": {
        "kind": "A cached/static",
        "mechanism": "consolidated daily-broadcast pass",
        "incremental_ready": "n-a",
    },
    "daily_beta": {
        "kind": "A cached/static",
        "mechanism": "SessionCache daily memo (#281)",
        "incremental_ready": "n-a",
    },
    "overnight_beta": {
        "kind": "A cached/static",
        "mechanism": "SessionCache daily memo (#262)",
        "incremental_ready": "n-a",
    },
    "overnight_intraday_split": {
        "kind": "A cached/static",
        "mechanism": "SessionCache daily memo (#281)",
        "incremental_ready": "n-a",
    },
    "asset_flags": {
        "kind": "A cached/static",
        "mechanism": "consolidated point-in-time pass",
        "incremental_ready": "n-a",
    },
    "round_levels": {
        "kind": "A cached/static",
        "mechanism": "consolidated point-in-time pass",
        "incremental_ready": "n-a",
    },
    "peer_relative": {
        "kind": "Gather",
        "mechanism": "universe gather (reader-phase)",
        "incremental_ready": "n-a",
    },
}


def _pct(sorted_ms: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (matches the sim harness's ``_percentile``)."""
    if not sorted_ms:
        return 0.0
    idx = min(len(sorted_ms) - 1, int(round((pct / 100.0) * (len(sorted_ms) - 1))))
    return round(sorted_ms[idx], 2)


def _meta_for(name: str) -> dict[str, str]:
    return GROUP_METADATA.get(
        name,
        {"kind": "unclassified", "mechanism": "(no metadata - newly added?)", "incremental_ready": "n-a"},
    )


def _incremental_ready(group: FeatureGroup, meta: dict[str, str]) -> str:
    """The incremental-readiness label, DERIVED from the group's live ``incremental_safe`` for reduction
    groups so it can never drift from the actual ``path``. A ``ReductionGroup`` is "armed" when it rides the
    incremental running-sum ``step`` fold live (``runs_incremental`` — what ``path`` reflects) and "parked"
    when its conditioning keeps it on the batch fresh-sum recompute. price_volume/market_beta are armed only
    under FP_RUST_REDUCE (their y-anchored corr denom rounds path-identically there); with the flag off they
    fall back to "parked", and this label tracks that automatically. Non-reduction groups keep their stable
    hand-curated label (``n-a`` for stateful/cached/gather/hand-written)."""
    if isinstance(group, ReductionGroup):
        return "armed" if runs_incremental(group) else "parked"
    return meta["incremental_ready"]


def measure_group_rows(reps: int) -> list[dict]:
    """Per-group DISTINCT p50/p95/p99: time each runnable group's ``compute_latest`` (the LIVE per-minute
    path) in isolation at the reference shard, over ``reps`` runs (after a warmup), keeping the full
    distribution. Sorted slowest-first by p99. This is the rankable per-group cost — the sim cannot rank
    shared-emit reductions against each other (it splits the batch emit evenly), the isolated path can."""
    frames = build_frames(REF_N_TICKERS, REF_WINDOW_MIN, REF_DAILY_DAYS, include_trades=REF_INCLUDE_TRADES)
    rows: list[dict] = []
    for group in runnable(frames):
        ordered = _time_group_distribution(group, frames, reps)
        meta = _meta_for(group.name)
        row = {
            "group": group.name,
            "feat_count": FEATURE_COUNTS.get(group.name, len(group.feature_names)),
            "kind": meta["kind"],
            "mechanism": meta["mechanism"],
            "incremental_ready": _incremental_ready(group, meta),
            # The EXECUTION PATH whose cost the p50/p95/p99 below reflect: "incremental (live)" for the 15
            # armed incremental_safe reduction groups (their O(1) running-sum ``step`` fold — the default live
            # path since #391, what the live fc actually runs), "batch" for every other group (the
            # ``compute_latest`` recompute, incl. the 8 parked incremental_safe=False reductions). Additive
            # field (schema_version unchanged) so the dashboard keeps binding p50/kind/incremental_ready as-is.
            "path": "incremental (live)" if runs_incremental(group) else "batch",
            "p50_ms": _pct(ordered, 50),
            "p95_ms": _pct(ordered, 95),
            "p99_ms": _pct(ordered, 99),
        }
        if "note" in meta:  # optional per-group caveat (e.g. swing's FP_SWING_STATEFUL gate, #334)
            row["note"] = meta["note"]
        rows.append(row)
    rows.sort(key=lambda row: (-row["p99_ms"], row["group"]))
    return rows


def _time_group_distribution(group: FeatureGroup, frames: dict, reps: int) -> list[float]:
    """The sorted ms distribution of the group's TRUE LIVE per-minute path over ``reps`` runs (warmup
    excluded). ``_live_call`` (shared with the budget gate) dispatches to ``StatefulEngine.step()`` for a
    stateful group and a live-breadth-thinned tape for a raw-trades tick group, so the JSON reflects real
    live cost — NOT the rolling-derive backfill twin / full-universe-tape profiler artifacts (#381)."""
    call = _live_call(group, frames)
    call()  # warmup (JIT/import/cache priming excluded from the distribution)
    times_ms: list[float] = []
    for _ in range(reps):
        start = time.perf_counter()
        call()
        times_ms.append((time.perf_counter() - start) * 1000.0)
    return sorted(times_ms)


def measure_e2e() -> list[float]:
    """The realistic in-flow bar->vector distribution: drive the REAL streaming path (the #315
    ``run_profile_sim_raw``: msgpack mock -> StockDataStream -> shard workers -> incremental fast path) at
    the bounded SIM scale and return the per-minute slowest-shard bar->vector ms over the post-warmup
    minutes. This is the honest number a bet pays (gather + IPC + shared-emit all paid together) — the
    per-group rows above do NOT sum to it."""
    with tempfile.TemporaryDirectory(prefix="latency_expect_") as tmp:
        by_minute, dispatch_walls = run_profile_sim_raw(
            SIM_N_SYMBOLS, SIM_N_SHARDS, SIM_MEASURE_MINUTES, SIM_WARMUP_MINUTES, SIM_WINDOW_MIN, tmp
        )
    return sorted(
        (max(r["ready_wall"] for r in recs if "ready_wall" in r) - dispatch_walls[minute]) * 1000.0
        for minute, recs in by_minute.items()
        if minute in dispatch_walls and any("ready_wall" in r for r in recs)
    )[SIM_WARMUP_MINUTES:]


def _crypto_log_text() -> str:
    """The crypto-capture log text to scrape. Inside fp-dev (no docker socket) the wrapper pre-harvests on
    the host and passes the path via ``CRYPTO_COMPUTE_MS_FILE``; otherwise (run on the host) shell out to
    ``docker logs`` directly. Best-effort — a missing file / unreachable docker yields empty text."""
    log_file = os.environ.get("CRYPTO_COMPUTE_MS_FILE")
    if log_file:
        path = Path(log_file)
        return path.read_text() if path.exists() else ""
    result = subprocess.run(
        ["docker", "logs", "--tail", str(CRYPTO_LOG_TAIL), CRYPTO_CAPTURE_CONTAINER],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout + result.stderr


def harvest_crypto_crosscheck() -> dict:
    """Harvest a recent window of live crypto-capture ``compute_ms`` (genuine 24/7 capture on the shared
    compute core) as a REALISM anchor. Best-effort: if the logs are unreachable, return a
    ``status: unavailable`` stub rather than failing the JSON regen (the sim numbers stand alone)."""
    samples = sorted(
        float(match.group(1))
        for line in _crypto_log_text().splitlines()
        if (match := _CRYPTO_COMPUTE_RE.search(line))
    )
    if not samples:
        return {
            "status": "unavailable",
            "note": "crypto-capture logs had no compute_ms lines at regen time (container down / no window)",
        }
    return {
        "status": "ok",
        "metric": "live crypto-capture per-minute compute_ms (whole crypto-applicable feature set)",
        "samples": len(samples),
        "p50_ms": _pct(samples, 50),
        "p95_ms": _pct(samples, 95),
        "p99_ms": _pct(samples, 99),
        "note": (
            "Genuine LIVE latency on the shared compute core, but a tiny 2-5 symbol crypto universe with "
            "SPY-relative groups excluded -> fixed-overhead-dominated, NOT per-group-attributable for "
            "equity. Use as a realism floor that the reproducible sim per-group numbers must be sane "
            "against, not as a per-group source."
        ),
    }


def _e2e_block(end_to_end_ms: list[float], prior_e2e: dict | None) -> dict:
    """The ``measured_at_sim_scale`` block. When THIS regen measured the e2e (``end_to_end_ms`` non-empty),
    return its fresh percentiles. When it did NOT (``--no-e2e`` — the heavy sim is gather/IPC-bound and too
    noisy to trust on a loaded box, so we skip it rather than write a fabricated number), CARRY FORWARD the
    prior file's measured block verbatim (so a per-group-only regen never zeroes / fakes the e2e), flagged
    ``measured_this_regen: false``. Only ever a real measurement or an honestly-labelled carried-forward one —
    never a zero stand-in."""
    if end_to_end_ms:
        return {
            "p50_ms": _pct(end_to_end_ms, 50),
            "p95_ms": _pct(end_to_end_ms, 95),
            "p99_ms": _pct(end_to_end_ms, 99),
            "minutes": len(end_to_end_ms),
            "measured_this_regen": True,
        }
    if prior_e2e:
        carried = {
            key: prior_e2e[key] for key in ("p50_ms", "p95_ms", "p99_ms", "minutes") if key in prior_e2e
        }
        carried["measured_this_regen"] = False
        return carried
    return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "minutes": 0, "measured_this_regen": False}


def build_document(
    groups: list[dict],
    end_to_end_ms: list[float],
    crypto: dict,
    generated_at: str,
    prior_e2e: dict | None = None,
) -> dict:
    """Assemble the full JSON document: header block (schema/units/e2e context/measurement provenance +
    the live crypto cross-check) then the slowest-first ``groups`` array. The header carries everything a
    UI needs to render without reading the MD. ``prior_e2e`` is the prior file's ``measured_at_sim_scale``,
    carried forward when this regen skipped the e2e sim (``--no-e2e``) so the block is never zeroed."""
    measured_e2e = _e2e_block(end_to_end_ms, prior_e2e)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "units": "milliseconds",
        "sorted_by": "p99_ms descending (slowest-first)",
        "measurement": {
            "source": "quantlib.features.latency_expectations --update",
            "per_group_method": (
                "each group's TRUE LIVE per-minute path timed in ISOLATION at the reference shard, "
                "p50/p95/p99 over a distribution of reps. The path is the one the live fc runs (see each "
                "row's `path`): the 15 armed incremental_safe reduction groups are timed via a seeded "
                "IncrementalEngine.step (the O(1) running-sum fold — the default live path since #391, so "
                "the dashboard now shows the INCREMENTAL cost, not the old batch recompute); a StatefulGroup "
                "via StatefulEngine.step; everything else (incl. the 8 parked incremental_safe=False "
                "reductions) via the batch compute_latest. Distinct + rankable per group (the sim splits the "
                "shared reduction emit evenly, so it cannot rank reductions); each incremental/reduction row "
                "is timed STANDALONE (its own single-group engine), so it OVER-counts vs its true share of "
                "the SHARED in-flow engine fold. Use for RELATIVE ranking + regression detection; the e2e "
                "block carries the in-flow bar->vector truth."
            ),
            "e2e_method": (
                "the REAL streaming path (run_profile_sim_raw: msgpack mock -> StockDataStream -> shard "
                "workers -> incremental fast path, the #315 e2e harness) at the sim scale; the in-flow "
                "bar->vector truth (gather + IPC + shared emit paid together)."
            ),
            "reproducible": True,
            "reference_shard": REFERENCE_SHARD,
            "sim_scale": SIM_SCALE,
        },
        "e2e_context": {
            "metric": "per-bet bar->vector (minute's last bar -> that bet's vector ready, slowest shard)",
            "measured_at_sim_scale": measured_e2e,
            "single_bet_isolated_p50_ms": 289,
            "typical_bet_under_load_p50_ms": 935,
            "target_p99_ms": 100,
            "note": (
                "measured_at_sim_scale carries measured_this_regen: when true it is THIS regen's sim run at "
                "the bounded sim scale; when false the e2e sim was SKIPPED this regen (--no-e2e, e.g. a "
                "per-group-only incremental refresh) and the block is the prior measurement carried forward "
                "verbatim, NOT a fresh number. The e2e sim is gather/IPC-bound, so it moves far less than the "
                "per-group reduction speedup AND is run-to-run noisy on a loaded box (a quiet box is needed "
                "for a trustworthy figure) — it is left carried-forward rather than overwritten with a noisy "
                "value. The isolated/under-load figures are the documented production anchors. The e2e number "
                "is NOT the sum of the per-group rows (which over-count reductions + exclude gather/IPC)."
            ),
        },
        "live_crypto_crosscheck": crypto,
        "group_count": len(groups),
        "feature_count": sum(row["feat_count"] for row in groups),
        "not_measured_groups": [
            {
                "group": name,
                "reason": "gather group - runs once in the reader phase, not a per-bet cost, and not "
                "runnable in the isolated reference-shard frames (no measurable compute_latest here)",
                "kind": GROUP_METADATA[name]["kind"],
            }
            for name in sorted(set(GROUP_METADATA) - {row["group"] for row in groups})
        ],
        "groups": groups,
    }


def _now_iso() -> str:
    """``SOURCE_DATE_EPOCH`` if set (reproducible builds), else the current UTC time, ISO-8601 with Z."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else datetime.now(timezone.utc)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_prior_e2e(path: Path) -> dict | None:
    """The prior file's ``e2e_context.measured_at_sim_scale`` (or None when the file/key is absent), so a
    ``--no-e2e`` regen carries the last real measurement forward instead of zeroing the block."""
    if not path.exists():
        return None
    prior = json.loads(path.read_text())
    return prior.get("e2e_context", {}).get("measured_at_sim_scale")


def measure_and_write(path: Path, reps: int, with_crypto: bool, with_e2e: bool) -> dict:
    """The one-shot re-measure: per-group isolated p50/p95/p99 + (optionally) the sim e2e block + the
    crypto cross-check, written deterministically. Returns the document written. The single entry the cron
    + the post-optimize trigger call. When ``with_e2e`` is False the prior file's e2e block is carried
    forward (the e2e sim is noisy on a loaded box — never overwrite it with a fabricated/zero number)."""
    groups = measure_group_rows(reps)
    end_to_end = measure_e2e() if with_e2e else []
    crypto = harvest_crypto_crosscheck() if with_crypto else {"status": "skipped"}
    prior_e2e = None if with_e2e else _load_prior_e2e(path)
    document = build_document(groups, end_to_end, crypto, _now_iso(), prior_e2e=prior_e2e)
    path.write_text(json.dumps(document, indent=2) + "\n")
    return document


def _print_table(groups: list[dict]) -> None:
    print(f"{'group':<28}{'feat':>5}{'p50_ms':>9}{'p95_ms':>9}{'p99_ms':>9}  kind")
    for row in groups:
        print(
            f"{row['group']:<28}{row['feat_count']:>5}{row['p50_ms']:>9.2f}{row['p95_ms']:>9.2f}"
            f"{row['p99_ms']:>9.2f}  {row['kind']}"
        )
    print("\nTOP-5 slowest (p99): " + ", ".join(f"{r['group']} ({r['p99_ms']:.0f}ms)" for r in groups[:5]))


def main() -> None:
    do_update = "--update" in sys.argv
    with_crypto = "--no-crypto" not in sys.argv
    with_e2e = "--no-e2e" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reps = int(args[0]) if args else DEFAULT_REPS
    print(
        f"measuring per-group compute_latest p50/p95/p99 in isolation at {REF_N_TICKERS} tickers "
        f"x {REF_WINDOW_MIN}m, {reps} reps"
        + (f" + e2e via the real streaming sim at {SIM_N_SYMBOLS}/{SIM_N_SHARDS}..." if with_e2e else "..."),
        flush=True,
    )
    groups = measure_group_rows(reps)
    _print_table(groups)
    end_to_end = measure_e2e() if with_e2e else []
    if end_to_end:
        print(
            f"\ne2e bar->vector @ sim scale: p50={_pct(end_to_end, 50):.0f}ms "
            f"p95={_pct(end_to_end, 95):.0f}ms p99={_pct(end_to_end, 99):.0f}ms"
        )
    if do_update:
        crypto = harvest_crypto_crosscheck() if with_crypto else {"status": "skipped"}
        prior_e2e = None if with_e2e else _load_prior_e2e(JSON_PATH)
        document = build_document(groups, end_to_end, crypto, _now_iso(), prior_e2e=prior_e2e)
        JSON_PATH.write_text(json.dumps(document, indent=2) + "\n")
        e2e_status = f"{len(end_to_end)} min" if end_to_end else "carried-forward (--no-e2e)"
        print(
            f"\nwrote {JSON_PATH} ({document['group_count']} groups, {document['feature_count']} features; "
            f"e2e: {e2e_status}; crypto cross-check: {crypto['status']})"
        )
    else:
        print(f"\n(dry run - pass --update to rewrite {JSON_PATH})")


if __name__ == "__main__":
    main()
