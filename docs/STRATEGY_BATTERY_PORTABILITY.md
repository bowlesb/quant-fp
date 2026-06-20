# Strategy-Battery → Production Portability (the shared-decision-core contract)

Status: VERIFICATION + DESIGN. Author: platform engineer, cycle 2026-06-19.
Precondition for building the battery (Ben, verbatim intent): *"verify that we can take the
strategies and easily port them to PRODUCTION without duplicating large parts of the code."*

**Related docs:** this establishes the write-once `decide()` contract; `STRATEGY_EXECUTION_ABSTRACTION.md`
builds on it to design the production-real STATE + Executor + Feed (the gated next step), and
`STRATEGY_CONTAINERS.md` is the live per-strategy container contract on the feature-vector bus.

---

## THE INVARIANT (the central design driver — everything else follows from this)

**A strategy's decision logic is written ONCE, and is both EXECUTION-AGNOSTIC and SPEED-AGNOSTIC.**
It produces *identical* decisions whether applied:
- **(a) as a fast batch/vectorized sweep over the historical panel** — the laser-quick battery
  backtest (Ben's hard <30–60s budget), OR
- **(b) per-event over the live feature-vector bus** — production.

**The one thing that must NEVER happen: two implementations of the same decision logic** (a
fast-for-backtest one + a separate live one). That is backtest≠live drift at the strategy level — the
exact failure the feature platform's shared seed/fold/emit eliminates for features. This is
**parity-by-construction lifted to strategies**, and it is the make-or-break of the battery being
worth building: a battery PASS is only a promotable artifact if the live container runs *that same
decision*, not a re-coded cousin of it.

### The Rust-vs-Python rule this FORCES (apply everywhere)

The question for every decision component is **"will BOTH the fast batch backtest AND the live
container execute this EXACT code?"** — not "what's fastest?".

| Decision logic | How it stays single-implementation | Rust? |
|---|---|---|
| **COLUMNAR** (thresholds, cross-sectional ranks, score / linear combos — *most* strategies) | ONE pure Polars/NumPy expression that **vectorizes over the whole panel** (batch, fast) AND **applies to a single bus vector** (live). Same expression, two application modes. | **No** — Python/Polars, parity by construction. |
| **SEQUENTIAL-and-hot** (triple-barrier first-touch, streak runs) | ONE **shared Rust kernel in `quant_tick`** that BOTH the backtest AND the live container call — parity-pinned by a Python reference oracle (same pattern as the feature Rust tests). | **Yes — ONE shared kernel**, never a harness-only fast kernel + a live Python re-impl. |
| **NEVER** | a backtest-only fast path + a live-only slow path implementing the same decision | — if you're tempted to write the decision twice, **that is the smell to flag**, not silently fork. |

So the Rust decision is reframed around **shared-code portability, not speed**: Rust appears only
where the logic is inherently sequential AND both sides call the *same* kernel. Everything columnar
stays one Polars/NumPy expression applied two ways.

### What this means for `decide()`

`decide()` is designed **columnar-first**: a pure function of feature *columns* + state, expressible
as a Polars/NumPy expression, so the identical code runs (a) batch over the panel and (b) on a single
bus row. §3 shows this explicitly for the worked archetype — the same expression producing the
backtest signals AND the live per-minute signal — and §2.6 states the contract + flags the one risk
(if speed ever seems to need a fork, that is THE key risk to surface, not paper over).

---

This is the strategy-layer analogue of the platform's **parity-by-construction** principle (the same
reason live==backfill holds for features: one shared seed/fold/emit). The decision logic is written
**ONCE** and executed by BOTH:

1. the **BATTERY**, applied (batch) over a historical panel (the backtest), AND
2. a **LIVE STRATEGY CONTAINER**, applied (per-event) to the feature-vector bus (the real bet).

The only legitimate difference is the **execution harness + the application mode** (vectorized panel
sweep vs per-event bus), never the decision logic.

---

## 0. Headline finding

**The shared-core pattern Ben asked for ALREADY EXISTS in the live containers — and one of them
(`overnight_beta`) is the exact precedent for the battery's cross-sectional L/S archetype.** The
work is therefore NOT to invent a new abstraction; it is to (a) name the contract the live
containers already follow, and (b) build the battery's `Strategy` to call that SAME contract instead
of its own inline logic. With that, a battery-validated cell graduates to live by writing a thin
harness, with **zero re-implementation of the decision logic**.

Concretely, every live container already separates:

