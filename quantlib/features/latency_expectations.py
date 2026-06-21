"""Agent-updatable latency EXPECTATIONS — re-measure each feature group's per-bet p50/p99 compute_latest
cost and rewrite ``docs/feature_latency_expectations.json`` deterministically (sorted slowest-first).

This is the LIVING DATA companion to ``docs/FEATURE_LATENCY_EXPECTATIONS.md`` (the stable explanation/design)
and ``docs/latency_budget.yaml`` (the pytest regression GATE). Ben's ask: the per-group expectations are a
JSON view — agent-updatable, UI-viewable — with p50 AND p99 per group, ordered by slowness, kept SEPARATE
from the prose that does not change as we optimize.

What it measures: for every runnable FeatureGroup, ``compute_latest`` (the LIVE per-minute path) is timed
over ``reps`` runs at the reference shard scale (the same ``build_frames`` the per-group budget gate uses),
and the p50/p99 of that distribution is recorded. Unlike ``profile.py`` (which keeps the MIN over reps for a
stable regression seed), this keeps the full distribution so a person/UI sees the typical (p50) and tail
(p99) cost — the two percentiles Ben asked for.

The curated per-group METADATA (kind / mechanism / incremental-readiness / feature count) is stable design
context that does not change minute-to-minute; it lives in ``GROUP_METADATA`` here and is the same framing
the MD documents. The MEASURED p50/p99 is the part that changes as we optimize, so a future agent reruns:

    docker run --rm -v "$PWD":/app -w /app --env-file .env fp-dev \\
        python -m quantlib.features.latency_expectations --update

``--update`` re-measures and rewrites the JSON in place (deterministic: sorted by p99 desc, stable key
order, ``generated_at`` taken from ``SOURCE_DATE_EPOCH`` if set else now). Omit ``--update`` to print the
table without writing. See docs/FEATURE_LATENCY_EXPECTATIONS.md for how to read it.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.compare import runnable
from quantlib.features.profile import build_frames

JSON_PATH = Path("docs/feature_latency_expectations.json")
SCHEMA_VERSION = 1

REF_N_TICKERS = 312
REF_WINDOW_MIN = 245
REF_DAILY_DAYS = 200
REF_INCLUDE_TRADES = True
REFERENCE_SCALE = {
    "n_tickers": REF_N_TICKERS,
    "window_min": REF_WINDOW_MIN,
    "daily_days": REF_DAILY_DAYS,
    "include_trades": REF_INCLUDE_TRADES,
}
DEFAULT_REPS = 25

# Stable per-group design metadata (Ben's A/B/Rust framing). The KIND/mechanism/incremental-readiness do
# NOT change minute-to-minute — only the measured p50/p99 does. Mirrors the framing in
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
        "mechanism": "quant_tick.swing_fold Rust kernel",
        "incremental_ready": "n-a",
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


def measure_group_percentiles(group: FeatureGroup, frames: dict, reps: int) -> tuple[float, float]:
    """Time ``group.compute_latest`` over ``reps`` runs (after a warmup) and return ``(p50_ms, p99_ms)`` of
    the distribution. Keeps the full distribution (not the min) so the tail percentile is real."""
    ctx = BatchContext(frames=frames)
    group.compute_latest(ctx)  # warmup (JIT/import/cache priming excluded from the distribution)
    times_ms: list[float] = []
    for _ in range(reps):
        start = time.perf_counter()
        group.compute_latest(ctx)
        times_ms.append((time.perf_counter() - start) * 1000.0)
    times_ms.sort()
    p50 = statistics.median(times_ms)
    p99 = times_ms[min(len(times_ms) - 1, int(round(0.99 * (len(times_ms) - 1))))]
    return round(p50, 2), round(p99, 2)


def measure_all(reps: int) -> list[dict]:
    """Measure p50/p99 for every runnable group, returning a list of per-group dicts sorted by p99
    descending (slowest-first) — the order a UI renders and the canonical on-disk order."""
    frames = build_frames(REF_N_TICKERS, REF_WINDOW_MIN, REF_DAILY_DAYS, include_trades=REF_INCLUDE_TRADES)
    groups: list[dict] = []
    for group in runnable(frames):
        p50, p99 = measure_group_percentiles(group, frames, reps)
        meta = GROUP_METADATA.get(
            group.name,
            {
                "kind": "unclassified",
                "mechanism": "(no metadata — newly added?)",
                "incremental_ready": "n-a",
            },
        )
        groups.append(
            {
                "group": group.name,
                "feat_count": len(group.feature_names),
                "kind": meta["kind"],
                "mechanism": meta["mechanism"],
                "incremental_ready": meta["incremental_ready"],
                "p50_ms": p50,
                "p99_ms": p99,
            }
        )
    groups.sort(key=lambda row: (-row["p99_ms"], row["group"]))
    return groups


def build_document(groups: list[dict], generated_at: str) -> dict:
    """Assemble the full JSON document: header block (schema/units/e2e context) + the slowest-first groups
    array. The header carries the stable context a UI needs to render without reading the MD."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "units": "milliseconds",
        "sorted_by": "p99_ms descending (slowest-first)",
        "measurement": {
            "source": "quantlib.features.latency_expectations --update",
            "path": "per-group compute_latest (the LIVE per-minute path), full distribution over reps",
            "reference_shard": REFERENCE_SCALE,
            "note": (
                "Single-shard per-group profiling view. It OVER-counts the B incremental-sum groups "
                "(they share ONE batched incremental emit in flow, so e.g. price_volume's standalone ms "
                "is not its in-flow share) and excludes the reader gather/IPC. Use for RELATIVE ranking + "
                "regression detection; the honest bar->vector number is the e2e gate."
            ),
        },
        "e2e_context": {
            "metric": "per-bet bar->vector (minute's last bar -> that bet's vector ready)",
            "single_bet_isolated_p50_ms": 289,
            "typical_bet_under_load_p50_ms": 935,
            "target_p99_ms": 100,
            "note": (
                "The e2e numbers are NOT the sum of the per-group ms below (those over-count reductions + "
                "exclude gather/IPC). See docs/FEATURE_LATENCY_EXPECTATIONS.md and the e2e gate "
                "(docs/latency_e2e_budget.yaml)."
            ),
        },
        "group_count": len(groups),
        "feature_count": sum(row["feat_count"] for row in groups),
        "not_measured_groups": [
            {
                "group": name,
                "reason": "gather group - runs once in the reader phase, not a per-bet cost, and not "
                "runnable in the single-shard profiler frames (no measurable compute_latest here)",
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


def update(reps: int, path: Path) -> dict:
    """Re-measure and rewrite the JSON deterministically. Returns the document written."""
    groups = measure_all(reps)
    document = build_document(groups, _now_iso())
    path.write_text(json.dumps(document, indent=2) + "\n")
    return document


def _print_table(groups: list[dict]) -> None:
    print(f"{'group':<28}{'feat':>5}{'p50_ms':>10}{'p99_ms':>10}  kind")
    for row in groups:
        print(
            f"{row['group']:<28}{row['feat_count']:>5}{row['p50_ms']:>10.2f}{row['p99_ms']:>10.2f}  {row['kind']}"
        )
    print("\nTOP-5 slowest (p99): " + ", ".join(f"{r['group']} ({r['p99_ms']:.0f}ms)" for r in groups[:5]))


def main() -> None:
    do_update = "--update" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reps = int(args[0]) if args else DEFAULT_REPS
    print(
        f"measuring per-group compute_latest p50/p99 at {REFERENCE_SCALE['n_tickers']} tickers "
        f"x {REFERENCE_SCALE['window_min']}m, {reps} reps...",
        flush=True,
    )
    groups = measure_all(reps)
    _print_table(groups)
    if do_update:
        document = build_document(groups, _now_iso())
        JSON_PATH.write_text(json.dumps(document, indent=2) + "\n")
        print(
            f"\nwrote {JSON_PATH} ({document['group_count']} groups, {document['feature_count']} features)"
        )
    else:
        print(f"\n(dry run — pass --update to rewrite {JSON_PATH})")


if __name__ == "__main__":
    main()
