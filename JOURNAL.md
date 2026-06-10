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
- Freed 2.1TB on SSD (deleted regenerable carved files after proving 15/25
  byte-identical re-extraction from sdb; kept curated extracts + recovery scripts;
  sdb/sda untouched). SSD now 2.6TB free; backfill unblocked.
- Built universe construction (quantlib.universe + scheduler): screened 12,722
  tradable equities, selected exactly 1,000 most-liquid (price>$5, ADV$>$10M;
  cut at ~$161M ADV) into universe_membership for the day. 8 tests pass.
- Built backfiller (REST bars -> source='backfill') + validate-bars gate.
  FINDING (2026-06-10): streamed vs same-day REST bars match 99.76% on OHLC,
  95% incl. volume; all mismatches are tiny late-corrections (volume ±1 print,
  sub-cent closes). Real-time bars are built just before late prints settle, so
  REST (post-consolidation) differs slightly. IMPLICATION: treat source='backfill'
  as authoritative for training/features; source='stream' is what we trade on live.
  This is a real, bounded source of train/serve skew to track — exactly why the
  schema keeps both sources. Official gate number should be measured on a fully
  settled prior day, not same-day.
- Aggregate parity validated on real data: trade_agg 95.2% within 2% (mean rel
  diff 0.7%), quote_agg spread 100% over 63 overlapping minutes.
- Scaled live bar ingestion to the full universe: confirmed 951 distinct symbols
  streaming bars in a 90s window. Trades/quotes kept on the liquid 10 subset.
- Phase 2: built v1 feature engine (quantlib/features, 18 features) + historical
  feature-store builder + live feature-computer, sharing quantlib/featurestore.
  FEATURE replay-equivalence = 100% identical (stream vs historical recompute).
  Feature-level train/serve skew eliminated by construction. Cleanup: removed dead
  services/status scaffold + unused webhook config.
- Phase 3 prep: forward-return cross-sectional labels (quantlib/labels). Built
  universe features (30,970) + labels for today; created training_data view
  (feature_vectors JOIN labels). Panel currently lopsided: broad breadth (998
  symbols) at ~1-2 timestamps + deep time on the 10-symbol subset, because
  full-universe stream bars only span ~31min so far. Real panel needs the 7-day
  backfill built on source='backfill'.
- PRIORITY-E SANITY LOOK (NOT an edge claim): per-feature Pearson corr vs fwd_30m
  on n=2339 rows from ~51 same-day timestamps. Recent-return features correlate
  NEGATIVELY with forward return (ret_15m -0.27, rel_ret_30m -0.23, ret_30m -0.15)
  = short-horizon reversal signature, directionally as hoped. vol_30m +0.36 likely
  a within-cross-section volatility artifact. CAVEAT: single day, overlapping/
  autocorrelated obs, one regime, Pearson not rank-IC — statistically meaningless;
  pipeline-sanity only. Defer real IC to multi-day universe panel from backfill.
