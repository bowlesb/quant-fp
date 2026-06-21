# Feature efficiency audit — every group's OPTIMAL state-holding design vs its CURRENT one

> The roadmap behind Ben's incremental-state mandate (memory `feedback-incremental-state-abstraction-mandate`):
> **every feature group is exactly one of** — (A) intraday-INVARIANT → compute ONCE/day + cache (~0/min),
> (B) DECOMPOSABLE → seed once + O(1) per-minute FOLD on held state (NOT a window re-scan), or Rust-resident
> for sequential-hot kinds. "Fast and cheap by construction." This doc audits all 62 measurable groups against
> that ideal, produces a ranked GAP LIST (where the CURRENT realization re-computes when it could carry state),
> and proposes the STANDING GATE that forces a new feature onto its optimal design.
>
> Pairs with: `docs/feature_latency_expectations.json` (the living p50/p99 + kind/incremental_ready — the
> source of the ms numbers cited here), `docs/INCREMENTAL_READINESS.md` (the per-group mechanism/lever detail),
> `docs/STATE_ABSTRACTION.md` (the seed/fold/StatefulGroup abstraction), `docs/latency_budget.yaml` (the gate).
> Numbers are the single-shard profiler view (312 sym × 245 min, with trades) — RELATIVE ranking, not the e2e
> bar→vector (which over-counts the B reductions' shared emit + excludes gather/IPC).

## The verdict in one paragraph

The platform is **mostly already on its optimal design.** The 23 ReductionGroups ride a shared O(1) running-sum
fold (`WindowedSumState`) — the only lever left is the Lead-gated `FP_INCREMENTAL` flip (20/23 ready, 3 parked
on the corr-denom-straddle). The 4 StatefulGroups are resident Rust EMA/lag/extrema folds. The Class-A
cached/static groups (24 of them) compute once per session and broadcast. The gather groups run once in the
reader phase, not per-bet. **The ONE production feature that fails the bar outright is `swing`** — it carries
NO state across minutes and re-folds its whole buffer every minute. That is the #1 gap and is fixed in this same
PR (see §"Swing — the fixed #1 gap"). The remaining gaps are a SECOND tier: hand-written window-re-aggregation
groups that re-scan their *bounded* window each minute (correct but O(window), not O(1)); these are small
absolute costs and only worth migrating opportunistically.

## How each group was classified

For every group I checked, in the code: does it override `compute_latest`? does it slice to a bounded window
(`compute_latest_on_window` / `reduce_buffer_minutes`) or carry running state, or does it fall through to the
DEFAULT `compute_latest` (= run the whole-buffer `compute()` then drop all but the last row)? The default is the
worst case — O(buffer) per minute to keep one row. The optimal design per kind:

| kind | optimal per-minute design | "on it?" today |
|---|---|---|
| **A — intraday-invariant** | compute once/session, cache, broadcast (~0/min) | YES — all 24 cached/static + gather groups |
| **B — windowed reduction** | shared running-sum fold, O(1)/min, read at T | YES (mechanism) — gated on `FP_INCREMENTAL` flip for the live payoff |
| **B — sequential-hot fold** | resident Rust kernel carrying per-symbol state, O(1)/min | YES — the 4 StatefulGroups; **NO — swing (re-folds whole buffer)** |
| **B — own latest-only / session-cumulative** | one aggregate-at-T, no per-minute scan | YES — the latest-only/session groups |
| **hand-written window** | carry a per-symbol fold OR slice to the bounded window | PARTIAL — sliced to window (O(window)), not O(1) |

## THE RANKED GAP LIST (where CURRENT ≠ OPTIMAL)

Ranked by per-minute saving (the cost the optimal design removes). "Cost" = the JSON's p50/p99 ms (single-shard
profiler). Saving is the part that is *re-computed* and could be *carried*.

### Tier 0 — the outright bar-failure (fixed in this PR)

| group | feat | current | p50 / p99 (ms) | optimal | why it's the worst | saving |
|---|---|---|---|---|---|---|
| **`swing`** | 9 | **NO `compute_latest` override** → default runs the FULL `swing_fold` over the WHOLE ring every minute, keeps 1 row | 28.7 / 42.4 | carry the per-symbol leg-state, fold ONLY the new minute (O(1)) | the Rust kernel's per-bar fold IS O(1), but the live path re-invokes it over the entire buffer each minute (re-folds 245 bars to keep 1). Its state needs the WHOLE session (n_pivots_today, leg ring) so window-slicing is unsafe — only carried state works | **measured 26.1→15.5ms p50 (1.7×) with the group-local carry; full O(1) (~2ms) needs delta-passing — see plan** |

