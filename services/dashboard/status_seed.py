"""Seed / refresh the CURRENT hour's row of the hourly status dashboard — the Lead conductor loop's write
entry point (docs/OPERATING_MODEL.md §"The hourly status dashboard").

The Lead's loop runs this each cycle on the HOST: it synthesizes one ``{progress, blockers}`` cell per
workstream from that workstream's ledger and calls :func:`status_grid.write_row`, which appends (or replaces,
idempotently) the current UTC hour's row in the append-only store the dashboard serves. Ben's reaction on the
row is preserved across re-synthesis.

THE WRITE PATH FOR THE LEAD (two equivalent options):

  1. Pass the synthesized cells directly::

        from status_seed import write_status_cells
        write_status_cells({
            "Latency":  {"progress": "...", "blockers": ""},
            "Parity":   {"progress": "...", "blockers": ""},
            ... one entry per workstream (omitted workstreams render blank) ...
        })

  2. Or run this module to seed the current hour from the static example below (a first-load placeholder)::

        STATUS_STORE_PATH=~/.quant-ops/status_dashboard.json python3 status_seed.py

The cells here are a CONCISE synthesis of each workstream's ledger tail at seed time — Progress is the recent
status, Blockers is ONLY a real attention-worthy problem the Lead can't move past without Ben (most hours:
none). The Lead OVERWRITES these every cycle with a fresh synthesis; this static set just makes the tab
non-empty on first load.
"""

from __future__ import annotations

from status_grid import write_row

# A concise synthesis of each workstream's ledger tail at seed time (2026-06-21 ~23:30Z). Progress = recent
# status; Blockers = ONLY a real Ben-needed problem (most: none). The Lead replaces this each cycle.
SEED_CELLS: dict[str, dict[str, str]] = {
    "Latency": {
        "progress": "Regression-check CLEAN: e2e p50=262ms / p99=304ms, both well under ceilings; "
        "no group reordered/spiked. Quiet end-state.",
        "blockers": "",
    },
    "Parity": {
        "progress": "Backlog exhausted of fixable divergence: bar groups clean, incremental gate suite GREEN "
        "(27 passed), fp 728 unchanged. FP_INCREMENTAL for the 15 is evidenced + safe.",
        "blockers": "56 open tick/quote parity items are a FP_TICK_SYMBOLS live-coverage artifact (only ~24 "
        "syms tick-subscribed) — unverifiable until tick subscription widens (Ben/Lead infra).",
    },
    "Modeller": {
        "progress": "Price-only confirmed null; harness fixed + experiment designed. Option-IV scoped "
        "(Alpaca snapshot-only, no history).",
        "blockers": "Gated on the order-flow/quote panel landing (quote backfill, DataIntegrity lane) — no "
        "new substrate to model until then.",
    },
    "DataIntegrity": {
        "progress": "Raw tape CURRENT — bars/trades/quotes all latest 2026-06-18 (06-19=Juneteenth, no gap); "
        "no non-holiday gaps. Quote-breadth backfill tooling dry-run-verified.",
        "blockers": "",
    },
    "Maintainer": {
        "progress": "Clean survey on origin/main; #380 SHIPPED (cron-fabric rebuild runbook in "
        "docs/OPERATIONS.md). No net-positive docs cleanup owed → clean DROP.",
        "blockers": "",
    },
    "Warehouse": {
        "progress": "Building this hourly-status dashboard (axis E): 4th 'Status' tab + /api/status-grid read "
        "+ reaction POST, persisted to ~/.quant-ops/status_dashboard.json. #368 News tab live.",
        "blockers": "",
    },
    "CD": {
        "progress": "CI/CD gate on main (#348/#354/#356): coverage-honest, env-aware, xdist-fast (~2-3min/PR). "
        "Daemon grading-only.",
        "blockers": "Before arming Phase-2 auto-merge: daemon-checkout must run from current-main (not the "
        "pinned fc tree) + collection-ERROR flake recovery. Neither urgent.",
    },
    "Lead": {
        "progress": "Overnight queue clear, 0 open PRs; 9 PRs merged gate-verified, fp 728 untouched. Strategy "
        "redeploy verified (overnight-beta 195%->0.6% CPU). READINESS ledger current.",
        "blockers": "Gated Monday clicks (Ben/market-closed): FP_INCREMENTAL flip (15 armed), "
        "FP_SWING_STATEFUL flip, Rust image roll, crypto-capture relaunch 694->728.",
    },
}


def write_status_cells(cells: dict[str, dict[str, str]], hour: str | None = None) -> dict[str, object]:
    """Synthesize-and-write one hour's row. ``cells`` maps workstream -> {progress, blockers}; missing
    workstreams render blank. Defaults to the current UTC hour. Returns the written row. This is the function
    the Lead's conductor loop calls each cycle with its fresh ledger synthesis."""
    return write_row(cells, hour=hour)


def main() -> None:
    """Seed the current hour's row from :data:`SEED_CELLS` so the Status tab is non-empty on first load."""
    row = write_status_cells(SEED_CELLS)
    print(f"seeded status row hour={row['hour']} workstreams={len(SEED_CELLS)}")  # noqa: T201


if __name__ == "__main__":
    main()