| Live container | PURE decision core (no I/O — the shared part) | Execution harness (the per-deployment part) |
|---|---|---|
| `reversion` | `VwapReversionModel.predict(vector)→Prediction`, `select_candidate(...)`, `evaluate_bet_gate(...)` | `ReversionStrategy` (bus poll, Alpaca orders, bet store, fill mgmt) |
| `smoke` | `MockMLModel.predict(vector)→Prediction` + threshold | `SmokeStrategy` (same harness shape) |
| `overnight_beta` | `OvernightBetaModel.select_legs(returns_by_name, market)→BetaLegs` | `OvernightBetaStrategy` (close/open auction, slippage log) |

The decision cores are already **pure, deterministic, no-wall-clock, NaN-safe, unit-tested** functions
over either a single `FeatureVector` (per-name shape) or a cross-sectional panel (the L/S shape). The
container's `__main__` is already a **thin harness**: `env → config + broker + store + MODEL +
panel/bus → strategy.run()` (see `strategies/overnight_beta/__main__.py:112`).

**The gap:** the Phase-0 battery `CrossSectionalLS` (as first built) computes its signal with its OWN
inline GBM/rank logic — it does NOT call a shared decision core. That is the one duplication to
remove. §4 specifies the refactor.

---

## 1. The two decision-core shapes (both already live)

A strategy's decision is one of two shapes. Both are already in `strategies/lib/`:

### Shape A — per-vector (single-name / threshold)
```python
class Model(Protocol):                        # strategies/lib/model.py
    def predict(self, vector: FeatureVector) -> Prediction: ...
```
`predict` reads features **by name** off ONE decoded `(symbol, minute)` vector and returns a score
(`Prediction.probability`). Selection (`select_candidate`) + the safety gate (`evaluate_bet_gate`)
are separate pure functions. This is `reversion` / `smoke`, and Ben's "probability-thresholded" and
"high-prob N-in-a-row" archetypes.

### Shape B — cross-sectional (the L/S basket — the battery's archetype 1)
```python
class CrossSectionalModel(Protocol):          # the contract to formalize (overnight_beta already fits)
    def select(self, panel: CrossSection) -> Legs: ...
```
`select` takes the WHOLE cross-section at one timestamp (per-name feature/return arrays) and returns
the long/short name sets (or per-name weights). This is EXACTLY `OvernightBetaModel.select_legs`
(`strategies/lib/overnight_beta_model.py:61`) and EXACTLY the battery's archetype 1 (rank top/bottom-k,
dollar-neutral EW). Ben's EOD / multi-day / sector / up-down-day are all this shape × parameters.

**Both shapes read features by NAME** — `vector.value("vwap_deviation_30m")` live, the same named
column in the panel in backtest. The name is the invariant that makes backtest==live.

---

## 2. The shared-decision-core contract

The contract that makes a decision core portable is a small, pure protocol that **takes a
point-in-time feature view + read-only state and returns intents (target positions), with NO I/O**:

```python
# proposed: quantlib/strategy_core/__init__.py  (the SHARED home both sides import)

@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    target_weight: float          # dollar-neutral weight in [-1, 1] (basket) OR notional sign (single)
    score: float                  # the model's conviction (rank/prob) — for sizing + logging

class DecisionCore(Protocol):
    """The ONE place a strategy's signal→intent logic lives. Pure: no bus, no broker, no DB, no
    wall-clock. Both the battery (backtest) and the live container call THIS, unchanged."""

    spec: ArchetypeSpec           # the promotable validation record (horizon/conditioner/sizing)

    def decide(self, cross_section: CrossSection) -> list[TargetPosition]:
        """Given the as-of-t cross-section (per-name named-feature reads + the conditioner inputs),
        return the target book. The battery calls this once per panel timestamp; the live container
        calls it once per cycle on the latest bus vectors. Identical code path."""
```

where `CrossSection` is a thin, source-agnostic view exposing exactly what a decision needs:

```python
class CrossSection(Protocol):
    symbols: list[str]
    minute: datetime
    def feature(self, name: str) -> np.ndarray:   # per-name values for a named feature, NaN-safe
    def feature_for(self, symbol: str, name: str) -> float:
