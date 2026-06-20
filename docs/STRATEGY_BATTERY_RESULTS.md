# Strategy-Battery — EMPIRICAL RESULTS (the executed working version)

Status: **EXECUTED + MEASURED.** This is "see how well it works" — the headline numbers from running
the working battery (`quantlib/battery/` + `quantlib/strategy_core/`) on the REAL store. The companion
design is `docs/STRATEGY_EXECUTION_ABSTRACTION.md`; the portability invariant is
`docs/STRATEGY_BATTERY_PORTABILITY.md`.

Target scope (stated up front, the acceptance basis): **liquid-1500 universe × ~6 months daily**
(2025-12-01 .. 2026-06-17), raw fast path (the default), on the cached daily panel. Image: `fp-dev`
(now bakes lightgbm + libgomp1). Reproduce: `python /tmp/run_anticheat.py` (anti-cheat + budget),
`python /tmp/run_faithfulness.py {baseline|overnight}` (faithfulness).

---

## 1. PERF BUDGET (REQ-P1) — PASS

| run | total | panel load | eval | cells |
|---|---|---|---|---|
| full default battery, raw fast path (liquid-1500 × 6mo, cached panel) | **36.2s** | 19.9s | 16.2s | 16 |
| GBM deeper mode (same scope) | ~71s | ~20s | ~51s | 16 |

**36.2s < 60s — in budget**, with the FAITHFUL (non-hack) booking path (the battery books P&L through
`BacktestExecutor.run_vectorized`, the columnar per-name-half-spread cost model — not a per-event
loop, not a fidelity shortcut). GBM is the opt-in deeper mode (it fits a model per fold). The
first-ever panel build (uncached 18-month reduce) is a one-time cost, cached thereafter.

---

## 2. ANTI-CHEAT SELF-PROOFS (REQ-A1) — ALL PASS

"How do we know we're not fooling ourselves." Each proof controls ONLY the feature and runs it through
the production machinery (real overnight labels, real per-name cost, walk-forward folds, BY-FDR).
Measured on the liquid-1500 × 6mo daily panel (166,226 rows, 1,490 symbols):

| proof | result | reading |
|---|---|---|
| **noise** (pure-noise feature) | **PASS** | IC −0.00246, NW_t −0.491, BY_p 0.688, verdict FAIL, survived=False. Random noise does NOT earn edge; leaderboard stays empty. |
| **planted** (feature = 5×forward-label + noise) | **PASS** | IC +0.964, edge_vs_shuffle +0.963, NW_t +492 → detected. The harness CAN see a real edge that exists (not too blunt). |
| **shuffle** (within-ts label shuffle on the planted feature) | **PASS** | real IC +0.964, shuffle-canary IC **+0.00091** (~0). The leakage arbiter collapses to zero. |
| **look_ahead** (a peek: feature shifted FORWARD 1 bar) | **PASS** | honest_edge −0.0113, peek_edge −0.0065, contained (<0.05). The walk-forward purge removes the look-ahead — peeking does not manufacture a surviving edge. |
| **tradeable_entry** | **PASS** | earliest entry 19:59 UTC (the overnight panel enters at the 15:59 ET close, ≥09:35 ET; the intraday panel samples from 13:35 UTC == 09:35 ET). Never the 09:30 print. |

These are codified in `quantlib/battery/anti_cheat.py` (`run_all`) so they re-run on any panel.

---

## 3. FAITHFULNESS — reproduce the trusted hand-rolled verdicts (the keystone)

### 3.1 Trusted-substrate-baseline (intraday 30m/60m) → NULL — REPRODUCED (verdict)
GBM over the trusted intraday cohort, 2026-05-15..06-17 (the available backfill window):

| horizon | IC | shuffle | edge_vs_shuffle | NW t | breakeven_bps | verdict |
|---|---|---|---|---|---|---|
| 30m | +0.01360 | +0.00046 | +0.01315 | **+1.438** | 3.90 | **FAIL (null)** |
| 60m | +0.00210 | −0.00323 | +0.00533 | **+0.166** | 6.78 | **FAIL (null)** |

