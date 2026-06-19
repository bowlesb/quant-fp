"""SYSTEM PROGRESS scorecard — Ben's six platform axes, computed read-only + persisted as a TIME SERIES.

Ben articulated six dimensions on which the platform improves over time and wants the TRAJECTORY legible at a
glance (not reconstructed each time). This module computes the CURRENT value of each axis from the existing
sources (no new compute, no schema change — read-only joins / manifest reads / a documented baseline / gh),
and ``scorecard_store.py`` persists a small headline snapshot each build so the panel can draw a sparkline.

The six axes (A-F):
  A · Features TRUSTED        — ``feature_trust.trust_state='TRUSTED'`` count + % of the live catalog.
  B · Features deployed       — the live bus ``BusSchema.from_registry()`` n_features + group count (the
                                deployed fingerprint set — what is actually live, matching the SYSTEM_LOG).
  C · Trust-process health    — ELIGIBLE (no open defect, advances next clean sweep) vs BLOCKED (open parity
                                defect) vs open-defect count — the lifecycle frontier.
  D · Latency bar->vector     — the documented end-to-end p50/p99 baseline (``docs/SIM_LATENCY_AUDIT.md``);
                                there is no live per-minute latency metric on disk, so the baseline IS the
                                source of truth (re-measured each latency cycle), parsed from its table.
  E · Raw-coverage            — bars/trades/quotes DEPTH (span days) + BREADTH (symbols/day) from the raw
                                manifests (reuses ``raw_coverage.build_raw_coverage`` — the same #144 read).
  F · Open issues             — open ``feature_parity_defect`` count + open PR count (gh) + quarantined-feature
                                count (a feature kept-but-untrusted under an open defect == the BLOCKED set).

A, C and the defect/quarantine side of F all fall out of the SAME single ``build_trust_frontier()`` DB read
(feature_trust x feature_parity_defect), so the scorecard pays one trust read, one manifest read, one doc
parse, and one cheap ``gh`` call — never re-querying what the frontier already computed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path

import scorecard_store
from feature_grid import build_trust_frontier
from raw_coverage import STORE_ROOT, build_raw_coverage

from quantlib.bus.schema import BusSchema

# Where the documented latency baseline lives. The Latency loop re-measures end-to-end bar->vector p50/p99
# into this audit each cycle; with no live per-minute latency series on disk it is the source of truth for
# axis D. Overridable for tests.
LATENCY_AUDIT_PATH = Path(os.environ.get("LATENCY_AUDIT_PATH", "/docs/SIM_LATENCY_AUDIT.md"))

# The repo root for the ``gh`` open-PR count (axis F). The dashboard container has no git checkout, so a
# missing/unauthenticated gh is tolerated (open_prs -> None) rather than failing the whole scorecard.
REPO_DIR = os.environ.get("SCORECARD_REPO_DIR", "/home/ben/quant-fp")

# A latency table row is "| <date> | <univ>/<shards> | <features> | <p50>ms | <p95>ms | **~<p99>ms** |".
# We pull the LAST such data row (the most recent re-measure) and read its p50 + p99 ms integers. Bold/tilde
# markup around the numbers is tolerated. Rows whose p50 cell is "—" (the historical 06-15 row) are skipped.
_LATENCY_ROW_RE = re.compile(
    r"^\|[^|]*\|[^|]*\|[^|]*\|\s*\**~?\s*([0-9]+)\s*ms\b[^|]*\|[^|]*\|\s*\**~?\s*([0-9]+)\s*ms\b",
    re.MULTILINE,
)


def _trusted_axis(frontier: dict[str, object]) -> dict[str, object]:
    """Axis A from the trust frontier: trusted count + % of the live catalog."""
    return {
        "value": int(frontier["n_trusted"]),
        "total": int(frontier["n_features"]),
        "pct": float(frontier["trusted_pct"]),
    }


def _deployed_axis() -> dict[str, object]:
    """Axis B: the live bus schema's feature + group count — the DEPLOYED fingerprint set (what is actually
    live), built straight from the registry the publishers/consumers share."""
    schema = BusSchema.from_registry()
    return {
        "value": schema.n_features,
        "groups": len(schema.group_names()),
        "fingerprint": f"0x{schema.fingerprint:016x}",
    }


def _process_health_axis(frontier: dict[str, object]) -> dict[str, object]:
    """Axis C from the trust frontier: the lifecycle frontier — eligible (no open defect, one clean sweep from
    trusted) vs blocked (open parity defect) vs the open-defect count, plus the projected trusted % (where
    trust lands if every eligible feature passes the next sweep)."""
    return {
        "eligible": int(frontier["n_eligible"]),
        "blocked": int(frontier["n_blocked"]),
        "open_defects": int(frontier["n_open_defects"]),
        "projected_trusted_pct": float(frontier["projected_trusted_pct"]),
    }


def parse_latency_baseline(path: Path | None = None) -> dict[str, object]:
    """Axis D: the documented end-to-end bar->vector p50/p99 from the latency audit's re-measure table.

    Reads the LAST end-to-end table row (the most recent re-measure) and returns its p50/p99 ms. Returns a
    null-valued reading (not an error) if the audit is missing or has no parseable row, so a doc-format change
    degrades this one axis instead of breaking the whole scorecard. ``path`` defaults to the module-level
    ``LATENCY_AUDIT_PATH`` read at CALL time (so a test/override of that module global takes effect)."""
    if path is None:
        path = LATENCY_AUDIT_PATH
    if not path.exists():
        return {"p50_ms": None, "p99_ms": None, "source": str(path), "available": False}
    text = path.read_text(encoding="utf-8")
    matches = _LATENCY_ROW_RE.findall(text)
    if not matches:
        return {"p50_ms": None, "p99_ms": None, "source": str(path), "available": False}
    p50, p99 = matches[-1]
    return {
        "p50_ms": int(p50),
        "p99_ms": int(p99),
        "budget_ms": 100,
        "source": str(path),
        "available": True,
    }


def _raw_coverage_axis(root: str = STORE_ROOT) -> dict[str, object]:
    """Axis E: per raw layer (bars / trades / quotes) DEPTH (span days, date range) + BREADTH (median
    symbols/day), summarized from the raw-coverage surface (reuses build_raw_coverage — the manifest read).
    """
    coverage = build_raw_coverage(root, days=0)  # full history; we only need the summary depth/breadth stats
    layers: dict[str, object] = {}
    for layer in coverage["layers"]:  # type: ignore[union-attr]
        layers[str(layer["tier"])] = {
            "earliest": layer["earliest"],
            "latest": layer["latest"],
            "span_days": int(layer["span_days"]),
            "n_symbols": int(layer["n_symbols"]),
            "median_symbols_per_day": float(layer["median_symbols_per_day"]),
        }
    return {
        "span_earliest": coverage["span_earliest"],
        "span_latest": coverage["span_latest"],
        "layers": layers,
    }


def open_pr_count(repo_dir: str = REPO_DIR) -> int | None:
    """Axis F (part): open PR count via ``gh pr list``. Returns None (not 0, not an error) if gh is
    unavailable/unauthenticated/times out — the dashboard container has no checkout, so this is best-effort and
    must not fail the scorecard."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "number"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return len(json.loads(result.stdout or "[]"))


