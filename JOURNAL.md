# Experiment & Decision Journal

Append-only. Newest entries at the top. Experiments record: hypothesis, config
hash, out-of-sample result, verdict, next step. Decisions record what changed and
why.

---

## 2026-06-10 — Project start

- Decision: build fresh at `~/quant`, ignoring prior repos (Edgar,
  automated-day-trading) per Ben's explicit request.
- Decision: committed approach = cross-sectional short-horizon ML ranking on a
  ~1,000-symbol liquid universe; LightGBM; paper-first with statistical gates.
  Rationale in `ARCHITECTURE.md`.
- Started Phase 0 foundation.
- Tore down legacy Edgar Docker stack (containers/networks removed, data volumes
  preserved) to free the single Alpaca data websocket. Old code/data left on disk.
- Ingestor live on SIP feed; verified bars_1m persistence end-to-end for 10 symbols.
  Confirmed the account already has SIP (Algo Trader Plus) entitlement.
- Executor + reconciliation live; hello-world paper order verified; reconciliation
  caught a stray DLTR paper position from the old system.
- Reset paper account to clean baseline (flattened positions, cancelled orders,
  truncated test order/fill/recon rows). Ben approved.
- Scheduler live computing data_quality_daily coverage; dashboard shows it.
- Phase 0 service set complete: timescaledb, ingestor, executor, scheduler,
  dashboard, prometheus, grafana. Remaining for gate = clean-days accumulation +
  reboot-survival check.
- Built shared `quantlib` aggregation library (parity cornerstone) with a
  live-vs-batch parity test; extended ingestor to trades/quotes via quantlib
  (monorepo build context). Verified rich, sane aggregates landing live.
- Wrote docs/RESEARCH.md: 40-item ML-approaches backlog organized by ring and a
  first experiment wave to exercise the full gauntlet once Phase 2/3 infra exists.
