# Strategy Execution Abstraction — production-real State + Executor + Feed

Status: **DESIGN** (no implementation this cycle — for Lead/Ben review before any build).
Author: platform engineer, cycle 2026-06-19.

Ben's anti-goal (the thing this design must NOT become): *"a beautiful experiment harness that
works ONLY in an experiment setting and has a million hacks that reduce reliability/trustworthiness
once we actually deploy."* The two parts he tried before and found very hard: **STATE** and
**PRETEND-VS-REAL EXECUTION**. This doc designs for production from the first line, grounded in
studying his prior attempt (`/home/ben/automated-day-tracking-claude/rust_trade_executor/`) so we
build on the walls he already hit instead of rediscovering them.

The unifying principle is the platform's: **parity-by-construction**. The strategy's decision logic,
and now also its STATE model and its EXECUTION semantics, are defined ONCE and behave identically in
backtest and live — the only swap is the *implementation* behind a trait/protocol (in-memory vs
persisted state; simulated vs real fills; panel vs bus feed). A divergence between the two IS the
hack-debt Ben fears, so the design's job is to make divergence structurally impossible, or — where it
genuinely can't be eliminated — to **flag it as a named risk with options**, never paper over it.

---

## 1. Prior art: what Ben built, and exactly where it broke

His `rust_trade_executor` is a real, thoughtful attempt — the *shapes* are good and we keep them.
What was incomplete is precisely the production-fidelity of STATE and PRETEND-VS-REAL. Concretely:

### 1.1 The shapes he built (keep these)
- **`TradeState` trait** (`src/state.rs:10`): `set/get/delete(key, Value)` with `MemoryState`
  (in-memory `HashMap`, offline sim) and `RedisState` (Redis, live). *Policies call it without
  knowing which backs them* — the right idea: one state interface, swappable storage.
- **`BarDelivery` trait** (`src/bar_delivery.rs`): `next_bar()` with `MemoryBarDelivery` (offline,
  instant) vs `PubSubBarDelivery` (live, Redis `bar:complete`). This is our `DataFeed`.
- **`Policy` trait** (`src/policy/mod.rs`): `evaluate()` runs in BOTH sim and live. This is our
  `decide()`. The aspiration — "policies work identically offline and real-time" — is exactly the
  invariant. (The `quant-fp` live containers `reversion`/`smoke`/`overnight_beta` already follow the
  same pure-decision + harness split — see `STRATEGY_BATTERY_PORTABILITY.md`.)

### 1.2 Where it BROKE (the load-bearing lessons — design around each)

| # | Wall he hit | Evidence | Root cause | What our design must do |
|---|---|---|---|---|
| **L1** | **Sim never placed orders** — `label_gen.rs`/`simulate.rs` run `evaluate()` but NEVER call `enter_trade()`; live `initiator.rs` does. So backtest fills at `bar.close` with no slippage/partial/reject; live gets real fills. | `label_gen.rs` uses `MemoryState`+`MemoryBarDelivery`, no order placement; live `initiator.rs:390` calls `enter_trade()` + waits for fill. | TWO execution paths. The exact backtest≠live drift. "Backtest 5% win, live 1% — surprise at go-live." | **One `Executor` trait**; `BacktestExecutor` SIMULATES the same fill semantics the real one produces. Sim and live both go through `execute(OrderIntent)`. |
| **L2** | **Partial fills unhandled** — `wait_for_fill()` only accepts status `"filled"`; `"partially_filled"` times out and the trade fails to record though shares were bought. | `policy/mod.rs:~560` bails unless `=="filled"`; `EntryResult.fill_qty` exists but is never validated vs requested. | The fill model assumed atomic full fills. | Fill is a **first-class outcome** (`Fill{filled_qty, avg_price, status}`), partials modeled in BOTH the sim and the live executor; the state machine handles `partially_filled`. |
| **L3** | **State source-of-truth split → drift/loss** — trade record in Postgres, live stop updates only in Redis. Redis crash ⇒ trailing-stop updates lost, fallback reconstructs the WRONG stop from params. | `executor.rs:144-164` reads Redis-first then falls back to policy params; Postgres never stores the updated stop. | Two stores, neither authoritative; dynamic state only in the volatile one. | **One typed `StrategyState`** persisted to ONE durable store (the trade DB), reconciled against the broker; no "dynamic state only in Redis" path. |
| **L4** | **No broker reconciliation** — live executor loads `IN_PROGRESS` from Postgres but never calls `get_positions()` to check what Alpaca actually holds; a stop that filled while the process was down leaves an orphaned `IN_PROGRESS` row. | `reconcile_closed_position()` exists only on the SIM path (`simulation/live.rs:159`), unused by the live executor. | Reconciliation was built for sim, never wired to live. | **Reconcile against the broker is mandatory** at startup + periodically; the broker is the source of truth for positions. |
| **L5** | **Entry not idempotent** — a retried entry request can double-trade; `initiator.rs` writes the trade once with no duplicate guard. | `initiator.rs:~409` single insert, no client-order-id dedupe at the state layer. | No idempotency key tying intent→order→state. | Every order carries a deterministic `client_order_id`; entry is idempotent (the broker rejects dup coids; the state upserts on coid). |
| **L6** | **`fill_time = entry_time`** (no execution-latency record); some actions stubbed (`ExitLimit` → `// TODO: Place limit order`). | `initiator.rs:~430`; `executor.rs:458`. | Time + action coverage shortcuts. | Fills carry the broker's real `filled_at`; the action set the executor supports is closed + tested, no silent TODOs. |