def _open_issues_axis(frontier: dict[str, object], repo_dir: str = REPO_DIR) -> dict[str, object]:
    """Axis F: open parity-defect count + open PR count + quarantined-feature count. The defect count and the
    quarantined set both come from the frontier (a quarantined feature == kept-but-untrusted under an open
    parity defect == the BLOCKED set); only the open-PR count needs the external gh call."""
    return {
        "open_defects": int(frontier["n_open_defects"]),
        "open_prs": open_pr_count(repo_dir),
        "quarantined": int(frontier["n_blocked"]),
    }


def _snapshot_axes(axes: dict[str, object]) -> dict[str, object]:
    """The small headline-scalar snapshot of the full axes payload (what the time series persists + sparklines
    draw from). Keeps only the numbers a trend line needs, not the per-group/per-layer detail."""
    trusted = axes["A_trusted"]
    deployed = axes["B_deployed"]
    health = axes["C_process_health"]
    latency = axes["D_latency"]
    raw = axes["E_raw_coverage"]
    issues = axes["F_open_issues"]
    raw_layers = raw["layers"]  # type: ignore[index]
    return {
        "A_trusted": {"value": trusted["value"], "pct": trusted["pct"]},  # type: ignore[index]
        "B_deployed": {"value": deployed["value"], "groups": deployed["groups"]},  # type: ignore[index]
        "C_process_health": {
            "eligible": health["eligible"],  # type: ignore[index]
            "blocked": health["blocked"],  # type: ignore[index]
            "open_defects": health["open_defects"],  # type: ignore[index]
        },
        "D_latency": {"p50_ms": latency["p50_ms"], "p99_ms": latency["p99_ms"]},  # type: ignore[index]
        "E_raw_coverage": {
            tier: {
                "span_days": layer["span_days"],  # type: ignore[index]
                "median_symbols_per_day": layer["median_symbols_per_day"],  # type: ignore[index]
            }
            for tier, layer in raw_layers.items()  # type: ignore[union-attr]
        },
        "F_open_issues": {
            "open_defects": issues["open_defects"],  # type: ignore[index]
            "open_prs": issues["open_prs"],  # type: ignore[index]
            "quarantined": issues["quarantined"],  # type: ignore[index]
        },
    }