```

- **Backtest adapter** (battery): wraps one timestamp-slice of the column-major `Panel` →
  `CrossSection`. The battery loops timestamps, calls `decide`, books the result through
  `long_short_per_name_cost` (the realistic-cost P&L) — §3.
- **Live adapter** (container): wraps the latest-by-symbol `dict[str, FeatureVector]` →
  `CrossSection` (each `feature(name)` is `[v.value(name) for v in latest]`). The container calls
  `decide`, diffs target vs held, and places the broker orders — §3.

`decide` never sees a `Panel`, a `BusConsumer`, an Alpaca client, or `datetime.now()`. That is what
makes it the single shared implementation. (Per-vector Shape-A cores are the degenerate case:
`decide` over a 1-name cross-section, or kept as the existing `predict` + `select_candidate`, which a
`CrossSection`-driven `decide` can call internally — no rewrite of `reversion`.)

---

## 2.5 The HOT-SWAP seams — `Executor` × `DataFeed` × `Clock` × `Runner`

This is the precise shape (the event-driven-backtester == live-trader pattern). The strategy's
`decide` is written ONCE; you **hot-swap the execution and the feed underneath it**. Four interfaces,
and a `Runner` that ties them — the strategy code never changes across the swap.

```python
# quantlib/strategy_core/  (the SHARED home)

@dataclass(frozen=True)
class OrderIntent:
    """What the strategy WANTS to transact this step — broker-agnostic. The Executor turns it into a
    simulated fill (backtest) or a real Alpaca order (live). Same intent in; only the fill differs."""
    symbol: str
    side: str                 # "buy" | "sell"
    target_weight: float      # the desired book weight (basket) ...
    notional: float | None = None   # ... or an absolute notional (single-name); exactly one is set
    reason: str = ""          # provenance for logging / the validation record

class Strategy(Protocol):
    spec: ArchetypeSpec
    def decide(self, cs: CrossSection, state: BookState) -> list[OrderIntent]:
        """PURE: given the as-of-t cross-section + the current book, return the orders to transact.
        No bus, no broker, no wall-clock. Called once per step by the Runner — identical live + backtest.
        MUST be columnar-friendly (see §2.6) so the BacktestExecutor can apply it across the panel fast."""

class Executor(Protocol):
    """THE pretend-trade vs actual-trade swap (Ben's explicit ask)."""
    def execute(self, intents: list[OrderIntent], cs: CrossSection, clock: Clock) -> list[Fill]: ...
    def book(self) -> BookState: ...
# BacktestExecutor(cost_model): simulates the fill over the panel at the tradeable entry (>=09:35),
#   charges the per-name half-spread + slippage, tracks position/P&L. (Reuses long_short_per_name_cost.)
# PaperExecutor(alpaca) / LiveExecutor(alpaca): submits the REAL order (paper today, real later),
#   captures the fill, reconciles the book. (This is exactly what reversion/overnight_beta do today.)

class DataFeed(Protocol):
    """Replays decision events. Same event shape out, different source."""
    def events(self) -> Iterator[FeedEvent]: ...      # FeedEvent = (CrossSection, ts)
# PanelFeed(panel):  yields each timestamp-slice of the historical panel as a CrossSection event.
# BusFeed(consumer): yields a CrossSection built from the latest-by-symbol bus vectors each cycle.

class Clock(Protocol):
    def now(self) -> datetime: ...
# SimClock(panel): time advances with the panel (the event ts).  RealClock(): wall-clock + market hours.

class Runner:
    """Ties {strategy, feed, executor, clock}. The ONE loop both paths share."""
    def __init__(self, strategy, feed, executor, clock): ...
    def run(self):
        for cs, ts in self._feed.events():
            intents = self._strategy.decide(cs, self._executor.book())   # SAME decide
            self._executor.execute(intents, cs, self._clock)