The meta-lesson: **the abstraction shapes were right; the failure was that the SIMULATED path and the
LIVE path were two different implementations that never tested each other, and STATE was split across
two stores with the dynamic part in the volatile one.** Our design fixes exactly those two things.

He also had a hard-won discipline worth importing: a **strict UTC-everywhere timezone rule**
(`docs/trading/PYTHON_WRAPPER.md`) — storage/processing UTC, ET only for market-hours logic, PT only
for display. We adopt it and *enforce* it (the prior repo stated it but didn't assert it).

---

## 2. The execution invariant (extends the decide() invariant)

`STRATEGY_BATTERY_PORTABILITY.md` established: **the decision logic is written once and run
identically batch (panel, fast) and per-event (live)**. This doc extends parity to the two hard
layers:

- **STATE parity:** there is ONE `StrategyState` model. Backtest uses an in-memory instance; live uses
  a persisted-and-reconciled instance. The strategy reads/writes the SAME typed fields either way.
- **EXECUTION parity:** there is ONE `Executor` interface taking ONE `OrderIntent`. `BacktestExecutor`
  produces fills by SIMULATING Alpaca's actual behavior (tradeable price, partials, slippage, the
  per-name half-spread, rejects); `PaperExecutor`/`LiveExecutor` produce fills from real Alpaca. The
  fill TYPE and the state transitions are identical; only the fill SOURCE differs.

So a strategy = `decide()` (signal→intent, §portability doc) + a `StrategyState` model + the set of
`OrderIntent`s it emits. None of those three know whether they're in backtest or live.

---

## 3. The Executor — designed against the REAL Alpaca API

The `BacktestExecutor` is only trustworthy if it simulates *what Alpaca actually does*. So the
interface is shaped by real Alpaca semantics (alpaca-py, as the live `quant-fp` containers use it:
`TradingClient.submit_order`, `get_order_by_client_id`, `get_all_positions`, `get_account`).

### 3.1 The order/fill model (real semantics, not a hand-wave)

```python
@dataclass(frozen=True)
class OrderIntent:                      # what decide() emits (broker-agnostic)
    client_order_id: str               # DETERMINISTIC idempotency key (fixes L5)
    symbol: str
    side: Side                         # BUY | SELL
    qty: float | None                  # fractional allowed; XOR notional
    notional: float | None
    type: OrderType                    # MARKET | LIMIT | STOP | STOP_LIMIT
    limit_price: float | None
    stop_price: float | None
    tif: TimeInForce                   # DAY | CLS (MOC) | OPG (MOO) | GTC
    bracket: Bracket | None            # optional OTO/OCO stop+target (Alpaca bracket)
    reason: str

@dataclass(frozen=True)
class Fill:                            # a fill EVENT (one order may produce several — partials)
    client_order_id: str
    symbol: str
    side: Side
    filled_qty: float                  # cumulative
    avg_price: float
    status: OrderState                 # NEW|PARTIALLY_FILLED|FILLED|CANCELED|REJECTED|EXPIRED
    filled_at: datetime | None         # the broker's real timestamp (fixes L6)
    raw_broker_order_id: str | None
```

