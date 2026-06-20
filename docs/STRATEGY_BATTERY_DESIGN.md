# Strategy-Battery / Edge-Evaluation Harness — Design

Status: DESIGN (no implementation). Author: platform architect cycle 2026-06-20.
Scope: a reusable abstraction that turns "I have a feature / feature-set" into
"verdict: which strategy archetypes show a real, tradeable edge — with evidence",
in one call, with the anti-fooling discipline built IN, not re-hand-rolled per experiment.

This generalizes the hand-rolled harnesses already in the repo:
`experiments/2026-06-18-trusted-substrate-baseline/` (cross-sectional baseline + stratified-IC),
`experiments/2026-06-19-laneC-scope-horizon/` (overnight multi-day panel),
`experiments/2026-06-19-volburst/` (triple-barrier two-sided), and the
`experiments/2026-06-20-signal-source-expansion/regime_screen.py` conditioning sweep —
on top of the existing discipline core `quantlib/backtest.py` + `quantlib/research.py` +
`quantlib/labels.py`. The battery does NOT replace that core; it orchestrates it.

---

## 0. Why this exists (the motivating lessons)

Three lessons from the edge hunt force the shape of this design. They are the reason
"AUC / IC" alone is the wrong gate:

1. **Predictive ≠ tradeable (the illiquid-tail trap).** B4 (overnight) had a real
   cross-sectional IC, but the IC concentrated in the illiquid/microcap tail — gated by
   auction cost and survivorship, NW|t| collapsed from 3.89 (full universe) to ~1.2 on the
   liquid-1500. A harness that reports one pooled IC number *hides* this. The battery must
   report **by-stratum (liquidity / spread) breakdowns by construction**, and gate on a
   **net-of-realistic-cost** backtest using the **per-name half-spread**, not a flat 2bp.

2. **Predictive ≠ directional (the symmetric-burst trap).** VolBurst hit AUC ~0.92 on
   "will |return| exceed 2% in X minutes" — but the burst was **direction-symmetric**, so the
   +2%/−1% triple-barrier trade was *negative-EV*. A harness that scores the magnitude label
   would have green-lit a losing strategy. The battery must distinguish **magnitude/volatility
   labels** (a sizing/timing input) from **directional/first-touch labels** (the thing you can
   actually bet), and only the latter graduates to a P&L verdict.

3. **Multi-day / overnight data traps.** Sub-$1 bad closing prints manufacture 50–226× fake
   returns; the fix (now standard) is a **$1 price floor + per-day symmetric winsorization +
   label-std sanity check**, plus a **tradeable entry ≥ 09:35 ET** (never the 09:30 print —
   the gap-fade look-ahead). These must be **guards the harness runs automatically**, not
   discipline each modeller re-remembers.

The unifying claim: *the discipline is the product.* The archetypes are cheap; getting the
look-ahead, cost, baseline, and trap-guards right every single time is what's expensive and
what modellers skip under time pressure. Move it inside the harness once.

---

## 1. The archetype taxonomy

### 1.1 The four orthogonal dimensions

Every strategy Ben named is a point in a 4-axis space. Naming the axes is what lets a
modeller reason about *coverage* ("what haven't I tested?") instead of a flat list.

```
ARCHETYPE = (HORIZON) × (LABEL / ENTRY) × (CONDITIONING / SELECTION) × (SIZING)
```

**Axis A — HORIZON (when does the bet resolve?)**
- Intraday short: 5m / 15m / 30m / 60m (the trusted-baseline + volburst regime).
- End-of-day: enter intraday or at close, resolve at the session close / MOC.
- Overnight: close → next-session-open (the laneC regime; entry = 15:59 close, exit = next 09:35).
- Multi-day: 2d / 3d / 5d holds (laneC by-horizon).
- *Path-bounded* (not a fixed horizon): triple-barrier — resolves on first-touch of an
  up/down/time barrier, so the effective horizon is data-dependent.

