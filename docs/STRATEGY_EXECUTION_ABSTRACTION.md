# Strategy Execution Abstraction — PLAN, REQUIREMENTS & TEST STRATEGY

Status: **PLAN for adversarial audit** (NO implementation — the build is gated on audits passing).
Author: platform engineer, cycle 2026-06-19. Reviewers: independent adversarial auditors (fanned out
by the Lead) + Ben.

This is a **plan + requirements + test strategy**, written to be attacked: every requirement is
falsifiable and maps to a test that would catch its violation. It is deliberately at the CONTRACT
level (interfaces + behavioral guarantees), not implementation. The purpose is to let auditors find
holes BEFORE any code is written.

Ben's anti-goal (the failure this plan must prevent): *"a beautiful experiment harness that works
ONLY in an experiment setting and has a million hacks that reduce reliability/trustworthiness once we
actually deploy."* The two parts he tried before and found very hard: **STATE** and **PRETEND-VS-REAL
EXECUTION**. This plan is grounded in studying his prior attempt
(`/home/ben/automated-day-tracking-claude/rust_trade_executor/`) so we design against the walls he
already hit (§3).

---

## 1. Requirements (the production-real invariants — each is falsifiable)

Numbered so audits + tests can cite them. Each REQ has an owning test in §6.

**Decision parity**
- **REQ-D1 (single implementation).** A strategy's decision logic is written ONCE. It is *execution-
  agnostic* and *speed-agnostic*: it yields identical decisions whether applied as a fast batch sweep
  over a historical panel (backtest) or per-event over the live bus (production). There must be NO
  second implementation of the same decision (no "fast-for-backtest" + "slow-for-live").
- **REQ-D2 (purity).** `decide()` is a pure function of (feature cross-section, strategy state). No
  I/O, no wall-clock, no RNG. Same inputs → same outputs, always.
- **REQ-D3 (columnar-first).** `decide()` is expressible as a columnar (Polars/NumPy) operation so the
  batch path vectorizes; inherently-sequential logic (triple-barrier/streak) is the ONLY exception and
  is served by a single shared `quant_tick` Rust kernel both paths call (never a harness-only kernel).

**Execution fidelity**
- **REQ-X1 (one executor contract).** There is ONE `Executor` interface taking ONE `OrderIntent` and
  producing `Fill`s. `BacktestExecutor` (pretend), `PaperExecutor`/`LiveExecutor` (real Alpaca, paper
  now) all satisfy it.
- **REQ-X2 (Alpaca-faithful simulation).** `BacktestExecutor` simulates what Alpaca *actually does*:
  fills at the tradeable price (≥09:35 / the correct auction print for the TIF), charges the per-name
  half-spread + slippage, models partial fills and rejects. Its `Fill`/order-state outputs must match
  `PaperExecutor`'s on scripted scenarios (a conformance test, REQ-X1/§6.2).