`OrderState` is the **full Alpaca lifecycle**, not just "filled" (fixes L2): `NEW`, `PENDING_NEW`,
`ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`. The executor + state
machine handle every terminal AND intermediate state.

### 3.2 The `Executor` interface (one in, faithful behavior both sides)

```python
class Executor(Protocol):
    def submit(self, intent: OrderIntent) -> OrderState: ...     # idempotent on client_order_id
    def poll(self, client_order_id: str) -> Fill: ...            # current cumulative fill
    def cancel(self, client_order_id: str) -> None: ...
    def positions(self) -> dict[str, Position]: ...             # broker truth (live) / sim book
    def account(self) -> AccountSnapshot: ...
```

- **`LiveExecutor` / `PaperExecutor`** (real Alpaca, paper today): `submit` → `TradingClient.submit_order`
  with the `client_order_id` (Alpaca dedupes dup coids → idempotent retries, fixes L5). `poll` →
  `get_order_by_client_id` mapping Alpaca's status+`filled_qty`+`filled_avg_price` into `Fill`.
  `positions` → `get_all_positions` (the broker truth for reconciliation, §4). Handles **rejects**
  (status REJECTED → surfaced, not swallowed), **rate limits** (429 → bounded exponential backoff +
  a single in-flight-per-coid guard), and never logs secrets.
- **`BacktestExecutor`** (pretend, over the panel): `submit` simulates the fill FAITHFULLY —
  - fill price = the **tradeable entry** for the intent's TIF (≥09:35 for the open, the close print
    for CLS/MOC, the next-open for OPG/MOO — the same tradeable-entry discipline the Panel already
    enforces), NOT a look-ahead price;
  - charge the **per-name half-spread** (from the panel's `half_spread_bps`) + slippage — the realistic
    cost model already in `quantlib/strategy_core/cost.py`;
  - **model partials**: when the intent's qty exceeds a per-bar liquidity cap (a fraction of the bar's
    volume), fill partially and emit `PARTIALLY_FILLED` then `FILLED`/`EXPIRED` across bars — so the
    SAME partial-fill state path the live executor exercises is exercised in backtest (fixes L1+L2);
  - **model rejects**: a sub-$1 / halted / zero-volume name → `REJECTED`, exactly as Alpaca would.

  The `BacktestExecutor` is the one place "simulate Alpaca" lives; its fidelity is the deliverable, and
  it is **pinned by a conformance test** (§6) that asserts its `Fill`/`OrderState` outputs match the
  `PaperExecutor`'s on the same scripted scenarios (full fill, partial, reject, cancel).

### 3.3 The fidelity boundary (named honestly, not hidden)
Some real effects the `BacktestExecutor` can only *approximate* from minute bars: intra-bar fill
sequencing, true queue position, auction (MOC/MOO) imbalance slippage, and exact partial-fill
schedules. The design's stance: **approximate them with an explicit, conservative model parameter**
(e.g. half-spread + a slippage bps + a volume-participation cap), surface the assumption in the
`BacktestResult`, and let the live `PaperExecutor`'s realized slippage *measure* the gap (exactly what
the `overnight_beta` container already does — it logs MOC/MOO slippage vs the model). This is the
honest version of "faithful simulation": the model is explicit and its error is measured live, not
assumed zero. **This is also the §7 key risk.**

---

## 4. StrategyState — first-class, persisted, reconciled, restart-safe

The prior repo's state was an untyped KV blob split across two stores (L3) with no broker
reconciliation (L4). We make state a **typed, single-source-of-truth, broker-reconciled** model.

### 4.1 The typed model (same in backtest + live)

