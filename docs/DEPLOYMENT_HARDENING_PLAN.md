# Deployment-Hardening Plan — the net-positive trusted-baseline portfolio, backtest → live paper → real money

**Status:** PLAN (no code, no deploy). Authored 2026-06-20 from Ben's direction to make
deployment-hardening the primary workstream. Scope: take the **net-positive trusted-baseline
portfolio** (the assembled weak-signal model, net-positive under REALIZED per-name cost — PR #271,
+$123,579 headline-10% / Sharpe 18.9 / 42 dates) from a **backtest result** to a **paper-trading
strategy container running end-to-end on the production executor**, with cost/risk/monitoring,
restart-safe and reconciled — and then map the further delta to real-money-ready (Ben trades up to
~$100K real after paper proves out).

This plan deliberately **builds on, and does not duplicate**, the existing ROADMAP M3→M5 ladder
([ROADMAP](ROADMAP.md)) and the BUILT execution layer
([STRATEGY_EXECUTION_ABSTRACTION](STRATEGY_EXECUTION_ABSTRACTION.md)). It is the concrete,
gated build sequence the Lead can hand to agents.

> **The core finding up front:** the edge IS this portfolio. The execution+state machinery is BUILT
> and three strategy containers ALREADY trade it-shaped logic on paper. But **the net-positive
> portfolio itself is not one of those containers** — it exists only as a backtest. And the live
> containers run a **hybrid** path: real Alpaca paper orders via `PaperAlpacaExecutor`, but
> restart/reconcile still on their **bespoke** `BetStore`/`PositionStore`, with `PgStateStore` wired
> only as a parallel append-only mirror. So the gap is two-fold: (a) the *strategy* isn't deployed,
> and (b) the *production reconcile/restart path* isn't the live path even on the strategies that are.

---

## 1. Current-state audit — what is actually built vs backtest-only vs stubbed

Cited to files/PRs. Honest about the seams.

### 1.1 The edge: the net-positive trusted-baseline portfolio — **BACKTEST ONLY**

- It is a **backtest experiment**, not a deployed strategy. It is the trusted-baseline harness L/S
  model evaluated under measured per-name half-spread cost.
  - `experiments/2026-06-20-realized-cost-stage1/RESULT.md` — headline-10% **$ = +123,579**
    (−22% vs the +158,130 flat-stub run), **Sharpe 18.9** (vs 31.45 flat-stub), median half-spread
    **8.39 bps** (2.8× the 3.0 bps flat stub), conservative 10% cut **+90,080**. 42 dates / 3,621
    OOS rows.
  - `experiments/2026-06-20-edge-hunt-meta-synthesis/EDGE_HUNT_META_SYNTHESIS.md` — verdict:
    "the combined trusted-baseline model is net-positive under accurate (Stage-1 measured) cost …
    Individual signals are individually-weak; the assembled model is not."
