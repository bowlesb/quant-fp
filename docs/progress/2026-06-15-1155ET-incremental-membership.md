# Autonomous cycle — 2026-06-15 ~11:55 ET (Mon)

## For the owner (nothing broken; one decision flagged)
- **System HEALTHY.** Healthcheck: 12 PASS / 2 WARN / 1 FAIL. The lone FAIL (`bar_to_vector_latency` ~60s)
  is the known **batch-path floor** (minute-close wait + Alpaca delivery), not a regression — it's exactly
  what P1 #1 (the fast path) exists to fix. The 2 WARNs are the empty validation ledger (P1 #2). No safe-fix
  was needed (capture Up, real SIP, 8/8 shards, freshness 2.7 min, coverage 82%, bias 33%).
- **No deploy / no restart this cycle.** The change advanced P1 #1 but is **library-only and inert on the
  live path** (the running worker uses `compute_reduction_batch`, not `IncrementalEngine`). Zero live risk.
- **One decision to be aware of (not urgent):** before the *fast slice* path can go live, the
  `DERIVE_SLICE=6 min` density assumption needs a parity-gated resolution for sparse symbols (gap > 6 min).
  Options written into the backlog item. This cycle deliberately validated with the gap-safe whole-buffer
  derive, so nothing live depends on it yet.

## What I advanced (P1 #1 — per-symbol fast/tick path)
Removed the **fixed-symbol-set blocker** that has kept the proven incremental accumulator off the live worker.

- The accumulator (`quantlib/features/incremental.py`) assumed **every symbol present every minute** and
  raised on any mismatch. Live capture delivers only a minute's **active** symbols (~4k of the 11.3k
  universe, churning minute to minute) — so the engine could not yet drive live compute.
- Now each minute's present rows are **aligned to a fixed session index** (the shard universe, a stable
  superset). Symbols absent this minute fold a **0 contribution** — provably identical to a missing bar in
  the batch (no row → no contribution) — and are **masked out of the OLS pairing** (`present=False → b=0`,
  which matters for the OBV cumulative slot whose running `y` stays finite even when the symbol is absent).
  A genuinely-new ticker raises `SymbolSetExpanded` → `step()` **re-seeds** from the buffer (the parity-safe
  daily-resync path) and continues.

### Parity is sacred — proven cell-for-cell, not assumed (`tests/test_fp_incremental_membership.py`)
1. `test_windowed_sum_absent_symbol_equals_missing_row` — the foundational identity: folding a 0 row for an
   absent symbol == omitting its row from `rust_windowed_sums`, across random per-minute dropout.
2. `test_incremental_step_matches_batch_under_membership_churn` — end-to-end over the **real declarative
   groups** with ~70% present each minute: incremental `step` == batch `compute_latest` for every symbol
   active at the mark.
All existing fixed-set incremental/emit/slice tests still pass (no regression). ruff clean.

Commit `513ee8c`, merged to `integration/converged` as `59e3f77`.

## Next step (left in the backlog for the next cycle)
Wire `IncrementalEngine` into `capture.process_bars` behind `FP_INCREMENTAL` (**default OFF**) for the
batched `ReductionGroup`s — one engine per `reduce_input` bucket held in `CaptureState`, seeded from the
universe at session start, `step_rust_unified` each minute in place of `compute_reduction_batch` — plus a
runtime parity self-check (`FP_INCREMENTAL_PARITY`) emitting a divergence metric. Default OFF = no deploy
risk; flip only after the self-check runs clean against live. Then: emit-on-arrival (the real latency win)
and resolve the slice-derive-depth-under-gaps constraint above.