**Axis B — LABEL / ENTRY (what are we predicting, and how do we book it?)**
This is the axis the lessons above live on. Four families:
- **Directional forward return** — `close[t+h]/entry − 1`, optionally **cross-sectional excess**
  (minus the per-minute universe median). The trusted-baseline label. Tradeable as a L/S basket.
- **Magnitude / volatility** — `|forward return|`, realized range, `burst = |ret| ≥ θ`.
  *NOT directly tradeable* — a sizing/timing input. The battery flags any magnitude label as
  "descriptive only; cannot graduate to P&L" unless paired with a directional first-touch.
- **First-touch / triple-barrier** — given entry, simulate the forward path; the label is which
  barrier (upper +θ_u / lower −θ_d / time) is hit FIRST. Two-sided: "bet both a +2% and a −2%
  are likely, enter, hold for the +2%." This is path-dependent (the hot loop, §3).
- **Persistence / streak** — directional label conditioned on "the signal fired the same way N
  bars/days in a row" (Ben's "high-prob N-days-in-a-row"). A *selection* over a directional label.

**Axis C — CONDITIONING / SELECTION (which bets do we take?)**
This is where "only take high-prob bets" and "sector-limited" and "up/down-market-day" live.
A conditioner is a point-in-time as-of-t predicate or stratum; it never peeks forward.
- **Probability / score threshold** — take only bets where the model's calibrated p (or rank)
  exceeds τ. The selection vs. coverage trade-off curve is itself an output.
- **Cross-sectional rank selection** — top/bottom-k per timestamp (the L/S basket — already in
  `long_short_backtest`).
- **Stratum-limited** — restrict to a sector, a liquidity tercile, a spread tercile, a vol regime
  (the `regime_screen.py` / `stratified_ic.py` machinery, generalized).
- **Market-state-conditioned** — take bets only on up-market days / down-market days / high-VIX
  (a per-timestamp regime label).
- **Persistence-conditioned** — the streak selector from Axis B.

**Axis D — SIZING (how big, and netting?)**
- Equal-weight top/bottom-k, dollar-neutral (the existing baseline).
- Score-proportional (size ∝ |prediction|, capped).
- Vol-targeted (size ∝ 1/realized-vol — the `vol_scaled` label already hints at this).
- Single-name path trade (triple-barrier: one position, fixed size, exits on first-touch).

### 1.2 Ben's named list → points in the space

| Ben's name | A Horizon | B Label/Entry | C Conditioning | D Sizing |
|---|---|---|---|---|
| End-of-day betting | EOD | directional fwd (excess) | rank top/bottom-k | EW dollar-neutral |
| Multi-day | 2–5d | directional fwd (excess) | rank top/bottom-k | EW |
| Probability-thresholded | any | directional (calibrated p) | score threshold τ | score-prop |
| High-prob N-in-a-row | multi-day | directional | persistence streak ≥ N + threshold | EW / single-name |
| Triple-barrier two-sided | path-bounded | first-touch (+2%/−1%) | (optional) prob threshold | single-name path |
| Sector-limited | any | directional | stratum = sector | EW within sector |
| Up/down-market-day | any | directional | market-state regime | EW |

### 1.3 Genuinely-distinct archetypes vs. parameterizations

The discipline of the taxonomy is to **collapse parameterizations into a handful of true
archetypes**, so the battery is ~6–8 distinct *mechanisms*, not 40 knobs. The distinct ones:

1. **Cross-sectional L/S** (directional excess, rank-selected, EW) — the baseline mechanism.
   *Horizon, sizing, and stratum are PARAMETERS of this one archetype*, not new archetypes.
   EOD / multi-day / sector-limited / up-down-day / probability-threshold are all this archetype
   with a different (horizon, conditioner, sizing) tuple. This is the bulk of Ben's list.
2. **Single-name triple-barrier** (first-touch path, single position) — genuinely distinct: the
   label is path-dependent, the P&L is per-trade not per-cross-section, the hot loop is different.
3. **Persistence/streak** (selection over a directional label) — distinct *selection mechanism*
   (a sequential per-symbol scan), reusable across horizons.