```python
@dataclass
class Position:
    symbol: str
    qty: float                         # signed (long +, short −)
    avg_entry_price: float
    opened_at: datetime

@dataclass
class PendingOrder:
    client_order_id: str
    intent: OrderIntent
    state: OrderState                  # last known lifecycle state
    filled_qty: float                  # cumulative so far (partials)
    avg_fill_price: float

@dataclass
class StrategyState:
    strategy_id: str
    positions: dict[str, Position]
    pending: dict[str, PendingOrder]   # keyed by client_order_id
    realized_pnl: float
    # strategy-specific carry (streak/persistence counters, last-rebalance day, trailing stops):
    counters: dict[str, float]         # TYPED accessors per strategy; persisted with the rest
    last_reconciled_at: datetime | None
```

Crucially, *dynamic* fields the prior repo lost on a Redis crash (trailing stops, streak counters,
updated TP) live HERE, in `counters`, persisted to the **same durable store** as positions — never in
a volatile side-store (fixes L3). The strategy reads/writes `counters` through typed helpers; the same
helpers run in backtest (in-memory `StrategyState`) and live (persisted `StrategyState`).

### 4.2 The `StateStore` interface (swappable persistence behind one shape)

```python
class StateStore(Protocol):
    def load(self, strategy_id: str) -> StrategyState: ...
    def save(self, state: StrategyState) -> None: ...           # atomic, upsert by strategy_id
    def append_fill(self, fill: Fill) -> None: ...              # append-only fill ledger (audit)
```

- **Backtest:** `MemoryStateStore` — `StrategyState` in process. Fast; the battery's vectorized path
  carries the per-timestamp book as columnar arrays (no per-event store calls), but the SAME typed
  `StrategyState` is the conceptual model and the per-event reference path uses it directly.
- **Live:** `PgStateStore` — `StrategyState` persisted to the strategy's own Postgres schema (the
  `quant-fp` containers already own per-strategy schemas, e.g. `BetStore`/`PositionStore`). `save` is
  atomic (one transaction); the **fill ledger is append-only** so the position is *derivable* from
  fills (an independent recompute that can catch state corruption).

### 4.3 Reconciliation — the broker is the source of truth (fixes L4)

```python
def reconcile(state: StrategyState, executor: Executor) -> ReconcileReport:
    broker_pos = executor.positions()         # Alpaca get_all_positions — TRUTH
    # 1. for each pending order, poll the broker; apply terminal fills/rejects to state
    # 2. compare state.positions to broker_pos; on mismatch, the BROKER WINS:
    #    - broker has a position state doesn't  -> adopt it (a fill we missed while down)
    #    - state has a position broker doesn't  -> it was closed server-side (stop filled
    #      while we were down) -> realize the P&L from the broker's closing fill, clear it
    # 3. record drift in a ReconcileReport (audited; a large drift alerts, never silently "fixes")
```

Reconciliation runs (a) at **startup** (restart-safety) and (b) **periodically** during trading. The
prior repo built this only for sim and never wired it live — we make it mandatory live and *also* run
it in backtest as a no-op invariant check (state and the sim book must already agree, so a failure
there catches an executor bug).

### 4.4 Restart-safety (the hard cases, handled explicitly)
Connecting to the platform's fc-relaunch / warm-start discipline (a restart must recover, never
double-act):
- **Restart with open positions:** `load()` from Postgres → `reconcile()` against the broker →
  resume managing. Idempotent `client_order_id`s mean re-submitting a still-needed order is a no-op at
  the broker.
- **Restart mid-order (submitted, fill unknown):** the pending order is in `state.pending`; `poll` by
  its `client_order_id` resolves the actual outcome from the broker; no re-submit, no double-fill.
- **Restart after a stop filled while down (the orphan, L4):** reconciliation sees the broker has no
  position where state did → realizes the close from the broker's fill → clears it. No "re-exit a dead
  trade."
- **Partial fill across a restart:** `PendingOrder.filled_qty` is persisted per poll; on restart the
  cumulative filled_qty from the broker is authoritative.

These are the exact scenarios the prior repo's §6 failure list enumerated — each now has a defined
path, because state is typed + single-source + broker-reconciled.