- It runs through the **strategy harness** (`run_strategy()` / battery, `STRATEGY_HARNESS.md`, #237)
  on a historical panel. There is **no `decide()`-shaped production strategy** for it, no container,
  no schema, no risk caps, no monitoring. **This is the central gap.**
- **Caveat carried from the audit:** the result is 42 dates and on a panel that is at least partly
  survivorship-biased (see the B4 delisting gate in the SYSTEM_LOG and `project-b4-overnight-survivor`).
  The realized-cost haircut is now honest (#271 charges measured per-name half-spread), but the
  **out-of-sample horizon is short and the universe needs the delisting-inclusive panel** the
  Modeller's research track is producing. Paper-proving is partly *how* we extend that horizon.

### 1.2 The execution + state layer — **BUILT, tested against a stub, partially live**

- BUILT: `quantlib/strategy_core/production_execution.py` (OrderIntent, G2 coid),
  `production_executor.py` (`ProductionExecutor` protocol, `FaithfulBacktestExecutor`,
  `PaperBrokerStub`), `production_state.py` (`PgStateStore`, append-only ledger, `recover_on_restart`,
  per-strategy G1 reconcile, G6 corp-actions). 19 green gates in `tests/test_production_execution.py`.
  Full contract + test map in [STRATEGY_EXECUTION_ABSTRACTION](STRATEGY_EXECUTION_ABSTRACTION.md)
  (PR #214). Covers REQ-D/X/S/G/P/A.
- **Real-broker executor EXISTS** but is **unproven against a live round-trip:**
  `quantlib/strategy_core/paper_alpaca_executor.py` (`PaperAlpacaExecutor`) maps Alpaca-py calls onto
  the `ProductionExecutor` contract (submit/poll/cancel/positions/get_order_by_coid, G2 coid). The
  conformance test (`sim == paper`) ran only against the **in-process `PaperBrokerStub`**, never a
  real Alpaca paper account. **This integration test is the open gate** before any cutover
  (STRATEGY_EXECUTION_ABSTRACTION.md:8).

### 1.3 The live strategy containers — **REAL paper orders, but hybrid state path**

All three run live on paper today (`docker ps`: `smoke-strategy` Up 17h, `reversion-strategy` Up 17h,
`overnight-beta-strategy` Up 9h), all wired to `PaperAlpacaExecutor`:

| Container | What `decide()` does | Edge? | State / restart path |
|---|---|---|---|
| **smoke** (`strategies/smoke/`) | trivial token buy on last-seen symbol, timed exit | none (apparatus proof) | bespoke `BetStore`; `PgStateStore` mirror |
| **reversion** (`strategies/reversion/`) | intraday VWAP mean-reversion (`VwapReversionModel`) | not an edge claim (uneconomic at minute turnover) | bespoke `BetStore`; `PgStateStore` mirror; `reconcile_on_startup()` reads `BetStore` |
| **overnight_beta** (`strategies/overnight_beta/`) | beta-quintile L/S held overnight (MOC→MOO); the W11 certified signal; purpose = **measure realized auction slippage** vs the 5 bps model | the only certified single signal | bespoke `PositionStore`; `PgStateStore` mirror |

Two important seams (verified in the code, not assumed):

1. **None of these is the net-positive portfolio.** overnight_beta is a *single* certified signal run
   to measure auction slippage; the net-positive *portfolio* of weak signals is a different, undeployed
   thing.
2. **The production reconcile/restart is NOT the live path.** `PgStateStore` is passed as `state_store`
   and used only to `append_fill(...)` + `load(...)` a parallel `StrategyState` mirror
   (`strategies/overnight_beta/strategy.py:153-181`). The **authoritative** startup reconcile and
   "resume open bets" logic still runs on the bespoke `BetStore`/`PositionStore`
   (`reversion/strategy.py:240,484` `reconcile_on_startup()` over `BetStore`). So the restart-safety,
   per-strategy G1 scoping, query-before-resubmit (G3), pre-trade gate (G4), and corp-action (G6) logic
   that PR #214 BUILT and TESTED is **not yet the code that recovers a live container.**

Safety caps are real and tiny (paper): per-name notional $50–$100, gross cap $200–$5,000, kill-switch
env (`SMOKE_ENABLED`/`REV_ENABLED`/`OBETA_ENABLED`) (`docker-compose.strategies.yml`).

### 1.4 The bus, allocation, feature production — **LIVE**

- Feature-vector bus + name-addressed decoupled consume: LIVE (`BUS_FEATURE_ACCESS`, #210/#211).
  Strategies resolve features by name+version off the published schema → fc ships independently.
- Feature production: live fc, fp `0x873f…`/728/63, full ~7,318-symbol universe.
- **Allocation layer:** the strategies share ONE Alpaca account with per-strategy coid namespaces +
  per-container gross caps. There is **no portfolio-level capital allocator / shared-account risk
  governor** beyond those per-container env caps — fine for tiny paper, a gap before multi-strategy
  real money (see §4).

### 1.5 One-line audit verdict

> The plumbing to trade is built and proven on paper for *placeholder/measurement* strategies. The
> **edge** (the net-positive portfolio) is not wired to any of it, and the **production-grade
> reconcile/restart** that was built and tested is not yet the path any live container actually runs.

---

## 2. The gap — precisely

### 2.1 Gap A — from "net-positive backtest" to "running in PAPER end-to-end on the production executor"

What has to become true, beyond what exists:

1. **A `decide()` for the portfolio.** The trusted-baseline portfolio is currently a harness
   `run_strategy()` config + a fitted model, not a pure, columnar `decide(cs, state) -> [OrderIntent]`
   (REQ-D1/D2/D3). It must be lifted into a single decision function that the **same** code runs
   batch (backtest) and per-event (live) — no second implementation.
2. **A strategy container** (`strategies/baseline_portfolio/`) with its own Postgres schema, Dockerfile,
   `__main__`, kill-switch + risk caps env, on the bus — modeled on the three existing containers.
3. **The production execution path as the LIVE path**, not a mirror: the container recovers via
   `recover_on_restart` / per-strategy `reconcile` over `PgStateStore` (REQ-S2/S3/S4/G1/G3/G6), runs
   `pre_trade_check` before each basket (REQ-G4), books actual partial weight (REQ-G5). This is the
   work item §1.3-seam-2 names: make the built layer the real path (for the new container first; the
   three existing ones migrate behind their own parity PRs).
4. **The real-Alpaca conformance gate** (§1.2): prove `PaperAlpacaExecutor` lifecycle (full/partial/
   reject/cancel) matches the sim against an actual paper account. This is the open gate in
   STRATEGY_EXECUTION_ABSTRACTION.md and blocks trusting any live fill.
5. **Backtest==paper decision parity** for THIS portfolio: the same `decide()`+state over
   `BacktestExecutor/PanelFeed` and `PaperExecutor/BusFeed` produces consistent intents on identical
   feature inputs (REQ-D1/S1).
6. **Cost/risk/monitoring in the live loop:** realized per-name half-spread & slippage logged vs the
   backtest cost model (the overnight_beta pattern, R2), a daily P&L/IC stat-gate, position & gross
   risk caps enforced pre-trade, a flatten/kill path.
7. **Observability:** the live strategy's positions, fills, realized vs modeled cost, and daily stat
   gate surfaced on the dashboard + status board.

### 2.2 Gap B — from "running in paper" to "real-money-ready"

Beyond Gap A (this is ROADMAP M4→M5, made concrete for this portfolio):

1. **≥20 trading-day paper track record** with daily stat-gate monitoring; realized net Sharpe & IC
   consistent with backtest within tolerance; **no risk-limit breach** (ROADMAP M4).
2. **The survivorship/horizon fix lands:** re-run the $-result on the delisting-inclusive panel
   (Modeller's track) and a longer OOS window so the +$123,579 isn't a 42-date survivor artifact
   (the B4 lesson). Paper days *extend* the OOS window in real time.
3. **Settled-day reconciliation muscle** exercised end-to-end: fills/fees vs broker records reconcile
   clean over many days (ROADMAP M4, Exec/Risk).
4. **Shared-account risk governor / capital allocator:** when more than one real-money strategy runs,
   a portfolio-level layer that sizes per-strategy capital, enforces an account-wide gross/net and
   drawdown limit, and owns the account-wide reconcile (the per-strategy reconcile is built; the
   account-wide *monitor* is named but not built — STRATEGY_EXECUTION_ABSTRACTION.md G1 closing note).
5. **Ben's go-live decision + thresholds** (§4): start ~$5–10K, scale toward ~$100K strictly per
   proven Sharpe + drawdown discipline; Ben approves go-live and each scale-up (ROADMAP M5).

---

## 3. Sequenced plan — ordered, gated steps mapped to Ben's acceptance criteria

Each step has an **acceptance gate** (a falsifiable pass condition) and an **isolation flag**:
🟢 = safe-isolated (worktree→PR, own schema/container, no fingerprint/fc/live-capture touch) ·
🔴 = touches live capture / fc / fingerprint / a *running* strategy container → **Lead-sequenced,
market-closed window only**.

Mapping target = Ben's acceptance criteria: the FP_GOALS headline targets (1000 feats × 10k tickers
< 2s p99; ≥95% parity per tier per session; 04:00–20:00 ET capture 0-drop; dev lifecycle for ≥5
groups; 1 model trained-on-backfill served-live with ZERO skew — `FP_GOALS.md:9-14`) and the ROADMAP
M0–M5 ladder. The platform-scale criteria (feats×tickers, latency, parity, capture) are largely
**already met or owned by other workstreams** (Latency, Parity, DIA); this plan's criteria are the
**execution/serving** ones: *zero train/serve skew*, *live paper round-trip*, *EOD-flat reconcile*,
*≥20-day track record* — plus the latency-visibility one (≤300ms / ≤2s p99) which the live loop must
not regress.

### Phase 0 — Lift the edge into a deployable shape (the FIRST buildable step)

- **Step 0.1 — `decide()` for the net-positive portfolio.** 🟢
  Extract the trusted-baseline portfolio model + sizing into a pure, columnar
  `decide(cs, state) -> [OrderIntent]` in `quantlib/strategy_core/` (or `strategies/baseline_portfolio/`).
  **Gate:** `decide` purity test (REQ-D2) + columnar-batch == per-event test (REQ-D1/D3,
  `test_batch_vs_per_event_select_identical_legs` pattern) green; running it batch over the #271 panel
  **reproduces the +$123,579 / Sharpe-18.9 result within tolerance** (REQ-A1 reproduce-trusted-verdict
  keystone). → maps to *zero train/serve skew*.
  **This is the first concrete buildable step.** It needs no live touch, no broker, no fc — pure
  research-tree work on the existing harness + panel, and it is the precondition for everything below.

- **Step 0.2 — backtest the `decide()` through the FaithfulBacktestExecutor.** 🟢
  Run the new `decide()` through `Runner(decide, MemoryStateStore, FaithfulBacktestExecutor, PanelFeed,
  SimClock)` (the built backtest path), not just the battery.
  **Gate:** P&L through the faithful executor (per-name half-spread, modeled partials) is consistent
  with the #271 battery result; predict-zero baseline = pure cost drag; shuffle canary ≈ 0 (REQ-A1).
  → *zero train/serve skew*, the anti-cheat self-proofs.

### Phase 1 — Prove the real-broker execution path (the open M0-for-this-strategy gate)

- **Step 1.1 — real-Alpaca paper conformance.** 🟢 (own paper sub-account / scoped coid namespace;
  no running container touched)
  Run the conformance suite (full/partial/reject/cancel/bracket lifecycle) of `PaperAlpacaExecutor`
  against the **actual paper Alpaca account**, asserting the lifecycle SHAPE matches
  `FaithfulBacktestExecutor` (REQ-X1/X2; the open gate in STRATEGY_EXECUTION_ABSTRACTION.md). Use tiny
  notional, a dedicated coid prefix so it never collides with the live containers.
  **Gate:** sim==paper conformance green on a real paper round-trip; rejects surfaced (not swallowed);
  rate-limit backoff bounded; **no secrets logged** (REQ-X5). → ROADMAP M0 (live paper round-trip) for
  the production executor specifically.

- **Step 1.2 — restart-safety + reconcile on real paper.** 🟢
  Exercise `recover_on_restart` (G3 four branches) and per-strategy `reconcile` (G1 coid-scoped, sibling
  ignored) against the real paper account: kill mid-order, orphaned close, partial-across-restart.
  **Gate:** restart recovers exact state from the ledger + broker, never double-trades, ignores sibling
  positions (REQ-S3/S4/G1/G3). → ROADMAP M4 "settled-day reconciliation muscle" precursor; *EOD-flat*
  discipline.

### Phase 2 — Deploy the portfolio as a paper container (Gap A complete)

- **Step 2.1 — `strategies/baseline_portfolio/` container.** 🟢 to build, 🔴 to *launch* alongside live
  containers (new container on the shared account + bus → Lead-sequenced launch, kill-switch default OFF).
  Own Postgres schema, Dockerfile (`FROM fp-dev`), `__main__`, bus consumer (name-addressed), risk-cap
  env (per-name + gross + kill-switch), `OBETA_ENABLED=0`-style default-off. The container uses
  **`PgStateStore` + `recover_on_restart`/`reconcile` as the AUTHORITATIVE path** (not a bespoke
  BetStore mirror) — i.e. the new container is where the built layer becomes the real path.
  **Gate:** container starts, consumes the bus, computes the portfolio `decide()` on live vectors,
  logs intended orders with `ENABLED=0` (compute+log, no orders) for ≥1 full session with no crash;
  feature names resolve against the live schema (bus-decouple proven end-to-end — the first real
  fc-only-deploy proof). → *zero train/serve skew* live; latency-visibility (the live decide loop
  timed, must stay within the ≤300ms / ≤2s budget — Latency owns the budget, this step must not
  regress it).

- **Step 2.2 — flip the kill-switch to paper-trade + EOD-flat proof.** 🔴 (Lead, market-closed flip;
  reviewed like OBETA #224)
  **Gate:** live paper basket submitted via `pre_trade_check` (REQ-G4) → fills booked at ACTUAL weight
  (REQ-G5) → EOD flatten leaves **0 positions / 0 open orders**, broker-confirmed, P&L recorded
  (ROADMAP M0 exit criteria, applied to this portfolio). Realized per-name cost LOGGED vs the model.

- **Step 2.3 — monitoring + daily stat gate.** 🟢
  Surface the portfolio's positions, daily realized P&L, realized-vs-modeled cost, and a daily IC /
  Sharpe stat-gate on the dashboard + status board; alert on risk-cap proximity or a stat-gate breach.
  **Gate:** the daily stat gate computes and renders; a synthetic breach alerts. → latency/coverage
  *visibility* criterion.

### Phase 3 — Paper track record → real-money-ready (Gap B / ROADMAP M4→M5)

- **Step 3.1 — survivorship/horizon revalidation.** 🟢 (depends on Modeller's delisting-inclusive panel)
  Re-run the #271 $-result on the delisting-inclusive panel + a longer OOS window.
  **Gate:** net-positive survives the delisting haircut and the longer window (or the result is
  honestly downgraded — NO false edge, ROADMAP M3 discipline).

- **Step 3.2 — ≥20-day paper track record.** ⏳ (calendar-gated, monitored)
  Run the paper portfolio ≥20 trading days with the daily stat gate.
  **Gate:** realized net Sharpe & IC consistent with backtest within tolerance; **no risk-limit
  breach**; settled-day fills/fees reconcile clean vs broker records every day (ROADMAP M4). → the
  *≥20-day track record* criterion.

- **Step 3.3 — shared-account risk governor.** 🟢 to build, 🔴 to arm
  A portfolio-level capital allocator + account-wide gross/net/drawdown limit + account-wide reconcile
  monitor (the named-but-unbuilt piece, STRATEGY_EXECUTION_ABSTRACTION.md G1 closing note). Required
  before >1 real-money strategy.
  **Gate:** account-wide caps enforced; a simulated breach halts new entries; account-wide drift alerts.

- **Step 3.4 — real-money go-live (Ben signs off).** 🔴🔴 (Ben decision, ROADMAP M5)
  Deploy ~$5–10K real; scale toward ~$100K strictly per proven Sharpe + drawdown discipline.
  **Gate:** Ben approves go-live and each scale-up; M4 fully met; real-money risk limits set.

### 3.1 Critical path / dependency summary

```
0.1 decide() ──► 0.2 faithful-backtest ──► 2.1 container(log-only) ─┐
1.1 real-paper conformance ──► 1.2 restart/reconcile-on-paper ──────┤
                                                                     ▼
                                              2.2 paper-trade + EOD-flat ──► 2.3 monitoring
                                                                     │
3.1 survivorship/horizon revalidation ───────────────────────────────┤
                                                                     ▼
                                              3.2 ≥20-day track record ──► 3.3 risk governor ──► 3.4 Ben go-live
```

Phase 0 and Phase 1 are **independent and parallelizable** (0.x = research-tree; 1.x = execution-tree)
and both feed Phase 2. **Step 0.1 is the single first buildable step** — start there.

---

## 4. Risks & open questions for Ben

1. **Real-money thresholds (M5).** Confirm: start at ~$5–10K, scale to ~$100K. What exact **per-scale-up
   gate** (e.g. realized Sharpe ≥ X over Y days, max drawdown ≤ Z) triggers each capital step, and is
   each step your explicit manual approval? (ROADMAP says yes; please confirm the numbers.)
2. **Survivorship horizon.** The #271 +$123,579 is **42 dates** on a partly survivorship-biased panel.
   Are you comfortable beginning the *paper* deploy (Gap A) in parallel with the survivorship
   revalidation (Step 3.1), using paper days to *extend* the honest OOS window — or do you want 3.1 to
   gate even the paper launch?
3. **Which edge goes live first.** This plan deploys the **net-positive portfolio**. The certified
   **overnight_beta** single signal is already live-paper as a slippage probe. Do you want the portfolio
   to be the first real-money candidate, overnight_beta, or both (which forces Step 3.3 the shared-account
   governor sooner)?
4. **Broker/account specifics.** Is the single shared paper Alpaca account the intended real-money
   topology too (one account, per-strategy coid namespaces), or do you want **separate accounts /
   sub-accounts per strategy** for real money (cleaner risk isolation, simpler reconcile, but more
   ops)? This decides whether Step 3.3 is mandatory or optional.
5. **Risk limits.** What account-wide hard limits do you want enforced from day one of real money —
   max gross exposure, max per-name, daily-loss kill, max drawdown before auto-flat? These become the
   risk-governor config (Step 3.3) and need your numbers.
6. **Migrating the existing containers.** The three live containers (smoke/reversion/overnight_beta)
   still reconcile on bespoke stores. Do you want them **migrated** onto the production reconcile path
   (each its own parity PR), or left as-is (they're tiny/paper/measurement) while only the new portfolio
   container uses the production path? (Recommendation: leave them; migrate opportunistically.)

---

## Navigation
Part of the [System Description](SYSTEM_DESCRIPTION.md). Builds on
[STRATEGY_EXECUTION_ABSTRACTION](STRATEGY_EXECUTION_ABSTRACTION.md) (the BUILT layer),
[ROADMAP](ROADMAP.md) (M0–M5), [STRATEGY_HARNESS](STRATEGY_HARNESS.md) (where the portfolio lives
today), [STRATEGY_CONTAINERS](STRATEGY_CONTAINERS.md) (the container shape),
[BUS_FEATURE_ACCESS](BUS_FEATURE_ACCESS.md) (the decoupled consume), and [FP_GOALS](FP_GOALS.md)
(the acceptance targets).