4. **Time-series / single-name directional** (no cross-section: predict one name's own forward
   return; e.g. a per-symbol momentum/mean-reversion timing rule) — distinct because there is no
   cross-sectional demeaning and the unit of P&L is the name, not the basket.

Everything else is (archetype 1) × parameters. That collapse is the point: a modeller covers
"EOD, multi-day, sector, up-down-day, prob-threshold" by running **archetype 1 across its
parameter grid** — one mechanism, one disciplined implementation, many cells.

### 1.4 What the full battery covers

The battery = {archetype 1 over its (horizon × conditioner × sizing) grid} ∪ {2, 3, 4}. A single
feature-set run sweeps:
- horizons {15m, 30m, 60m, EOD, overnight, 2d, 3d}
- conditioners {none, sector, liquidity-tercile, spread-tercile, vol-regime, up/down-market-day,
  prob-threshold τ∈{.6,.7,.8}, persistence N∈{2,3}}
- sizing {EW, score-prop, vol-target}
- plus triple-barrier {(+2%/−1%), (+1%/−1%)} × {5m,20m,30m} and the single-name timing rule.

That is the "established ways to look for edges of many types" Ben asked for. A modeller who
invents a feature does not decide *whether* it adds value — they run the battery and read which
**cells** lit up (and the §6 multiple-comparisons guard tells them which lit cells to believe).

---

## 2. The core abstraction

### 2.1 The one-call entry point

```python
# quantlib/battery/__init__.py  (proposed)
def evaluate_features(
    feature_set: FeatureSetRef,        # store pointer: (group/version list) OR a wide panel
    date_range: tuple[date, date],
    *,
    archetypes: list[ArchetypeSpec] | None = None,   # None -> the PRE-REGISTERED default battery
    universe: UniverseSpec = TRUSTED,                 # symbol filter + liquidity floor
    seed: int = 13,
) -> BatteryReport:
    ...
```

A modeller writes exactly this and reads the returned `BatteryReport`. They do not touch
walk-forward, shuffle, cost, winsorization, or entry-time logic — those live inside.

### 2.2 The Strategy interface (every archetype implements it)

```python
class Strategy(Protocol):
    spec: ArchetypeSpec                              # horizon, label, conditioner, sizing (the §1 point)

    def label(self, panel: Panel) -> Labels:
        """Produce the forward/path label POINT-IN-TIME. Vectorized (Polars/NumPy) for
        forward-return & magnitude; calls the Rust kernel for first-touch / streak."""

    def signal(self, panel: Panel, fold: Fold) -> Predictions:
        """Train-on-train, predict-on-test via the SHARED walk-forward core. For a single
        feature this may be the raw feature value (no model); for a set, a GBM."""

    def backtest(self, preds: Predictions, panel: Panel) -> BacktestResult:
        """Net-of-cost P&L for THIS archetype's sizing/selection. Reuses
        long_short_backtest for basket archetypes; a per-trade roll-up for triple-barrier."""
```

Crucially `label` / `signal` / `backtest` all consume a **shared, pre-loaded `Panel`** (§3.4) and
the **shared `walk_forward_folds`** — so adding an archetype is ~50 lines, and the leakage core is
written exactly once.

### 2.3 `BacktestResult` — the rigorous bundle

This is the heart. A result is NOT a Sharpe number; it is an evidence bundle that makes the three
traps un-foolable. Every field below maps to a lesson from §0.