def build_scorecard(root: str = STORE_ROOT, repo_dir: str = REPO_DIR) -> dict[str, object]:
    """Compute all six axes (A-F) from the existing sources, read-only. Pays one trust-frontier DB read (A, C,
    F-defects/quarantine), one raw-manifest read (E), one latency-doc parse (D), and one cheap gh call (F-PRs).

    Returns ``{generated_at, axes: {A_trusted, B_deployed, C_process_health, D_latency, E_raw_coverage,
    F_open_issues}, snapshot}`` where ``snapshot`` is the headline-scalar form the time series persists. The
    caller (the endpoint / a Lead-loop helper) appends ``snapshot`` to ``scorecard_store`` so the panel draws a
    trajectory; building never writes (so a test / a read-only caller can build without side effects)."""
    frontier = build_trust_frontier()
    axes: dict[str, object] = {
        "A_trusted": _trusted_axis(frontier),
        "B_deployed": _deployed_axis(),
        "C_process_health": _process_health_axis(frontier),
        "D_latency": parse_latency_baseline(),
        "E_raw_coverage": _raw_coverage_axis(root),
        "F_open_issues": _open_issues_axis(frontier, repo_dir),
    }
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "axes": axes,
        "snapshot": _snapshot_axes(axes),
    }


class ScorecardCache:
    """Tiny TTL cache mirroring ``feature_grid.GridCache`` / ``raw_coverage.RawCoverageCache``. The scorecard's
    cost is the trust read + manifest read (~1-3s cold); a 60s TTL makes a busy refresh instant while staying
    fresh enough for a surface that only moves on a sweep / backfill / deploy. APPENDS a time-series snapshot
    on every fresh build (de-duped per UTC minute by the store), so the trajectory accrues as Ben watches it.
    """

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._view: dict[str, object] | None = None
        self._view_at: float = 0.0

    def scorecard(
        self, root: str = STORE_ROOT, repo_dir: str = REPO_DIR, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        if not force and self._view is not None and (now - self._view_at) <= self.ttl:
            return self._view
        view = build_scorecard(root, repo_dir)
        # Persist the headline snapshot for the time series. The store append is the CACHE's side effect, not
        # the pure builder's, so a read-only / test caller can build_scorecard() without writing.
        scorecard_store.append_snapshot(view["snapshot"])  # type: ignore[arg-type]
        self._view = view
        self._view_at = now
        return view


CACHE = ScorecardCache()
