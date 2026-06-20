# Strategy-Battery → Production Portability (the shared-decision-core contract)

Status: VERIFICATION + DESIGN. Author: platform engineer, cycle 2026-06-19.
Precondition for building the battery (Ben, verbatim intent): *"verify that we can take the
strategies and easily port them to PRODUCTION without duplicating large parts of the code."*

This is the strategy-layer analogue of the platform's **parity-by-construction** principle (the same
reason live==backfill holds for features: one shared seed/fold/emit). A strategy's **decision logic**
(signal → bet, given a feature vector / panel + state) must be written **ONCE** and executed by BOTH:

1. the **BATTERY**, applied over a historical panel (the backtest), AND
2. a **LIVE STRATEGY CONTAINER**, applied to the feature-vector bus per cycle (the real bet).

The only legitimate difference between the two is the **execution harness** (panel iteration vs bus
subscription + broker order placement), never the decision logic.

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

**Battery (backtest) calls it** — over each historical panel timestamp:
```python
for minute in panel.timestamps():
    targets = core.decide(PanelCrossSection(panel, minute))   # SAME decide()
# → booked through long_short_per_name_cost (realistic per-name half-spread P&L) + the nulls/strata.
```

**Live container calls it** — the thin harness, ~the same size as `overnight_beta/strategy.py`:
```python
def cycle(self):
    self.consume()                                            # bus poll → latest_by_symbol
    targets = self._core.decide(BusCrossSection(self._latest))   # SAME decide(), zero re-code
    self.reconcile_to_targets(targets)                        # diff vs held → Alpaca MOC/MOO orders
```

The ENTIRE difference is `PanelCrossSection` vs `BusCrossSection` (each ~15 lines) and the booking
harness (P&L roll-up vs broker orders). **The `decide` body — the actual strategy — is one shared
function.** A battery PASS carries its `ArchetypeSpec` + the `signal_feature`/`model` ref; the live
container is instantiated from exactly that, so graduation is configuration, not coding.

---

## 4. Reconciliation verdict + the refactor

**The live containers already fit the contract** (Shape A: `reversion`/`smoke`; Shape B:
`overnight_beta`). They need only a one-time, mechanical lift to import the cores from the shared home
(`strategies/lib/` → `quantlib/strategy_core/`) so the battery can import them too without depending
on the `strategies/` deployment package. No logic changes; the cores are already pure + tested.

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

**Acceptance for the seam:** a test materializes one cross-section, builds BOTH a `PanelCrossSection`
and a `BusCrossSection` from the identical feature values, runs the SAME `decide`, and asserts the
target books are identical. That is the parity-by-construction proof for the strategy layer — the
strategy analogue of the feature stream==backfill parity test.

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