```python
@dataclass
class BacktestResult:
    spec: ArchetypeSpec

    # --- headline economics (net of REALISTIC cost) ---
    net_per_period: float            # after per-name half-spread + borrow + slippage
    gross_per_period: float
    sharpe_net: float
    hit_rate: float
    mean_turnover: float
    breakeven_cost_bps: float        # the economic gate IC hides

    # --- the two null baselines (the only legitimate "no-skill" lines) ---
    shuffle_canary: NullStat         # within-timestamp label shuffle -> ~0 by construction
    predict_zero: NullStat           # constant prediction -> IC 0; P&L = pure cost drag
    edge_vs_shuffle: float           # real_ic - shuffle_ic ; the trust arbiter

    # --- breakdowns (trap #1: the illiquid tail) ---
    by_stratum: dict[str, StratumStat]   # liquidity tercile, spread tercile, sector
    by_regime: dict[str, StratumStat]    # up/down-market-day, high/low-VIX
    #   each StratumStat repeats {real_ic, shuffle_ic, nw_t, net_per_period, breakeven_cost_bps,
    #   n_names} so "the edge is ONLY in the illiquid tail" is visible at a glance.

    # --- cost-sensitivity curve (trap #1) ---
    cost_curve: list[tuple[float, float]]   # (one_way_cost_bps, net_per_period); where does it die?
    cost_used_bps: float                    # the per-name half-spread actually charged

    # --- significance (overlap-aware) ---
    mean_ic: float
    nw_t: float                       # Newey-West t on the per-timestamp IC series
    n_test_ts: int
    n_rows: int

    # --- directionality check (trap #2) ---
    directional: bool                 # False for magnitude/volatility labels -> NO P&L verdict
    up_vs_down_asymmetry: float | None  # for first-touch: P(up first) - P(down first); symmetric -> kill

    # --- data-trap sanity (trap #3, auto-run) ---
    sanity: SanityReport
    #   price_floor_applied: bool ($1 floor)
    #   winsorized: bool (per-day symmetric)
    #   label_std: float  + label_std_ok: bool (catches the 145x fake-return blow-up)
    #   entry_minute_ok: bool (>= 09:35 ET, never the 09:30 print)
    #   tradeable_fraction: float (rows that pass the liquidity floor)

    verdict: Verdict   # PASS / FAIL / DESCRIPTIVE-ONLY / TRAP-FLAGGED, with the reason string
```

`NullStat` and `StratumStat` are small records of `{ic, nw_t, net_per_period, n}`. `SanityReport`
is the auto-guard from §0.3.

### 2.4 `BatteryReport` — the comparable verdict

```python
@dataclass
class BatteryReport:
    feature_set: FeatureSetRef
    date_range: tuple[date, date]
    results: dict[ArchetypeKey, BacktestResult]   # one per cell in the battery
    family_correction: FamilyCorrection           # BY-FDR across the WHOLE battery (§6)
    leaderboard: list[ArchetypeKey]               # cells PASSING after correction, ranked by net Sharpe
    summary_md: str                               # human-readable, dashboard-renderable
```

The modeller's loop is then exactly:

```python
report = evaluate_features(my_new_feature_set, ("2025-01-01", "2026-06-17"))
print(report.summary_md)        # which archetypes lit up, AFTER multiple-comparison correction
```