```

So:
```python
battery_backtest = Runner(strategy, PanelFeed(panel),  BacktestExecutor(cost_model), SimClock(panel))
live_container   = Runner(strategy, BusFeed(consumer), PaperExecutor(alpaca),        RealClock())
```
**SAME `Runner`, SAME `strategy.decide`, swapped components.** The live container becomes a thin
harness: wire env → `{strategy, BusFeed, PaperExecutor, RealClock}` → `Runner.run()`. No duplicated
decision logic. A battery PASS carries its `spec` (+ frozen model) so the live `Runner` is instantiated
from exactly the validated configuration — graduation is configuration, not code.

## 2.6 Performance — the BacktestExecutor must be VECTORIZED, not a per-event loop

Ben's <30–60s battery budget forbids a slow per-timestamp Python loop calling `decide` 300×/day ×
~250 days. The resolution: **`decide` is pure + columnar-friendly, so the `BacktestExecutor` applies
it in BATCH over the whole panel**, while the live `PaperExecutor` calls the identical logic per event.

Concretely, archetype 1's `decide` is `score → rank → top/bottom-k per timestamp`. All three are
group-by-timestamp columnar ops over the resident panel arrays (numpy/polars) — the `BacktestExecutor`
computes the entire book for ALL timestamps at once (the current battery already does this via
`long_short_per_name_cost`'s per-timestamp bucketing), NOT a Python `for ts` loop. The live executor
runs the SAME scalar `decide` on one cross-section per minute. The decision **logic** is identical;
the **application** is batch-vs-streaming — which is exactly the feature platform's batch-backfill vs
per-minute-stream split under one shared `emit`.

**Contract for `decide`:** it must be expressible as a pure function of `(score_vector, group_ids)` →
book, with no cross-step state that can't be carried as an explicit columnar `BookState` (e.g.
turnover is a diff of consecutive per-timestamp books — vectorizable). **Inherently-sequential
archetypes — triple-barrier first-touch, persistence/streak — CANNOT be applied in one columnar pass**
(the early-exit along the path is sequential); those are the Phase-1 **Rust** kernel (`first_touch`),
flagged here so the seam is built knowing batch-vectorization covers archetype 1 but not 2/3. The
`Executor` interface accommodates both: `BacktestExecutor` may run a vectorized batch path for
cross-sectional archetypes and the Rust per-path kernel for triple-barrier — the strategy `decide`
contract is the same; only the executor's internal application differs.

---

## 3. Worked example — ONE archetype, ONE core, BOTH paths

Take the cross-sectional L/S top/bottom-k (Ben's EOD / multi-day / the overnight-beta shape). The
decision core is written once:

```python
# quantlib/strategy_core/cross_sectional_ls.py  (SHARED — imported by battery AND container)
class CrossSectionalLS(DecisionCore):
    def __init__(self, spec: ArchetypeSpec, signal_feature: str | None = None,
                 model: RankModel | None = None) -> None:
        self.spec = spec
        self._signal_feature = signal_feature      # raw-feature fast path (rank this column)
        self._model = model                        # optional GBM ranker (opt-in deeper mode)

    def decide(self, cs: CrossSection) -> list[TargetPosition]:
        score = (self._model.rank(cs) if self._model
                 else cs.feature(self._signal_feature))      # by NAME — identical live + backtest
        order = np.argsort(score)                            # ascending conviction
        k = max(1, int(self.spec.frac * len(order)))
        longs, shorts = order[-k:], order[:k]
        out = []
        for i in longs:  out.append(TargetPosition(cs.symbols[i], +1.0/k, float(score[i])))
        for i in shorts: out.append(TargetPosition(cs.symbols[i], -1.0/k, float(score[i])))
        return out
```

THREE call sites share this ONE `decide` (the invariant made concrete):

**(1) Battery — FAST BATCH sweep over the panel** (the laser-quick backtest):
```python
# The columnar core (score = rank a named feature, or a frozen model.rank) vectorizes over the
# WHOLE panel at once — one Polars/NumPy expression, NO per-timestamp Python loop:
score   = panel.feature_matrix[:, col]                 # the SAME named-feature read, all rows
book    = batch_topk_per_timestamp(score, panel.minute_epoch, frac)   # group-by-ts rank, vectorized
# booked through BacktestExecutor.run_vectorized (per-name half-spread P&L + nulls/strata). <30-60s.
```

**(2) Battery — PER-EVENT reference** (faithful to live; pins the batch path):
```python
for cs, ts in PanelFeed(panel).events():               # one timestamp slice at a time
    intents = strategy.decide(cs, executor.book())     # the SAME decide()
    executor.execute(intents, cs, clock)               # BacktestExecutor: pretend fill over the panel
```

**(3) Live container — PER-EVENT over the bus** (production), a thin harness:
```python
for cs, ts in BusFeed(consumer).events():              # latest bus vectors as a CrossSection
    intents = strategy.decide(cs, executor.book())     # the SAME decide(), zero re-code
    executor.execute(intents, cs, clock)               # PaperExecutor: real Alpaca order
