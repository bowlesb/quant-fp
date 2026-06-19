# System progress scorecard

`/scorecard` (page) + `/api/scorecard` (current) + `/api/scorecard/history` (time series).

Ben articulated six dimensions on which the platform improves over time and wants the **trajectory** legible
at a glance — not reconstructed each time. The scorecard turns that mental model into the thing he looks at:
each axis's current value **and** a sparkline of where it has been.

Everything is computed **read-only** from the existing sources — no new compute, no DB schema change. The
time series is persisted with the **same append-only JSON-snapshot pattern as the status board**
(`scorecard_store.py` mirrors `status_store.py`), under `~/.quant-ops/scorecard_snapshots.json`.

## The six axes

| axis | reads from | headline |
|------|-----------|----------|
| **A · Features trusted** | `feature_trust.trust_state='TRUSTED'` (via `build_trust_frontier`) | trusted count + % of the live catalog |
| **B · Features deployed** | `BusSchema.from_registry()` — the live bus schema | n_features + group count + fingerprint (the deployed set) |
| **C · Trust-process health** | the trust frontier (`feature_trust` × open `feature_parity_defect`) | eligible (1 clean sweep from trusted) vs blocked vs open defects + projected trusted % |
| **D · Latency bar→vector** | `docs/SIM_LATENCY_AUDIT.md` re-measure table | end-to-end p50/p99 ms (documented baseline; budget 100ms) |
| **E · Raw-tape coverage** | the raw manifests (reuses `raw_coverage.build_raw_coverage`) | per-layer depth (span days) + breadth (median symbols/day) |
| **F · Open issues** | open `feature_parity_defect` + `gh pr list` + the blocked (quarantined) set | open defects + open PRs + quarantined features |

A, C, and the defect/quarantine side of F all fall out of the **single** `build_trust_frontier()` DB read, so
the whole scorecard pays one trust read, one manifest read, one doc parse, and one cheap `gh` call.

### Notes / honest caveats
- **D** has no live per-minute latency metric on disk, so the documented audit baseline (re-measured each
  Latency cycle) **is** the source of truth. The parser reads the LAST end-to-end table row; if the audit's
  table format changes, axis D reports `available: false` rather than failing the whole scorecard.
- **F · open PRs** uses `gh`, which the dashboard container has no checkout for → it reports `null` there
  (best-effort, never fails the build). A host-side caller (the Lead loop) with a checkout gets the real count.
- **F · quarantined** == the BLOCKED set: a feature kept-but-untrusted under an open parity defect (the
  quarantine-and-investigate policy), so it equals `C.blocked`.

## Time series

Each time the scorecard is built **through the cache** it appends a small headline-scalar snapshot to the
append-only store (de-duped per UTC minute, so a busy refresh does not stack duplicate points). The page's
sparklines draw from `/api/scorecard/history` (oldest-first). The pure `build_scorecard()` never writes, so a
read-only / test caller can compute without side effects — the append is the cache's side effect.

A snapshot::

    {
      "ts": "2026-06-19T16:00:00Z",
      "axes": {
        "A_trusted":        {"value": 106, "pct": 15.3},
        "B_deployed":       {"value": 694, "groups": 56},
        "C_process_health": {"eligible": 532, "blocked": 56, "open_defects": 56},
        "D_latency":        {"p50_ms": 401, "p99_ms": 761},
        "E_raw_coverage":   {"bars": {"span_days": 3819, "median_symbols_per_day": 4474.5}, ...},
        "F_open_issues":    {"open_defects": 56, "open_prs": null, "quarantined": 56}
      }
    }

## API shapes

`GET /api/scorecard` (`?refresh=1` bypasses the 60s TTL cache and writes a fresh snapshot):

    {generated_at, axes: {A_trusted, B_deployed, C_process_health, D_latency, E_raw_coverage, F_open_issues},
     snapshot}

`GET /api/scorecard/history` → `[{ts, axes: {...headline scalars...}}, ...]` (oldest-first).

## Deploy wiring (docker-compose)

The dashboard service gains:
- `SCORECARD_STORE_PATH: /quant-ops/scorecard_snapshots.json` — the time series on the read-write
  `~/.quant-ops` mount (same as `STATUS_STORE_PATH`).
- `./docs/SIM_LATENCY_AUDIT.md:/docs/SIM_LATENCY_AUDIT.md:ro` — so axis D can parse the baseline.

The panel appears on the live `:8088` only after the dashboard image is rebuilt against `main` (the Lead
sequences the rebuild). New `.py` files are auto-included via the Dockerfile's `COPY services/dashboard/*.py`.