and the answer to "does my feature add value?" is `report.leaderboard` (empty = honest null;
this is the *expected and healthy* outcome, given the edge hunt's track record).

---

## 3. The performance architecture

### 3.1 The genuine hot loop vs. the vectorizable rest

Profiling the existing hand-rolled code tells us exactly where time goes. Two classes:

**VECTORIZABLE (Polars / NumPy — Rust is OVERKILL here):**
- Forward return `close[t+h]/entry − 1` — a self-join on shifted timestamp. `build_dataset.py`
  already does this in Polars (`forward_returns`, lines 94–106). Stays Polars.
- Cross-sectional excess / median / winsorization — group-by per timestamp. Polars
  (`cross_sectional_excess_frame`). Stays Polars.
- Magnitude / |return| / realized range — element-wise. NumPy.
- Cross-sectional rank-IC, the L/S basket P&L roll-up, Newey-West — already pure-Python over
  per-timestamp dicts in `quantlib/backtest.py`, fast enough (it iterates timestamps, not ticks).
  **Keep as-is.** Rust here would save milliseconds and cost weeks.
- The GBM fit/predict — LightGBM, already native. Not our code to speed up.

**PATH-DEPENDENT (the genuine hot loop — Rust EARNS its keep):**
- **Triple-barrier first-touch.** `volburst/build_samples.py` lines 215–244 is a Python inner
  loop: for each entry row, slice the forward high/low path, find the FIRST index where
  `up_ret ≥ +θ` vs `dn_ret ≤ −θ`, decide the winner. This is O(rows × horizon) with an inner
  `np.where(...)[0][0]` per row — the single most expensive thing in the volburst build, and it
  does NOT vectorize cleanly (first-touch is inherently sequential along the path; the early-exit
  is the whole point and a full vectorized `cumany` wastes it). At 7700 symbols × ~300 entries/day
  × 18 months × 3 barrier configs this is the dominant cost. **Rust kernel.**
- **Persistence / streak** — per-symbol sequential scan counting same-sign runs (the
  `tick_run_features` shape already in `lib.rs`). Sequential by definition. **Rust kernel.**
- **Intraday path simulation** for single-name path trades (mark-to-market along the path,
  apply the barrier, book the realized exit). Same first-touch kernel.

### 3.2 Direct answer to Ben: "Rust? Overkill?"

**Per-component, not a blanket yes/no:**

| Component | Verdict | Why |
|---|---|---|
| Forward-return / excess / winsor labels | **Polars — Rust overkill** | self-join + group-by; Polars is already columnar/native and parity-proven in `build_dataset.py` |
| Magnitude / range labels | **NumPy — Rust overkill** | element-wise |
| Rank-IC / L/S P&L / Newey-West roll-up | **Pure Python — fine as-is** | iterates timestamps (hundreds), not ticks (millions) |
| **Triple-barrier first-touch** | **Rust — earns its keep** | sequential path scan with early-exit, O(rows×horizon), the dominant cost; cannot vectorize without throwing away the early-exit |
| **Persistence / streak selector** | **Rust — earns its keep** | inherently sequential per-symbol run-length; `tick_run_features` is the same shape |
| Intraday path-sim for single-name trades | **Rust — earns its keep** | same first-touch kernel |
| Panel load / windowed reductions | **Already Rust** (`windowed_reduce`, `rolling_extrema`) | reuse — don't rebuild |

So: **Rust is NOT overkill, but it is needed for exactly ONE new kernel family — the
path-dependent first-touch / streak scan** — plus reuse of the existing `windowed_*` kernels.
Everything else is Polars/NumPy. This is why the MVP (§5) can ship the cross-sectional battery in
pure Polars *before* any Rust, and add the Rust kernel only when the triple-barrier archetype is
turned on. The Rust verdict is "yes, but narrowly and later," not "rewrite the harness in Rust."

### 3.3 The first-touch kernel signature (illustrative — the one new Rust function)

It mirrors the existing `rolling_extrema` layout (parallel `(symbol, minute, value)` arrays,
single ordered pass, per-symbol blocks), so it slots into `rust/src/lib.rs` and the
`quant_tick` maturin module with zero new infra:

```rust
/// First-touch triple-barrier over per-(symbol, minute) bar paths. Inputs are PARALLEL arrays
/// sorted by (symbol, minute): symbol code, minute, entry-eligible flag, and the forward HIGH/LOW
/// path. For each entry row it scans forward up to `horizon` minutes and returns which barrier was
/// touched first: +1 (upper +theta_u), -1 (lower -theta_d), 0 (time barrier / no touch), plus the
/// minutes-to-touch and the realized exit return. One ordered pass per symbol block; early-exits on
/// first touch. A pure-Python reference (the volburst loop) pins the output for parity, exactly like
/// tests/test_fp_rust.py pins tick_run_features.
fn triple_barrier_first_touch(
    symbol: Vec<i64>, minute: Vec<i64>, entry_ok: Vec<bool>,
    high: Vec<f64>, low: Vec<f64>, close: Vec<f64>,
    theta_up: f64, theta_dn: f64, horizon: i64,
) -> (Vec<i64> /*barrier*/, Vec<i64> /*mins_to_touch*/, Vec<f64> /*exit_ret*/);
```

Parity is by construction: the Python volburst loop becomes the reference oracle in
`tests/test_fp_rust.py`, the same pattern that already pins `tick_run_features`.

### 3.4 The panel data structure (fast windowed look-ahead, load once)

The battery must iterate over time-series × many tickers and "do something like look-ahead
projection" without re-loading. The layout:

- **In memory: a column-major `Panel`** = parallel NumPy/Arrow arrays sorted by `(symbol, minute)`,
  symbol as an integer code (a contiguous block per symbol — exactly what every `lib.rs` kernel
  already assumes). Columns: `symbol_code, minute_epoch, <features...>, high, low, close, volume,
  half_spread`. This is the *same* layout the Rust kernels consume, so labeling and look-ahead
  share it with zero conversion.
- **Look-ahead projection** = a forward window per (symbol, minute). For vectorizable labels this
  is a shifted self-join; for path-dependent labels it is the Rust forward scan over the
  per-symbol block. Both read the SAME arrays.
- **Load once, evaluate many.** `evaluate_features` loads the panel ONCE from the feature store
  (point-in-time parquet, per `build_dataset.py`'s glob-by-date join) + the raw bars for the
  exit/path prices, then runs ALL archetypes against the resident arrays. The battery never
  re-reads the store per archetype — the cross-product of {horizons × conditioners × sizing} is a
  loop over the in-memory panel. This is the key to "extremely fast": one I/O pass, N archetype
  evaluations.
- **Memory bound:** the existing builders go one-date-at-a-time to bound memory; the battery keeps
  that for the *build* (panel assembly) but, once a date-range panel is materialized to a single
  Arrow table (or a memory-mapped parquet), evaluation streams over it. For the full
  7700-sym × 18-mo minute grid the panel is large; the battery supports a `universe`/`date_range`
  slice and a "daily-reduced" panel (one row per symbol/day) for the EOD/overnight/multi-day
  archetypes (laneC already proves this two-stage reduce → panel pattern).

---

## 4. How this kills the repeatable hand-rolling

Today every experiment re-implements the same scaffolding. The battery removes each, once:

| Hand-rolled today (per experiment) | Built-in (write once) |
|---|---|
| `walk_forward_folds` purge wiring | `Strategy.signal` calls the shared core; modeller never sees it |
| within-timestamp shuffle canary | every `BacktestResult.shuffle_canary`, automatic |
| predict-zero baseline | every `BacktestResult.predict_zero`, automatic |
| tradeable-entry ≥ 09:35 filter | `SanityReport.entry_minute_ok`, enforced in `label` |
| $1 floor + per-day winsor + label-std check | `SanityReport`, auto-run, blocks the verdict if it trips |
| per-name half-spread cost (vs flat 2bp) | `cost_used_bps` + `cost_curve`, pulled from the panel's `half_spread` column |
| stratified-IC by liquidity/spread/sector | `BacktestResult.by_stratum`, every run |
| Newey-West overlap-aware t | already `nw_t`, surfaced everywhere |
| bespoke verdict markdown per experiment | `BatteryReport.summary_md`, standardized + comparable |

**Standardized + comparable across features.** Because every feature-set runs the *same* battery
with the *same* nulls and cost model, two features' `BatteryReport`s are directly comparable —
a leaderboard across features becomes possible, which hand-rolled one-off scripts never allowed.

**Feature-store integration.** `FeatureSetRef` points at `(group, version)` dirs in `/store`
(the `build_dataset.py` `trusted_groups()` / `group_version_dir()` pattern) or a `feature_sets`
version row. The battery pulls the point-in-time panel itself — the modeller names a feature-set
and a date range, nothing more.

**Graduation to live.** A battery-validated cell carries its full `ArchetypeSpec` (horizon, label,
conditioner, sizing) + its `BacktestResult` as the validation record. That spec is exactly what a
live strategy container needs to subscribe to the feature-vector bus and place the bet — so a
PASS in the battery is a *promotable artifact*, not just a number in a notebook. (Ties into
`docs/STRATEGY_CONTAINERS.md` / the feature-vector bus.)

---

## 5. Phased build plan

The smallest first slice that delivers value, and what proves the abstraction before scaling.

### Phase 0 — MVP: the cross-sectional battery in pure Polars (NO Rust)
*Goal: prove the abstraction with the 3–5 highest-value archetypes, reusing existing code.*

- Lift `quantlib/backtest.py` + `research.py` UNCHANGED as the discipline core (already done — just
  wrap, don't rewrite).
- Implement archetype 1 (cross-sectional L/S directional) + its parameter grid:
  horizons {30m, 60m, EOD, overnight}, conditioners {none, sector, liquidity-tercile,
  up/down-market-day}, sizing {EW}. This single archetype + grid already covers ~5 of Ben's 7
  named strategies.
- Implement `BacktestResult` with the full bundle EXCEPT path-dependent fields; wire the
  `SanityReport` guards (reuse laneC's $1 floor + winsor + label-std; reuse build_dataset's
  ≥09:35 entry).
- Implement the per-name half-spread cost + `cost_curve` (the spread column already exists in the
  order-flow panel).
- `evaluate_features` one-call entry + `BatteryReport.summary_md`.
- **Proof the abstraction is right:** re-run the EXISTING trusted-substrate-baseline and laneC
  overnight findings THROUGH the battery and reproduce their published verdicts (baseline null;
  laneC full-univ HIT / liquid-1500 collapse). If the battery reproduces the hand-rolled numbers,
  the abstraction is faithful. This is the acceptance test for Phase 0.

### Phase 1 — the Rust path-dependent kernel
*Goal: add the genuinely-distinct triple-barrier archetype, the one place Rust earns its keep.*

- Add `triple_barrier_first_touch` (§3.3) to `rust/src/lib.rs`; pin parity against the volburst
  Python loop in `tests/test_fp_rust.py`.
- Implement archetype 2 (single-name triple-barrier) + the directionality / up-vs-down-asymmetry
  check (so the symmetric-burst trap is caught automatically — VolBurst would now be auto-flagged
  DESCRIPTIVE-ONLY).
- Add archetype 3 (persistence/streak) reusing the run-length kernel shape.
- **Proof:** re-run volburst through the battery; it must return DESCRIPTIVE-ONLY / negative-EV,
  matching the hand-rolled verdict.

### Phase 2 — full battery + feature-store integration + family correction
- The full conditioner grid (spread tercile, vol regime, prob-threshold τ, persistence N).
- `FeatureSetRef` → store-pulled panel (the `build_dataset.py` join, generalized + cached).
- Archetype 4 (single-name time-series directional).
- `FamilyCorrection` (BY-FDR across the whole battery — §6), the leaderboard, the dashboard render.
- **Proof:** a brand-new feature-set runs end-to-end in one call and produces a corrected
  leaderboard with no hand-rolling.

### Phase 3 (later) — graduation hook
- A PASS cell emits a live-strategy spec for the strategy-container bus.

**Smallest valuable slice = Phase 0.** It is shippable, reuses the proven core, and its
acceptance test (reproduce two existing published verdicts) is objective.

---

## 6. Honest risks / trade-offs

**1. Multiple comparisons — running 40 cells IS p-hacking unless defended.**
This is the single biggest risk: a battery that runs 40 archetype-cells will, by chance, light up
~2 at p<0.05 on pure noise. Defenses, all built-in:
- **BY-FDR (Benjamini–Yekutieli) across the WHOLE battery** — the dependent-test FDR control, since
  the cells share the panel and overlap (the existing `family_correction.py` in the trusted-baseline
  is the seed). The leaderboard reports only cells surviving correction.
- **Pre-registration of the default battery.** The default `archetypes=None` set is FIXED and
  versioned; a modeller running the default isn't choosing cells post-hoc. Custom archetype lists
  are flagged "exploratory, not pre-registered" in the report.
- **The shuffle canary per cell** is a per-cell null that BY-FDR sits on top of — a cell must beat
  BOTH its own shuffle AND survive family correction.
- **Honest framing:** the battery's *expected* output is mostly empty leaderboards (the edge hunt's
  track record is mostly nulls). The report says so explicitly, so a lone surviving cell is treated
  with suspicion, not celebration.

**2. Build cost vs. iteration-speed payoff.**
The MVP is ~1–2 weeks (it's mostly wrapping existing code). The Rust kernel is the only genuinely
new low-level work (~few days incl. parity test). The payoff: every future feature experiment drops
from "re-hand-roll the harness (days, error-prone)" to "one call (minutes)". Given the edge hunt
runs many experiments per week, the breakeven is fast. The risk is building Phases 2–3 *before*
Phase 0 proves the abstraction — hence the explicit Phase-0 acceptance test (reproduce existing
verdicts) before any scaling.

**3. What could make it over-engineered.**
- **Premature Rust.** Writing path-dependent kernels before the cross-sectional battery is used
  would be over-engineering — Phase 0 is deliberately Rust-free.
- **Too many archetypes too early.** The §1.3 collapse (6–8 mechanisms, not 40) is the guard; if
  the taxonomy ever grows a 12th "archetype" that's really archetype-1-with-params, reject it.
- **A config DSL for archetypes.** Resist a generic strategy-DSL; `ArchetypeSpec` is a small frozen
  dataclass, not a mini-language. Four mechanisms + a parameter grid is enough.
- **Re-implementing the discipline core.** The core (`backtest.py`) is proven and tested; the
  battery WRAPS it. Any rewrite of walk-forward/shuffle/Newey-West is a red flag.

**4. The panel-size cliff.** The full 7700-sym × 18-mo minute panel is large; loading it all for an
intraday battery may exceed memory. Mitigation: the `universe`/`date_range` slice + the
daily-reduced panel for EOD/overnight/multi-day (laneC's two-stage pattern). The intraday-minute
battery runs on a liquid-universe slice, which is also the *only* tradeable universe anyway (per
trap #1) — so the slice is a feature, not a limitation.

---

## 7. Summary for the Lead

- **Taxonomy:** 4 orthogonal axes — HORIZON × LABEL/ENTRY × CONDITIONING/SELECTION × SIZING —
  collapsing to **~6–8 distinct mechanisms** (cross-sectional L/S; triple-barrier first-touch;
  persistence/streak; single-name time-series), with everything else a *parameterization* of the
  cross-sectional archetype. The battery = that one archetype's (horizon × conditioner × sizing)
  grid ∪ the three distinct mechanisms.
- **Core abstraction:** `evaluate_features(feature_set, date_range) -> BatteryReport`; each cell a
  `Strategy(label/signal/backtest)` over a shared point-in-time `Panel` + the shared walk-forward
  core; the rigorous `BacktestResult` bundle (net-of-per-name-cost P&L, shuffle + predict-zero
  nulls, by-liquidity/spread/sector/regime breakdowns, cost-sensitivity curve, directionality
  check, auto data-trap `SanityReport`) makes all three historical traps un-foolable.
- **Rust verdict (per-component):** **NOT a blanket yes.** Polars/NumPy for all the
  vectorizable labeling + the P&L roll-up (Rust would be overkill); **Rust for exactly one new
  kernel family — the path-dependent first-touch / streak scan** — plus reuse of the existing
  `windowed_*`/`rolling_extrema` kernels. "Yes, but narrowly and later," not "rewrite in Rust."
- **MVP slice:** Phase 0 — the cross-sectional L/S archetype + grid in **pure Polars, no Rust**,
  wrapping the unchanged `quantlib/backtest.py` core; acceptance = **reproduce the existing
  trusted-baseline-null and laneC overnight HIT/collapse verdicts** through the battery.
- **Top risks:** (1) multiple-comparisons p-hacking across 40 cells → BY-FDR + a pre-registered
  default battery + per-cell shuffle + honest "expect empty leaderboards" framing; (2) building
  Phases 2–3 before Phase 0 proves the abstraction; (3) over-engineering via premature Rust / a
  strategy-DSL / too many archetypes — guarded by the mechanism-collapse and the Rust-free MVP.
```