```

(2) and (3) are **literally the same `Runner` loop** with `{PanelFeed, BacktestExecutor}` vs
`{BusFeed, PaperExecutor}` swapped — `strategy.decide` is byte-identical. (1) is the same decision
*expression* applied batch instead of per-event.

**How parity between the batch form (1) and the per-event form (2)/(3) is GUARANTEED** — this is the
crux of the invariant, not an afterthought:
- **Columnar archetypes (this one):** the score is a pure function of feature columns
  (`score = signal_sign * cs.feature(name)`, or `model.rank(cs)`). Ranking top/bottom-k per timestamp
  is the same operation whether done once-per-timestamp (per-event) or grouped-by-timestamp over the
  whole panel (batch) — `numpy.argsort` within a group == the batch group-by rank. The **test
  `test_executor_swap_parity_backtest_vs_paper`** pins that the per-event `decide` over a
  `PanelCrossSection` and over a `BusCrossSection` built from identical data emit identical
  `OrderIntent`s; a batch-vs-per-event equivalence test pins (1)==(2) on the same panel. There is ONE
  scoring expression — `CrossSectionalLS.score` — and both application modes call it.
- **Sequential archetypes (Phase 1):** the score is a shared `quant_tick` Rust kernel
  (`triple_barrier_first_touch`); the batch backtest and the live container both call that SAME kernel,
  pinned by a Python reference oracle in `tests/test_fp_rust.py` (the existing feature-Rust pattern).
  There is NO harness-only fast kernel + live re-impl.

The ENTIRE difference across all three is the feed (`PanelCrossSection` vs `BusCrossSection`, ~15
lines each), the application mode (vectorized panel sweep vs per-event), and the executor (pretend P&L
roll-up vs broker orders). **The decision expression is one shared thing.** A battery PASS carries its
`ArchetypeSpec` + the `signal_feature`/frozen-`model` ref; the live container is instantiated from
exactly that — graduation is configuration, not coding.

---

## 4. Reconciliation verdict + the refactor

**The live containers already fit the contract** (Shape A: `reversion`/`smoke`; Shape B:
`overnight_beta`). They need only a one-time, mechanical lift to import the cores from the shared home
(`strategies/lib/` → `quantlib/strategy_core/`) so the battery can import them too without depending
on the `strategies/` deployment package. No logic changes; the cores are already pure + tested.

**Re-expressing each existing container as `Runner(strategy, BusFeed, PaperExecutor, RealClock)` —
the decision logic lifts UNCHANGED into `decide()`:**

| Container | Today | Lifts to `decide()` UNCHANGED | Executor / Feed it keeps |
|---|---|---|---|
| `reversion` | `select_candidate(model, latest_by_symbol, threshold, held)` → one long | `decide(cs, state)`: run `VwapReversionModel.predict` per name (already pure), pick the best clearing the threshold not already held → `[OrderIntent(sym,"buy",notional=...)]` | `PaperExecutor` = its current submit/manage/finalize loop; `BusFeed` = its `consume()` |
| `smoke` | gate + `MockMLModel.predict(latest)` → one buy | `decide`: the same predict+threshold on the 1-name cross-section → `[OrderIntent]` | same |
| `overnight_beta` | `select_legs(returns_by_name, market)` → long/short legs | `decide` IS `select_legs` (already cross-sectional, already pure) → one `OrderIntent` per leg name | `PaperExecutor` = its CLS/OPG auction loop; `PanelFeed`/`BusFeed` = its `PanelLoader` |

The lift is mechanical: the `evaluate_bet_gate` / cadence / cap logic stays in the **Executor** (it's
execution-policy, not signal); the `predict`/`select_candidate`/`select_legs` signal logic becomes
`decide`. **No rewrite — extract inline logic into `decide()`, wrap with the `Runner`.** Proposed as
its own PR (boundary: no live-container behavior change without a PR), with a parity test per container
(same `decide`, `PanelFeed`+`BacktestExecutor` vs `BusFeed`+`PaperExecutor`, identical intents on
identical data).

**The battery is what must change** (and it is new code, so this is cheap): build archetype 1 as a
`DecisionCore.decide` over a `CrossSection`, NOT the current inline GBM/rank in
`CrossSectionalLS.backtest`. The battery's existing pieces stay:
- the load-once column-major `Panel` (just add a `PanelCrossSection` timestamp-slice view),
- the discipline core wrap (`walk_forward`, the two nulls, NW-t, per-name cost, BY-FDR),
- the `BacktestResult` bundle + `SanityReport`.
Only the **signal computation** moves behind `decide` so it is the same object the live container runs.

`overnight_beta` is the existence proof this works: its `select_legs` is already the cross-sectional
decision core, already serves a live container, and is the same shape as the battery's archetype 1.
Porting the battery's archetype to call a `select_legs`-shaped core is the whole task.

### Walk-forward / model-fit caveat (stated, not hidden)
For the **raw-feature fast path** (`decide` ranks a named feature), backtest==live is exact — the same
`cs.feature(name)` ranking runs both sides. For the **GBM deeper mode**, the battery fits a model
walk-forward (train-on-past, predict-on-test); the live container loads a FROZEN trained model and
calls `model.rank(cs)` per cycle. The shared object is the *trained model's `rank`*; the battery's
walk-forward is a backtest-only training harness that PRODUCES that frozen model (exactly as features
are computed identically but the fold structure is backtest-only). This mirrors the feature platform:
shared `emit`, backtest-only fold orchestration. The graduating artifact is the frozen model + spec.

---

## 5. What gets built (Phase 0, to this contract)

1. `quantlib/strategy_core/` — `DecisionCore`/`TargetPosition`/`CrossSection` protocols +
   `CrossSectionalLS` decide-core (the raw-feature fast path now; the GBM ranker as `model=`).
2. Battery `Strategy.backtest` calls `core.decide` over `PanelCrossSection` slices (replacing the
   inline signal); everything else (cost, nulls, strata, BY-FDR, sanity) unchanged.
3. The live `BusCrossSection` adapter + a `reconcile_to_targets` harness mixin (so a future graduated
   container is the thin shell of §3) — adapter shipped + unit-tested now; a full new live container is
   a later cycle, but the seam is proven by a test that runs the SAME `decide` over a `PanelCrossSection`
   and a `BusCrossSection` built from the same data and asserts identical targets.
4. Existing `strategies/lib/` cores re-homed under `quantlib/strategy_core/` (mechanical; no logic
   change) so battery + containers share one import. (Proposed as a follow-up PR to avoid touching the
   live containers in the battery PR — boundary: no live-container behavior change without its own PR.)

**Layering (enforces the invariant):** `quantlib/strategy_core/` is **self-contained** — the pure
decide-cores + adapters + `Executor`/`DataFeed`/`Clock`/`Runner` + the cost model (`cost.py` lives
HERE, with the executor it serves). `quantlib/battery/` **depends on** `strategy_core`, never the
reverse, and the live containers will import the same `strategy_core` — so there is exactly one home
for the decision + execution logic both sides share. (The cost model was moved out of `battery/` into
`strategy_core/` for this reason; a `strategy_core → battery` import is the architectural smell that
would let the two diverge.)

**Acceptance for the seam (the invariant, pinned by tests):**
- `test_decide_parity_panel_vs_bus` — the SAME `decide` over a `PanelCrossSection` and a
  `BusCrossSection` built from identical data yields identical target books (execution-agnostic).
- `test_batch_vs_per_event_select_identical_legs` — the SAME scoring expression selects identical legs
  whether applied as the fast BATCH sweep (the battery's `run_vectorized`) or PER-EVENT (the live
  `Runner`) (speed-agnostic). **This is the proof there is no backtest-only fast path that can drift.**
- `test_executor_swap_parity_backtest_vs_paper` — the SAME `decide` emits identical `OrderIntent`s
  whether routed to `BacktestExecutor` (pretend) or a `PaperExecutor` (real broker).

Together these are the strategy-layer analogue of the feature stream==backfill parity test.

---

## 6. Summary for the Lead

- **Shared-core contract:** `DecisionCore.decide(CrossSection) -> list[TargetPosition]` — pure, no
  I/O, reads features BY NAME; both the battery and the live container call it unchanged. Two shapes
  (per-vector + cross-sectional); both already exist in `strategies/lib/`.
- **Do the existing containers fit?** YES. `overnight_beta.select_legs` is already the cross-sectional
  decision core serving a live container — the exact precedent for the battery's archetype 1.
  `reversion`/`smoke` already isolate `predict`+`select_candidate`+gate as pure functions. The lift is
  re-homing the cores to a shared module (mechanical, no logic change), proposed as its own PR.
- **Worked example:** §3 — one `CrossSectionalLS.decide` serving the battery backtest AND a live cycle;
  the only difference is a ~15-line `PanelCrossSection` vs `BusCrossSection` adapter + the booking
  harness. No duplicated decision block.
- **The refactor:** the BATTERY changes (move its signal behind `decide`); the containers don't (only
  re-homed). Net: a battery PASS graduates to live as configuration (spec + frozen model), not code.
- **Phase 0 will be built to this contract**, and ships the parity test (same `decide`, two adapters,
  identical targets) as the strategy-layer parity-by-construction proof — and still hits Ben's
  <30–60s battery budget (the `decide`-over-panel path is the same cost as the current inline rank).
