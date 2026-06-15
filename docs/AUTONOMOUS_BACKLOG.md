# Autonomous Backlog — the loop's goal-driven queue

The autonomous loop reads THIS file every cycle, maintains the live system, then advances the
single highest-priority unchecked improvement item. It is the operational expression of our goals
(parity-true real-time features × ~10k equities, sub-minute bet latency, validated trust, edge hunt).

**Loop discipline each cycle:** MAINTAIN (healthcheck + safe-fix) → ADVANCE (top unchecked item) →
RECORD (commit + progress note) → REFILL (if the improvement queue is thin, append new high-value
items so the loop NEVER idles). Mark items `[x]` when done, with the commit/branch in the line.

---

## P0 — Maintenance (every cycle, recurring)
- [ ] Run `docker exec feature-computer python -m quantlib.ops.healthcheck`; safe-fix dead/wedged containers and empty universe only; escalate anything else at the TOP of the note.
- [ ] Confirm collection: capture Up, SIP ESTABLISHED, freshness < 3 min, coverage growing, no crash loops.

## P1 — Improvement (ordered; do the top unchecked one)
- [ ] **Per-symbol fast/tick path on the REAL feed** — the only route to sub-minute bet latency. Unify the real-feed entrypoint with the fast/tick path so a symbol's vector is emitted as its bar arrives, not after the minute closes. Measure before/after on the bar→vector Grafana dashboard. PARITY MUST HOLD (live == backfill).
- [ ] **Parity validation ledger live** — run the after-market cycle so `feature_validation_day` / `feature_trust` populate; certify per-feature trust grades; gate training on certified features. (Crown jewel — empty today.)
- [ ] **Nightly re-seed + relaunch automation** — the capture is launched with a HARDCODED `2026-06-15` date arg; it will NOT roll to tomorrow. Wire a nightly job: re-seed universe for the new session + relaunch capture with the new date. Required for true autonomous daily operation.
- [ ] **Prometheus stale-rule cleanup** — `ingestor_alerts.yml` still references the retired `ingestor-coverage` job (now no series). Prune or repoint.
- [ ] **Latency drill-down hardening** — once the fast path lands, re-key the `bar_to_vector_latency` thresholds to the new architecture; confirm per-ticker drill-down still meaningful.

## P2 — Breadth & features (do after P1 is clear, or interleave when P1 is blocked)
- [ ] Port remaining high-value features from the old codebase (continue the FEATURE_PORT_PLAN).
- [ ] Real ADV-dollar ranking to replace the alphabetical/placeholder universe ordering once backfill history accrues.
- [ ] EDGAR real-time ingestion feature (filings event-clock).

## P3 — Research (non-blocking, when compute is idle)
- [ ] DL foundation-model prototyping on the clean panel (3090).

---

## How to REFILL (never idle)
If every P1/P2 item is checked or blocked, append new items drawn from: gaps the healthcheck WARNs at,
new failure modes seen in `docs/progress/`, feature ideas, parity edge cases, latency reductions, and
the Definition-of-Done criteria. Half the job is innovation — generate, don't stall.

## Log
- 2026-06-15: universe 3,070→11,336 (bias removed); dual latency metric + per-ticker drill-down + Grafana; continuous healthcheck engine + 5-min tripwire + MAINTENANCE_PROTOCOL.md. (integration/converged)