- **REQ-X3 (full order lifecycle).** Both executors handle the full Alpaca lifecycle:
  `NEW/PENDING/ACCEPTED/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/EXPIRED` — partials and rejects are
  first-class, never time-outs (the prior repo's L2 wall).
- **REQ-X4 (idempotency).** Every order carries a deterministic `client_order_id`; submitting the same
  intent twice is a no-op at the broker and a single state entry (prior repo's L5 wall).
- **REQ-X5 (real-broker realities).** The live executor handles rejects (surfaced, not swallowed),
  rate limits (bounded backoff), and never logs secrets.

**State**
- **REQ-S1 (one typed model).** ONE typed `StrategyState` (positions, pending orders, realized P&L,
  typed per-strategy counters — streak/persistence/trailing-stop). The SAME model in backtest and
  live; the strategy reads/writes the same typed fields either way.
- **REQ-S2 (single durable source of truth).** Live state persists to ONE durable store (the
  strategy's Postgres schema) with an append-only fill ledger from which positions are recomputable.
  No "dynamic state only in a volatile side-store" (prior repo's L3 wall).
- **REQ-S3 (broker reconciliation).** The broker is the source of truth for positions. Reconciliation
  runs at startup AND periodically; on mismatch the broker wins; large drift ALERTS (never silently
  auto-fixes) (prior repo's L4 wall).
- **REQ-S4 (restart-safe).** A restart recovers state and never double-trades, for every hard case:
  mid-order (fill unknown), orphaned stop (filled while down), partial fill across restart.

**Budget without hacks**
- **REQ-P1 (laser-quick).** A typical `evaluate_features` battery run completes in <30–60s wall-clock
  on the stated target scope (liquid-universe slice + ~1yr) — MEASURED.
- **REQ-P2 (no fidelity hack for speed).** REQ-P1 is met WITHOUT a backtest-only fast-but-unfaithful
  path that violates REQ-X2/REQ-D1. If speed ever appears to require such a fork, it is escalated as a
  go/no-go (§7 R1), not silently taken.

**Discipline / anti-self-deception (Ben's central concern — see §6.3)**
- **REQ-A1.** The harness's own self-proofs (shuffle canary, predict-zero, known-null, planted-edge,
  look-ahead/purge, tradeable-entry, multiple-comparisons correction, reproduce-trusted-verdicts) are
  REQUIRED tests that must pass; if any fails, the harness is considered to be fooling itself and is
  not trusted.

---

## 2. The abstraction (contract level — signatures + behavioral contracts, NOT implementation)

Seven contracts. Each is a small protocol; the *behavior* (the columns below), not the code, is what
audits attack.

| Contract | Shape (signature) | Behavioral guarantee (what an audit checks) |
|---|---|---|
| **`Strategy.decide`** | `decide(cs: CrossSection, state: StrategyState) -> list[OrderIntent]` | Pure (REQ-D2); columnar-expressible (REQ-D3); identical batch vs per-event (REQ-D1). |
| **`OrderIntent`** | frozen record: `client_order_id, symbol, side, qty\|notional, type, limit/stop, tif, bracket?, reason` | Deterministic `client_order_id` (REQ-X4); broker-agnostic (no Alpaca types leak in). |
| **`Fill`** | frozen event: `client_order_id, symbol, side, filled_qty(cumulative), avg_price, state, filled_at, broker_order_id?` | Models partials (cumulative qty) + the full lifecycle `state` (REQ-X3); real broker `filled_at` (prior L6). |
| **`Executor`** | `submit(intent)->OrderState; poll(coid)->Fill; cancel(coid); positions()->{sym:Position}; account()->Acct` | Idempotent submit (REQ-X4); `BacktestExecutor` faithful to `PaperExecutor` (REQ-X2); `positions()` = broker truth live (REQ-S3). |
| **`DataFeed`** | `events() -> Iterator[(CrossSection, ts)]` | `PanelFeed` (replay panel) and `BusFeed` (live bus) emit the SAME event shape. |
| **`Clock`** | `now() -> datetime` | `SimClock` panel-driven (reproducible, no wall-clock); `RealClock` wall-clock + Alpaca market clock. |
| **`StrategyState` / `StateStore`** | state: `positions, pending, realized_pnl, counters`; store: `load(id); save(state); append_fill(fill)` | One typed model (REQ-S1); `MemoryStateStore` (sim) / `PgStateStore` (live, durable SoT, append-only ledger, REQ-S2). |
| **`Runner`** | ties `{decide, StateStore, Executor, DataFeed, Clock}` | The ONE loop both backtest and live share; only the four components swap. |

The TIF set (`DAY/CLS(MOC)/OPG(MOO)/GTC`), order types (`MARKET/LIMIT/STOP/STOP_LIMIT`), bracket/OCO,
and the partial-fill policy (`ACCEPT_RESIZE | CANCEL_REMAINDER | RETRY`, per-strategy) are enumerated,
closed sets — no open-ended "TODO action" (prior L6). Reconciliation is a named operation
`reconcile(state, executor) -> ReconcileReport` (broker wins; drift audited).

Layering rule (architectural invariant an audit can check): `quantlib/strategy_core/` is
self-contained; `quantlib/battery/` and the live containers DEPEND on it; nothing in `strategy_core`
imports `battery` or a deployment package. One home for the shared decision+execution+state logic.

---

## 3. Prior-repo lessons (what Ben tried + where it broke)

His `rust_trade_executor` is a thoughtful attempt; the trait-SHAPES are right and we keep them. It
broke on the production-fidelity of STATE and PRETEND-VS-REAL — the exact two hard parts.

**Shapes built (keep):** `TradeState` trait (`state.rs:10`, `MemoryState` sim / `RedisState` live —
policies unaware which); `BarDelivery` trait (= our `DataFeed`); `Policy.evaluate()` runs in both sim
and live (= our `decide()`). `alpaca.rs` already had `get_positions`, bracket/OTO, `client_order_id`.

**The 6 walls (file:line evidence; each maps to a REQ that closes it):**

| # | Wall | Evidence | Closed by |
|---|---|---|---|
| L1 | Sim NEVER placed orders — `label_gen.rs`/`simulate.rs` run `evaluate()` but never `enter_trade()`; only live `initiator.rs` does → backtest≠live | offline bins vs `initiator.rs:390` | REQ-X1/X2 (one executor; sim faithful) |
| L2 | Partial fills unhandled — `wait_for_fill` only accepts `"filled"` (`policy/mod.rs:547`); `"partially_filled"` times out though shares bought | `policy/mod.rs:547,560` | REQ-X3 |
| L3 | State split Postgres+Redis; dynamic stops only in volatile Redis → crash loses them, fallback rebuilds WRONG stop from params | `executor.rs:146-157` | REQ-S1/S2 |
| L4 | No broker reconciliation live — `reconcile_closed_position` exists only on the SIM path, unused live → orphaned `IN_PROGRESS` | `simulation/live.rs:159` vs `executor.rs` | REQ-S3 |
| L5 | Non-idempotent entry — single insert, no coid dedupe | `initiator.rs:~409` | REQ-X4 |
| L6 | `fill_time=entry_time`; stubbed actions (`// TODO: Place limit order`) | `initiator.rs:~430`, `executor.rs:458` | REQ-X3 + closed action set |

**Meta-lesson (the thing to not repeat):** sim and live were TWO execution implementations that never
tested each other, and state was split with the dynamic part in the volatile store + no broker
reconcile. The plan's whole job is to make those structurally impossible. (Also: a stated-but-
unenforced UTC-everywhere tz rule — we adopt AND enforce it, REQ in §6.1.)

---

## 4. The architecture in one picture

```
            decide()  ── pure, columnar, written ONCE (REQ-D1/D2/D3)
               │  reads
   CrossSection│            StrategyState  ── one typed model (REQ-S1)
   (from feed) │            (from store)
               ▼
            Runner  ── the one loop; ties the four swappable components
        ┌──────┴───────────────────────────────────────────┐
   BACKTEST                                              LIVE
   PanelFeed + SimClock                                 BusFeed + RealClock
   MemoryStateStore                                     PgStateStore (durable SoT, REQ-S2)
   BacktestExecutor ── simulates Alpaca (REQ-X2)        PaperExecutor ── real Alpaca
        │  fast path = run_vectorized (REQ-P1)               │  + reconcile (REQ-S3), restart-safe (REQ-S4)
        └───────────────── SAME decide(), SAME state model, SAME OrderIntent ─────────────┘
```

---

## 5. Worked example (the seam made concrete, still contract-level)

Cross-sectional L/S overnight (the `overnight_beta` shape, already live):
- `decide(cs, state)` ranks a columnar score, diffs the target book vs `state.positions`, emits
  `OrderIntent`s with deterministic `client_order_id`s, TIF=CLS.
- **Backtest:** `Runner(decide, MemoryStateStore, BacktestExecutor, PanelFeed, SimClock)`. Fast path
  applies the score+rank BATCH over the panel and books simulated fills (per-name half-spread,
  modeled partials) into the in-memory `StrategyState`.
- **Live:** `Runner(decide, PgStateStore, PaperExecutor, BusFeed, RealClock)` — a thin harness; each
  cycle: `BusFeed`→`decide` (same code)→`PaperExecutor.submit`→`reconcile`→`PgStateStore.save`; on
  restart `load`+`reconcile` resume.

The decision code, the `StrategyState` fields, and the `OrderIntent`s are identical; only
`{StateStore, Executor, DataFeed, Clock}` swap. Existing containers (`overnight_beta`/`reversion`/
`smoke`) re-express with their decision logic lifted UNCHANGED; their bespoke `BetStore`/`PositionStore`
become the typed `StrategyState`+`PgStateStore` (a schema-shape change, not a logic rewrite) + add
`reconcile`. (Own PR; no live-container behavior change without its own PR + a parity test.)

---

## 6. ⭐ TEST STRATEGY (first-class — three tiers; each REQ has a test that would catch its violation)

The plan is only trustworthy if the tests below are specified up front. Each is named, with its pass
condition, so an audit can check coverage and a builder cannot quietly skip one.

### 6.1 Tier 1 — UNIT (each contract/component in isolation)

| Test | Pass condition | Guards |
|---|---|---|
| `decide` purity | same (cs, state) → identical intents across repeated calls; no wall-clock/RNG/IO reachable | REQ-D2 |
| `decide` columnar-batch == per-event | the score expression selects identical legs applied batch (whole panel) vs per-event (one slice) | REQ-D1/D3 (`test_batch_vs_per_event_select_identical_legs` — already green) |
| `BacktestExecutor` fill at tradeable price | a MARKET/DAY intent fills at the ≥09:35 entry; CLS at the close print; OPG at next-open; never a look-ahead price | REQ-X2 |
| `BacktestExecutor` charges per-name half-spread | net fill cost == panel `half_spread_bps` (+slippage), per name | REQ-X2 |
| `BacktestExecutor` partial-fill model | qty > per-bar liquidity cap → `PARTIALLY_FILLED` then resolve across bars | REQ-X3 |
| `BacktestExecutor` reject model | sub-$1 / zero-volume / halted name → `REJECTED` | REQ-X3 |
| `OrderIntent` idempotency key | same logical intent → same deterministic `client_order_id` | REQ-X4 |
| `StrategyState` transitions | submit→pending; partial→update cumulative; fill→position+realized P&L; close→flat; each transition deterministic | REQ-S1 |
| Fill-ledger recompute | positions recomputed from the append-only ledger == `state.positions` | REQ-S2 |
| UTC enforcement | a naive datetime into any contract boundary RAISES (not silently accepted) | tz rule |

### 6.2 Tier 2 — INTEGRATION (components together; the parity + production-real behaviors)

| Test | Pass condition | Guards |
|---|---|---|
| **Executor conformance (sim==paper)** | the SAME scripted scenarios (full fill, partial, reject, cancel, bracket) produce matching `Fill`/`OrderState` sequences from `BacktestExecutor` and `PaperExecutor` (paper Alpaca, sandbox) | REQ-X1/X2 — the core anti-L1 proof |
| **decide+state parity backtest vs live** | the same `decide`+`StrategyState` run through `BacktestExecutor/PanelFeed` and `PaperExecutor/BusFeed` produce consistent positions/intents on identical feature inputs | REQ-D1/S1 |
| **Restart-safety: mid-order** | kill the runner after `submit` before fill; restart `load`+`reconcile` → resolves the real fill from the broker, no re-submit, no double-fill | REQ-S4 |
| **Restart-safety: orphaned stop** | broker fills a stop while the runner is down; restart `reconcile` → realizes the close from the broker, clears the position, no "re-exit dead trade" | REQ-S3/S4 |
| **Restart-safety: partial across restart** | partial fill, kill, restart → cumulative filled_qty from the broker is authoritative; no double-count | REQ-S4 |
| **Reconciliation: broker wins** | inject a state/broker mismatch (broker closed a position) → `reconcile` adopts broker truth, audits drift, large drift alerts | REQ-S3 |
| **Rate-limit / reject handling** | a 429 → bounded backoff, no crash; a REJECT → surfaced to the state machine, not swallowed | REQ-X5 |
| **Budget** | `evaluate_features` on the stated scope < 30–60s, MEASURED, with the faithful (non-hack) fill path | REQ-P1/P2 |

### 6.3 ⭐⭐ Tier 3 — ANTI-CHEAT self-proofs ("how do we know we're not fooling ourselves" — Ben's central concern)

These are the harness proving it is NOT fooling itself. ALL are REQUIRED; any failure ⇒ the harness is
untrusted (REQ-A1). Most already exist in `quantlib/backtest.py` / the battery and are LIFTED here as
mandatory gates.

| Self-proof | Construction | PASS condition (what "honest" looks like) | If it FAILS |
|---|---|---|---|
| **Shuffle canary** | permute labels WITHIN each timestamp, re-score | canary IC ≈ 0; real edge must exceed it | a leak: the pipeline sees the future |
| **Predict-zero baseline** | constant (zero) prediction | IC 0; P&L = pure cost drag (negative net) | the cost model is wrong / P&L is fictitious |
| **Known-NULL feature (pure noise)** | feed a random-noise "feature" through the full battery | leaderboard EMPTY after BY-FDR | the harness manufactures edge from noise — fatal |
| **Planted synthetic edge** | inject a feature = (forward return + noise) with a known IC | the cell is DETECTED (not just "conservative null") | the harness is too blunt to find real edge |
| **Look-ahead / purge** | shift a genuine feature forward 1 bar (peeking) | the spurious edge VANISHES under the walk-forward purge | purge/embargo is broken → silent look-ahead |
| **Tradeable-entry** | assert earliest entry ≥ 09:35 ET; attempt a 09:30-print entry | rejected / flagged by the SanityReport | the gap-fade look-ahead trap |
| **Data-trap guards** | sub-$1 prints, per-day winsor, label-std sanity | label_std in-band; $1 floor applied; a 50–226× fake-return blow-up is FLAGGED | the illiquid-tail / bad-print traps |
| **Multiple-comparisons** | run the whole grid on pure noise | ≤ family-FDR cells survive BY-FDR (≈0) | p-hacking across cells is undefended |
| **Reproduce trusted verdicts** | run the existing hand-rolled findings THROUGH the battery | reproduces: trusted-baseline NULL (intraday 30/60m IC≈0, NW\|t\|≈0.1); laneC overnight full-univ HIT (IC≈0.035, NW t≈3.89, breakeven≈22bps) → liquid-1500 COLLAPSE (IC≈0.011, edge≈+0.007, t≈1.20, breakeven≈4.12) | if the harness DISAGREES with trusted results, FLAG it — either the harness or the prior result is wrong, and we must know which |

The reproduce-trusted-verdicts proof is the keystone: a brand-new abstraction that re-derives the
team's hard-won published numbers (a HIT that collapses on the tradeable universe, and an honest null)
is faithful; one that doesn't is suspect. This is the Phase-0 acceptance criterion.

---

## 7. Honest risks (lead with the make-or-break)

- **R1 — speed vs fill-fidelity (THE key tension, REQ-P2).** Cross-sectional basket fills ARE columnar
  (group-by at tradeable price + per-name half-spread; rare partials as a cost adjustment), so the
  fast batch path is faithful. The genuine tension is PATH-DEPENDENT archetypes (triple-barrier/streak)
  whose fill schedule is sequential → the shared `quant_tick` Rust kernel both sides call. **If a
  future archetype needs intra-bar fill sequencing that neither vectorizes nor fits the kernel, that is
  a go/no-go to bring to Ben — NOT a silent backtest-only fast-but-fake fill path.** This is the one
  place the plan explicitly refuses to choose unilaterally.
- **R2 — minute-bar simulation fidelity is fundamentally limited.** We cannot perfectly simulate
  intra-minute fills/auctions from minute bars. Mitigation (a measurement, not a hack): an explicit
  conservative fill model (half-spread + slippage bps + volume-participation cap) surfaced in the
  `BacktestResult`, AND the live `PaperExecutor` LOGS realized slippage vs the model (the
  `overnight_beta` pattern) so the gap is measured and tightened, never assumed zero.
- **R3 — reconciliation drift is real/adversarial.** Broker can close server-side (margin/halt/risk).
  Stance: broker wins; large drift alerts (no silent auto-fix); the append-only ledger recomputes
  positions to catch corruption; state can lag the broker between cycles but idempotent coids make a
  lagged re-submit safe.
- **R4 — partial-fill policy is a strategy choice, not a default to hide.** Made explicit per strategy
  (`ACCEPT_RESIZE | CANCEL_REMAINDER | RETRY`), and the SAME policy runs in backtest so the sim
  exercises it.
- **R5 — over-abstraction.** The prior repo's untyped KV state was *under*-typed (→ drift); the
  opposite failure is a generic state DSL. Guard: `StrategyState` is a small typed dataclass + a typed
  `counters` map; `Executor`/`StateStore` are ~3-method protocols. No schema engine.
- **R6 — the conformance test depends on paper-Alpaca availability/determinism.** Paper fills aren't
  perfectly deterministic; the conformance test scripts SCENARIOS (full/partial/reject/cancel) and
  asserts the lifecycle SHAPE matches, not exact prices/timings — flagged so auditors weigh it.

---

## 8. One-page summary (for the audit packet)

- **Requirements:** decide() written-once + pure + columnar (D1–D3); ONE Alpaca-faithful Executor with
  full lifecycle + partials + rejects + idempotency (X1–X5); ONE typed, durable, broker-reconciled,
  restart-safe StrategyState (S1–S4); <30–60s WITHOUT a fidelity hack (P1–P2); the anti-cheat
  self-proofs are required (A1).
- **Abstraction (contracts):** `Strategy.decide`, `OrderIntent`, `Fill`, `Executor`
  (Backtest/Paper/Live), `DataFeed` (Panel/Bus), `Clock` (Sim/Real), `StrategyState`/`StateStore`
  (Memory/Pg), `Runner` — only the four components swap between backtest and live.
- **Prior-repo lessons:** the trait-shapes were right; it broke on (L1) sim≠live execution paths, (L2)
  partial fills, (L3) split/volatile state, (L4) no broker reconcile, (L5) non-idempotent entry, (L6)
  lost fill metadata. Each maps to a REQ that closes it.
- **Test strategy:** Tier-1 UNIT (executor fill-sim fidelity, state transitions, decide purity);
  Tier-2 INTEGRATION (sim==paper conformance, backtest==live parity, restart-safety ×3, reconciliation,
  budget); Tier-3 ANTI-CHEAT self-proofs (shuffle canary, predict-zero, known-null→empty leaderboard,
  planted-edge→detected, look-ahead/purge, tradeable-entry, BY-FDR multiple-comparisons, and
  REPRODUCE the trusted baseline-null + laneC HIT→collapse — the keystone).
- **Risks:** R1 speed-vs-fidelity for path-dependent archetypes (the go/no-go, never silently fork);
  R2 minute-bar fidelity (measured via live slippage); R3 reconcile drift; R4 partial-fill policy
  explicit; R5 no over-abstraction; R6 conformance test vs paper-Alpaca nondeterminism.
- **Status:** NO build. Gated on the adversarial audits passing.