The published baseline verdict was an honest NULL (NW\|t\| ~0.1, no tradeable edge). The battery
reproduces the **null verdict**: both cells FAIL the gate — NW\|t\| well below 2.0 and breakeven below
realistic cost. The exact IC differs from the published −0.0014 because this run uses a DIFFERENT date
window (the published run's 20-day window vs the currently-available backfill) and a representative
(not byte-identical) feature subset; the load-bearing result — *no tradeable edge, gate not cleared* —
matches. (HONEST NOTE: a perfect numeric reproduction needs the exact original panel; the verdict-level
reproduction is what certifies the abstraction is faithful.)

### 3.2 laneC overnight 1d: full-univ HIT → liquid-1500 COLLAPSE
<!-- RESULTS PENDING — the 18-month full-universe deep-panel build is running; numbers filled on completion. -->
Expected (the published verdict to reproduce): full-universe 1d HIT (IC ≈ 0.035, NW t ≈ 3.89,
breakeven ≈ 22bps) that COLLAPSES on the liquid-1500 (IC ≈ 0.011, edge ≈ +0.007, NW t ≈ 1.20,
breakeven ≈ 4.12). The `by_stratum` liquidity breakdown should show the edge concentrating in the
illiquid tail (the trap #1 signature).

---

## 4. PORTABILITY worked example (REQ-D1/X1/S1) — PASS

The SAME `decide()` + the SAME `StrategyState` model run through `BacktestExecutor` (pretend fills
over the panel) AND `PaperExecutorStub` (live-shaped, idempotent on `client_order_id`) produce
**identical positions** — the live container is a thin harness with NO duplicated decision logic
(`tests/test_execution_state.py::test_worked_example_backtest_vs_paper_thin_harness`).

Also pinned: `StrategyState.apply_fill` transitions (open / weighted-avg / realize-P&L / partial-then-
complete), the append-only fill-ledger recompute == positions (REQ-S2), the BacktestExecutor sub-$1
REJECT (faithful to Alpaca, REQ-X2/X3), and PaperExecutor idempotency (REQ-X4).

---

## 5. Test inventory (43 green on fp-dev)

- `test_battery.py` (18): SanityReport guards, both nulls, per-name cost + curve, BY-FDR, verdict tree,
  synthetic null + signal recovery, winsor.
- `test_strategy_core.py` (11): the decide() panel-vs-bus parity, batch-vs-per-event equivalence,
  executor-swap parity, PanelFeed replay, Runner.
- `test_execution_state.py` (6): StrategyState transitions, ledger recompute, executor reject,
  idempotency, the backtest-vs-paper worked example.
- `test_backtest.py` (8): the UNCHANGED discipline core (proves the wrap didn't break it).

---

## 6. Honest notes / flags

- **No fidelity hack taken for speed (REQ-P2).** The 36s budget is met with the columnar faithful
  booking path. The speed-vs-fidelity tension is real only for PATH-DEPENDENT archetypes
  (triple-barrier/streak) — those are Phase-1 (the shared `quant_tick` Rust kernel both sides call); if
  one ever needs intra-bar fill sequencing that neither vectorizes nor fits the kernel, that is the
  flagged go/no-go (design R1), not a silent fork.
- **Faithfulness is verdict-level, not 4th-decimal.** Reproducing the trusted VERDICT (null vs
  HIT→collapse) on the available data certifies the abstraction; exact numeric match needs the original
  panels and is not the bar.
- **The real `PaperExecutor`/`LiveExecutor` (alpaca-py) is the next step.** This PR ships the faithful
  STUB (idempotent, Alpaca-shaped Fill/lifecycle) that proves the seam; the real broker wiring +
  reconciliation + restart-safety integration tests are the follow-on (designed in the abstraction doc).