`swing` is unique: it is the only production group whose live `compute_latest` re-scans an UNBOUNDED (whole-session)
buffer. Every other window group at least slices to its bounded declared window. INCREMENTAL_READINESS.md /
feature_latency_expectations.json labelled swing "Rust-resident / DONE" — that is true of the *kernel* but NOT of
the *live invocation*, which re-folds the whole buffer. This audit corrects that and the fix lands here.

### Tier 1 — hand-written window groups that re-aggregate their BOUNDED window each minute

These override `compute_latest` and slice to their declared window (so NOT a whole-buffer scan — the Tier-0
disease), but they re-run a polars group-by over that window every minute instead of carrying a per-symbol fold.
Correct, bounded, but O(window) not O(1). Migrating each to a carried CumulativeState/tick-ring kind would make
them O(1); the absolute saving is modest (small windows), so these are opportunistic, not urgent.

| group | feat | current | p50 / p99 (ms) | optimal | saving |
|---|---|---|---|---|---|
| `subminute_gap_fano` | 1 | `compute_latest_on_window` 60m re-aggregate (per-minute tick group-by) | 63.6 / 87.5 | tick-ring kind carrying the running gap moments | high relative — it's the slowest hand-written; a Rust tick-ring fold would cut most of it (the #324 marshaling lesson: measure first) |
| `size_entropy` | 2 | custom `compute_latest`, 30/60m window bin-count re-aggregate | 27.9 / 45.7 | carry the windowed bin counts, fold new minute | moderate |
| `print_hhi` | 2 | `compute_latest_on_window`, 30/60m notional-HHI re-aggregate | 13.8 / 23.9 | carry the windowed Σnotional / Σnotional² | low-moderate |
| `microstructure_burst` | 4 | `compute_latest_on_window(1)` — own-minute only | 18.0 / 30.9 | already minute-local; the cost is the per-minute tick marshal, not a re-scan | low (no carry to add — it's already at-T) |
| `inter_arrival` / `tick_runlength` / `trade_size_dist` / `large_print_burst` | 3/3/3/3 | `compute_latest_on_window(1)` — own-minute only | ≤19 / ≤19 | already minute-local (at-T); not re-scanning | ~0 (already optimal-ish) |
| `momentum_run` | 12 | custom windowed-slice `compute_latest` (derives returns over buffer, then window) | 38.1 / 78.5 | assessed irreducible OLS skew/streak; Rust kernel = marginal (already noted in readiness) | low (deferred — measured marginal) |
| `draw_range` | 3 | own latest-only window agg | 5.0 / 11.6 | already latest-only | ~0 |

### Tier 2 — already optimal (no gap) — recorded so the audit is complete

- **ReductionGroups (23, 377 feat)** — `clean_momentum`, `distribution`, `liquidity`, `momentum`, `volume`,
  `trade_flow`, `volatility`, `quote_spread`, `efficiency`, `trend_quality`, … — all on the shared
  `WindowedSumState` running-sum fold. Optimal design IS in place; the live O(1) payoff is the **Lead-gated
  `FP_INCREMENTAL` flip** (20/23 ready). The 3 PARKED (`price_volume`, `market_beta`, `residual_analysis`) stay
  correctly on the batch fresh-sum path (corr-denom-straddle — no correctness loss, see INCREMENTAL_READINESS.md).
  The standalone profiler ms for these OVER-counts (they share ONE batched emit in flow), so their high rows are
  not their real in-flow share.
- **StatefulGroups (4, 87 feat)** — `candlestick`, `price_levels`, `price_returns`, `technical` — resident
  StatefulEngine (EMA/lag/extrema fold), O(1)/minute in-kernel. Optimal.
- **Class-A cached/static (≈24)** — `sector`, `calendar`, `asset_flags`, `round_levels`, `prior_day`,
  `multi_day_returns/_vwap`, `daily_beta`, `liquidity_rank`, `return_dispersion`, `overnight_*`,
  `calendar_events`, `edgar_filing_frequency`, … — compute once per session, cache (`SessionCache`), broadcast.
  ~0/min. Optimal. (Several were measured NET-NEGATIVE to consolidate further — don't.)
- **Gather (≈8)** — `breadth`, `market_context`, `market_turbulence`, `cross_sectional_rank`, `peer_relative`,
  `sector_beta`, `sector_return` — run ONCE in the reader phase, not a per-bet cost. N/A.
- **Session-cumulative latest-only** — `dumper_state`, `runner_state`, `gap_fill_state`, `intraday_seasonality`
  — one aggregate-at-T, no per-minute scan. Optimal (could be PROMOTED to a declared CumulativeState kind for
  uniformity, but the cost is already ~0).

## The biggest efficiency wins, ranked

1. **`swing` → carried leg-state** (this PR): 26.1→15.5ms p50 group-local; the only whole-session re-scan in
   production. **Done here** (gated, value-identical, fp-unchanged).
2. **`FP_INCREMENTAL` flip** (Lead-gated, already-built mechanism): turns the 20 ready ReductionGroups from
   per-minute rolling recompute → reading pre-folded sums. The single largest live-latency lever; gated on the
   PARITY soak (RustIncremental's #66).
3. **Delta-passing to stateful/window groups** (engine work — scoped below): the residual swing cost (and the
   Tier-1 window groups') is whole-ring polars MARSHALING, not fold arithmetic. Passing the per-minute DELTA
   frame (the capture path already has it as `new_frame`) instead of the full ring would take swing ~15.5→~2ms
   and similarly shrink the Tier-1 groups. One shared engine change, many beneficiaries.
4. **`subminute_gap_fano` tick-ring Rust kernel** (Tier-1, measure-first per the #324 lesson): the slowest
   hand-written group; a resident tick-ring fold could cut most of its 63ms — but marshaling overhead can erase
   the win on cheap group-bys (print_hhi/size_entropy LOST to Rust in #324), so MEASURE before building.

## The scoped swing follow-on (the full O(1), if the Lead wants the last 13ms)

The group-local carry (this PR) removes the FOLD re-computation (O(window) arithmetic → O(new-bars)). The
remaining ~15ms is the per-minute polars MARSHALING of the whole ring frame (`unique`, `filter(minute==max)`,
the slice scan, the output build) — inherent to the group receiving the full trailing ring each minute. The full
O(1) (~2ms, the measured steady no-op-fold cost) needs the **capture path to hand stateful/window groups the
per-minute DELTA** (the `new_frame` it already builds at `process_bars` before pushing to the ring), with a
`seed(buffer)` on the first minute / restart. That is a shared engine change (a new group hook
`step(delta_frame)` alongside `compute_latest(full_buffer)`), value-identical and fp-neutral, that benefits
swing AND every Tier-1 window group at once. Scoped, not built here — flagged for the Lead to sequence.

## THE STANDING GATE — so a new feature can't silently fail the bar

The audit found ONE silent failure (`swing`) that slipped past every existing gate because nothing ASSERTS a
group is on its optimal design. Proposal (spec — operationalize at the Lead's call):

1. **Declare-your-kind, enforced.** Add a required `state_kind` classmethod/attr to `FeatureGroup` — one of
   `{CACHED_STATIC, REDUCTION, STATEFUL_RESIDENT, LATEST_ONLY, WINDOW_REAGG, GATHER}`. A new group MUST declare
   it. A registry-load assertion (the same shape as the feature-count assertion in `groups/__init__.py`) fails
   if a group's declared kind is inconsistent with its base class (e.g. a `FeatureGroup` claiming
   `STATEFUL_RESIDENT` must carry state / override `compute_latest`).

2. **The "no whole-buffer re-scan" gate** (the one that would have caught swing). Extend
   `tests/test_fp_latency_budget.py` (or a sibling): for every group whose `compute_latest` is the DEFAULT
   (un-overridden) AND whose `reduce_buffer_minutes()` is `None` (unbounded), FAIL unless the group is explicitly
   on the allow-list of "genuinely whole-buffer-cheap" kinds. swing (un-overridden + unbounded + 9 feat) would
   trip this immediately. Mechanically: `assert group.compute_latest.__func__ is not FeatureGroup.compute_latest
   or group.reduce_buffer_minutes() is not None or group.name in _WHOLE_BUFFER_OK`.

3. **Wire `optimal_design` + `on_optimal` into the JSON.** Add the two fields per group to
   `feature_latency_expectations.json` (and have `latency_expectations.py --update` populate `on_optimal` from
   the gate in #2), so the UI surfaces "N groups off their optimal design" and a regression is visible, not
   buried. (Done conceptually by THIS doc's gap list; the JSON enrichment is the durable machine-readable form.)

The combination makes "fast and cheap by construction" *checkable*: a new feature declares its kind, the gate
refuses an unbounded whole-buffer re-scan, and the JSON shows the fleet's on-optimal status at a glance.