---

## 5. DataFeed + Clock (the remaining swap; unchanged from the portability doc)

- `DataFeed`: `PanelFeed` (replays the historical panel as per-timestamp cross-sections) vs `BusFeed`
  (the live feature-vector bus snapshot). Same event shape. (Prior repo's `BarDelivery` trait is the
  same idea.)
- `Clock`: `SimClock` (panel-driven, reproducible, no wall-clock — matches the feature-time rule) vs
  `RealClock` (wall-clock + the Alpaca market clock for hours/auction windows).

The `Runner` ties `{strategy.decide, StrategyState/StateStore, Executor, DataFeed, Clock}` — the ONE
loop both paths share.

---

## 6. Worked example — ONE strategy, ONE state model, both executors

Cross-sectional L/S overnight (the `overnight_beta` shape, already live). The decision + state +
intents are written once:

```python
# decide(): pure, columnar (STRATEGY_BATTERY_PORTABILITY.md) — unchanged
def decide(cs: CrossSection, state: StrategyState) -> list[OrderIntent]:
    legs = cross_sectional_ls.score_and_rank(cs, frac=0.2)      # one Polars/NumPy expression
    intents = []
    for leg in legs:                                            # target book -> intents (diff vs state)
        held = state.positions.get(leg.symbol)
        if held is None or sign(held.qty) != leg.side:
            intents.append(OrderIntent(
                client_order_id=coid(state.strategy_id, leg.symbol, cs.minute),  # deterministic
                symbol=leg.symbol, side=leg.side, notional=PER_LEG, type=MARKET, tif=CLS))
    return intents
```

**(a) Battery backtest** — `Runner(decide, MemoryStateStore, BacktestExecutor(cost_model), PanelFeed,
SimClock)`. The fast path applies `score_and_rank` BATCH over the whole panel (vectorized,
`run_vectorized`, <30–60s); the per-event reference path runs the SAME `decide` over `PanelFeed`
events and books fills through `BacktestExecutor` (simulated partials/cost) into the in-memory
`StrategyState`. The two are pinned equal (`test_batch_vs_per_event_select_identical_legs`).

**(b) Live container** — `Runner(decide, PgStateStore, PaperExecutor(alpaca), BusFeed, RealClock)`. A
THIN harness: on each cycle, `BusFeed` → `decide` (SAME code) → `PaperExecutor.submit` (real Alpaca
CLS order with the deterministic coid) → `reconcile` updates `StrategyState` from broker fills →
`PgStateStore.save`. On restart: `load` + `reconcile` resume.

The decision code, the `StrategyState` fields, and the `OrderIntent`s are byte-identical across (a)
and (b). Only `{StateStore, Executor, DataFeed, Clock}` are swapped. **That is the whole design.**

