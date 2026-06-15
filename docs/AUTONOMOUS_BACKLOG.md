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
  - **2026-06-15 increment (513ee8c / merge 59e3f77):** removed the fixed-symbol-set blocker — `IncrementalEngine` now handles a FLUCTUATING active set (live delivers ~4k of 11.3k symbols/minute, churning). Absent symbols fold a 0 contribution (== a missing bar in the batch) and are masked out of the OLS pairing; a genuinely-new ticker raises `SymbolSetExpanded` → `step()` re-seeds (parity-safe resync). Proven cell-for-cell vs `rust_windowed_sums` and vs batch `compute_latest` under churn (`tests/test_fp_incremental_membership.py`). Library-only, inert on live path, no deploy.
  - **NEXT STEP (next cycle):** wire `IncrementalEngine` into `capture.process_bars` behind `FP_INCREMENTAL` (default OFF) for the batched `ReductionGroup`s — hold one engine per `reduce_input` bucket in `CaptureState`, seed from the universe at session start, `step_rust_unified` each minute instead of `compute_reduction_batch`. Add a runtime parity self-check (`FP_INCREMENTAL_PARITY`) comparing incremental vs batch each minute → a metric. Keep default OFF so no deploy risk; flip only after the self-check runs clean live. THEN tackle emit-on-arrival (the actual latency win) + the open slice-derive constraint below.
  - **OPEN PARITY CONSTRAINT (must resolve before the fast SLICE path goes live):** `DERIVE_SLICE=6` min assumes active symbols are dense; a present symbol whose prior bar is >6 min back would slice-derive a wrong short-lag value (this cycle validated membership with the gap-safe whole-buffer derive, `slice_derive=False`). Options for next owner/cycle: per-symbol variable slice to reach the last bar, recompute the rare sparse symbols whole-buffer, or null/skip a symbol with a stale derive (don't bet on it). DO NOT ship the slice fast-path live until this is parity-gated.
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
- 2026-06-15 ~11:40 ET: P1 #1 increment — `IncrementalEngine` fluctuating-symbol-set support (the live-integration blocker), parity-proven under membership churn. (513ee8c → merge 59e3f77, integration/converged)