### Existing-container fit
`overnight_beta` / `reversion` / `smoke` already separate a pure decision core from an execution
harness (see `STRATEGY_BATTERY_PORTABILITY.md` §4). Re-expressing them in this shape is: (1) lift
their inline gate/cadence/cap logic into the `Executor`/`Runner` policy layer (it's execution-policy),
(2) replace their bespoke `BetStore`/`PositionStore` with the typed `StrategyState` + `PgStateStore`
(their stores are already per-strategy Postgres — a schema-shape change, not a logic change), (3) add
the mandatory `reconcile`. No decision-logic rewrite. Proposed as its own PR (no live-container
behavior change without its own PR + a per-container parity test).

---

## 7. Honest risks (the make-or-break tensions)

**R1 — Speed vs execution fidelity (THE key tension).** The battery must be <30–60s (a vectorized
batch sweep), but the `BacktestExecutor`'s faithful per-event fill simulation (partials, sequencing)
is naturally per-event. Resolution: the **decision** is vectorized (the expensive part — score + rank
over the panel), and the **fill simulation for the cross-sectional L/S archetype is also columnar**
(per-timestamp basket fills at the tradeable price + per-name half-spread = a group-by, already
`run_vectorized`). Partials/rejects for *liquid cross-sectional baskets* are rare and modeled as a
columnar cost adjustment, NOT a per-event loop. **The genuine tension is for PATH-DEPENDENT archetypes
(triple-barrier/streak), where the fill schedule IS sequential** — there the fast path is the shared
`quant_tick` Rust kernel (one kernel both backtest and live call), NOT a Python per-event loop and NOT
a separate fast-but-unfaithful sim. **If a future archetype needs intra-bar fill sequencing that
neither vectorizes nor fits the Rust kernel, that is the point to STOP and flag — do not add a
backtest-only fast-but-fake fill path.** This is the one place I would bring a go/no-go to Ben rather
than silently choose.

**R2 — Simulation fidelity is fundamentally limited by minute bars.** We cannot perfectly simulate
intra-minute fills/auctions from minute bars. Mitigation (not a hack, a measurement): explicit
conservative fill model + the live `PaperExecutor` logs realized slippage vs model (the `overnight_beta`
pattern), so the fidelity gap is *measured and tightened*, never assumed zero. The `BacktestResult`
surfaces the assumed cost so a PASS is honest about what it assumed.

**R3 — Reconciliation drift is real and adversarial.** The broker can close a position server-side
(margin, halt, risk). Design stance: broker is truth; large drift ALERTS (does not silently auto-fix);
the append-only fill ledger lets us recompute the position independently to catch corruption. We
accept that reconciliation can't be instantaneous — between cycles, state can lag the broker; the
window is bounded by the reconcile cadence and every order is idempotent so a lagged re-submit is safe.

**R4 — Partial-fill policy is a strategy decision, not a default.** "Filled 600 of 1000 — accept and
resize, or cancel-and-retry?" The prior repo had no answer. We make it an explicit, per-strategy
policy on the `Executor`/`Runner` (e.g. `on_partial: ACCEPT_RESIZE | CANCEL_REMAINDER | RETRY`),
defaulting to ACCEPT_RESIZE (book what filled, update `StrategyState` to the real qty), and the SAME
policy runs in backtest so the sim exercises it.

**R5 — Over-abstraction.** The prior repo's untyped `Value` KV state was *under*-typed (→ drift). The
opposite failure is a generic state DSL. Guard: `StrategyState` is a small typed dataclass with a
per-strategy `counters` dict, NOT a schema engine; the `Executor`/`StateStore` are ~3-method
protocols. Four mechanisms + a parameter grid (portability doc) + this typed state is the whole
surface.

---

## 8. Summary for the Lead / Ben

- **Prior art:** the trait-shapes (`TradeState`/`BarDelivery`/`Policy`) were RIGHT and we keep them.
  It broke on exactly the two hard parts: (L1/L2) the **sim and live were two execution paths** that
  never tested each other and ignored partial fills; (L3/L4) **state was an untyped blob split across
  Postgres+Redis with the dynamic part in the volatile store and no broker reconciliation**. Plus
  (L5) non-idempotent entry and (L6) lost fill metadata.
- **Executor:** ONE `Executor`/`OrderIntent`/`Fill` modeled on REAL Alpaca semantics (full lifecycle,
  partials, rejects, rate limits, idempotent coids, bracket orders); `BacktestExecutor` *faithfully
  simulates* it (tradeable price + per-name half-spread + modeled partials/rejects) and is pinned by a
  conformance test against `PaperExecutor`.
- **StrategyState:** ONE typed model, single durable source of truth (the strategy's Postgres schema),
  append-only fill ledger, **mandatory broker reconciliation** (broker = truth), restart-safe for the
  hard cases (mid-order, orphaned stop, partial across restart) — every prior failure scenario has a
  defined path.
- **Invariant preserved:** `decide()` written once, run batch + per-event; state + execution now also
  parity-by-construction. Rust appears ONLY for shared sequential kernels both sides call.
- **Key risk flagged, not hidden:** speed-vs-fill-fidelity for path-dependent archetypes (R1) — the
  one place to bring a go/no-go rather than silently fork; and minute-bar simulation fidelity (R2),
  mitigated by measuring the live-vs-model slippage gap.
- **No build this cycle** — this is the foundation for Lead/Ben review before Phase 0 internals.
