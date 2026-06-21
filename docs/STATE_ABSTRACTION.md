# In-process feature state — the general abstraction (design)

> **⚠️ AUTHORITATIVE SOURCE NOTE (added 2026-06-21 per #381).** This doc is RETAINED for the state-KIND
> taxonomy (additive-window / cumulative / extrema / lag / EMA / tick-ring) and the parity-by-construction
> principle — those are unchanged and correct. **But its per-group COST labels in the ADOPTION MAP below are
> SUPERSEDED by `docs/LATENCY_EXHAUSTIVE_AUDIT.md` (#381), the current authoritative per-group latency /
> abstraction source.** #381 traced all 63 groups' LIVE per-minute path in `origin/main` source and refuted
> three classifications here: (1) the "12 BATCH-fullbuffer / 90 features rebuild from full history every
> minute" claim is a mechanical label on each group's BACKFILL `compute()` (`reduce_buffer_minutes()==None`),
> NOT the live form — all 12 live `compute_latest` paths are bounded / session-cached / single-minute; (2) the
> 5 "A SessionCache-cached" static groups are pure-ts filters, NOT `SessionCache`; (3) `return_dispersion` is
> a hybrid (cached snapshot + 60m gather). Those rows are corrected inline below. **Read #381 for the
> source-traced per-group truth; read this doc for the kind taxonomy.**
>
> Status: DESIGN + MEASURED ADOPTION (updated 2026-06-21). Answers: "do all feature groups share ONE flexible
> abstraction for the in-process state they need for fast compute, with backfill engaging it consistently?"
> The KINDS (additive-window, EMA, lag/last-k, extrema) are BUILT and parity-proven; the held-state
> `RunningState` contract (`up_to_date`/`rebuild_from_history`) is on `base.FeatureGroup`. The two remaining
> gaps are concrete, not conceptual: (1) the ~10x incremental lever is DORMANT in the live equity fc
> (`FP_INCREMENTAL` off) and has no equity PARITY soak backing the flip; (2) the plain groups labeled
> "BATCH-fullbuffer" below rebuild from the full trailing buffer ONLY in their BACKFILL form — their LIVE
> `compute_latest` is already bounded / cached / single-minute (per #381 §6.1). The measured adoption map +
> the activation gap + the migration path are the sections at the bottom.

## The principle that must hold (non-negotiable)

Parity is by construction iff **live and backfill differ ONLY in how the state is OBTAINED, never in
how outputs are DERIVED from it.** `ReductionGroup` already embodies this: one declaration → the engine
emits a backfill form (rolling over full history) and a live form (fold-and-read state), both running
the identical `assemble()`. The general abstraction must preserve exactly this: shared derive logic,
two ways to reach the state.

## The interface every fast-path group implements

```
class FeatureState(Protocol):
    def state_spec(self) -> StateSpec: ...          # declare what to allocate (kind, columns, windows)
    def seed(self, history: Frame) -> None: ...      # initialize from a buffer of prior minutes
    def fold(self, minute: MinuteMatrix) -> None: ... # advance one minute (add new, expire old)
    def emit(self) -> CanonicalColumns: ...          # derive the group's canonical columns from state
```

- **Live path:** `seed(buffer)` once at session start (also crash recovery), then `fold(minute)` each
  minute, then `emit()` at the mark. O(symbols × state) per minute — the 0.49ms regime.
- **Backfill path:** the SAME `emit()` over state reached by replaying `seed`+`fold` across the
  rolling history (or the engine's vectorized rolling equivalent that is *proven equal* to fold). The
  group author writes `emit()` ONCE; the engine guarantees the backfill state matches the live state.
- **Parity test per state kind:** `seed(H);` then `fold(m)` == `seed(H+m)` (folding one minute equals
  re-seeding with it appended), cell-for-cell. This is the single invariant that makes the whole class
  parity-true — it's exactly what `test_fp_incremental` proves for `WindowedSumState`, generalized.

## The state KINDS (the flexible part)

"Whatever state a group needs" is not unbounded — it collapses to a small set of KINDS, each with one
correct `fold`/`emit` and one parity invariant. A group declares which kinds it needs; it never writes
bespoke incremental bookkeeping:

| kind | state | fold | features today |
|---|---|---|---|
| **Additive window** | running Σ per (sym,win,col): sum, sumsq, paired OLS sums | add new minute, expire the one that left | volume, volatility, trade_flow, momentum, OLS groups (DONE: `WindowedSumState`) |
| **Cumulative** | running accumulator, no expiry | add new minute | OBV, session-cumulative VWAP/volume |
| **Rolling extrema** | monotonic deque per (sym,win) | push new, pop dominated, drop expired | price levels (`high_240m`), Donchian, `dist_from_high` |
| **Lag / last-k** | small ring buffer of the last k minutes | push, overwrite oldest | `shift(k)` returns, run-length, candlestick lookback |
| **Recursive (EMA)** | one value per (sym, halflife) | `v = α·new + (1-α)·v` | EMAs, RSI/MACD-style technicals |
| **Tick ring (Layer C)** | bounded per-symbol tick buffer | append ticks, evict by time | microstructure burst, tick run-length |

Each kind is implemented and parity-tested ONCE in the engine. Adding a feature picks kinds; it does
NOT touch the fast/backfill split. That is the "all groups use the abstraction the same way" goal.

## How backfill engages consistently (the core logic lives in the engine)

The shared core (not in any group) owns:
1. **The dual-form generator** — from a group's `state_spec` + `emit`, produce the live driver
   (`seed`/`fold`/`emit` loop) AND the backfill driver (vectorized rolling that is proven `==` to the
   fold-replay for that kind). Reductions already do this; each new kind adds its rolling-equivalent.
2. **The seed/expiry/re-seed policy** — buffer sizing (≥ max window + lag, asserted), per-session
   re-seed to bound float drift, crash-recovery from the last buffer.
3. **The assembly** — `emit()` outputs land in the same canonical columns, so `assemble_from_long`
   runs unchanged for live and backfill.

A group thus only ever declares state + writes `emit()`. The backfill engagement is established and
identical across groups because it's engine-owned, not re-implemented per group.

## Honest status & roadmap

- **Done & proven:** additive-window kind (`WindowedSumState`, parity-exact, 0.49ms fold; V2 emits from
  the running sums directly — the reference implementation of this interface); recursive-EMA + lag/last-k
  kinds (`EMAState` / `LastKState` in `stateful.py`, used by technical/candlestick + price_returns); and the
  ROLLING-EXTREMA kind (`ExtremaState`, a per-(symbol,window) monotonic deque, used by price_levels) — each
  with its `fold==reseed` parity test in tests/test_fp_stateful.py + tests/test_fp_rest_kinds.py.
- **Not yet generalized:** cumulative (OBV beyond the OLS-regressor case) and the tick-ring (Layer C) kinds
  are still bespoke or on the slow path. Each needs its `fold`/`emit` + the `fold==reseed` parity test.
- **Sequencing:** land V2 (additive emit-from-state) first — it crystallizes the interface against a
  working kind. THEN extract `FeatureState` from it and migrate the next-highest-value kind
  (cumulative, then extrema — the price-levels 240m hot path). Each migration is gated on its
  `fold==reseed` parity test and the validation ledger on real data.
- **What to watch:** the temptation to let a group write bespoke incremental state (re-introduces the
  parity-drift risk the abstraction exists to remove). Rule: if a group needs state, it must express it
  as a declared KIND with the engine-owned backfill form — never hand-rolled. A genuinely novel state
  shape means adding a KIND to the engine (with its parity test), not a one-off in the group.

---

## ADOPTION MAP — every group classified (measured 2026-06-21, live registry: 63 groups / 728 features)

Ben's target: every feature is **(A) intraday-invariant → compute-once + cache** or **(B) prior-state +
O(1)-per-minute fold**. Anything else is **BATCH** — the live path rebuilds it from the full trailing buffer
EVERY minute (the slow path the abstraction exists to kill). Counts below are the live `feature-computer`
registry, classified mechanically (`isinstance` + `reduce_buffer_minutes()` + `compute_latest`/`SessionCache`
overrides), NOT from this doc's prose. The 728/63 mismatch with older "190 feature" notes is the FINGERPRINT
feature count (728 named outputs across 63 groups); both are correct, they count different things.

### Headline: the held-state contract IS landed; the remaining GAP is ACTIVATION (the dormant `FP_INCREMENTAL` flip + the Rust kernel), NOT a live full-buffer rebuild. (Updated per #381 — the "BATCH plain groups rebuild from full history every minute" framing was BACKFILL-only; live `compute_latest` is already bounded/cached.)

| Category | groups | features | what they are |
|---|---|---|---|
| **A — intraday-invariant (cached or cacheable)** | 13 | 99 | static/calendar + daily-snapshot groups. **NOTE (#381 §6.2):** of these, 5 (`sector`, `calendar`, `asset_flags`, `round_levels`, `calendar_events`, 31f) are **pure-ts/reference FILTERS, NOT `SessionCache`** — they default to full `compute()` + filter-to-T (≤6.5ms, ~0 marginal cost). The other 8 (68f) are true `SessionCache` daily-snapshot groups. A LABEL-accuracy correction, not a latency lever (caching a pure-ts filter saves nothing). |
| **B — O(1) fold (incremental state)** | 19 | 288 | 15 `ReductionGroup` incremental_safe (201f) + 4 `StatefulGroup` EMA/Lag/Extrema (87f) |
| **B-ready, FLAG-OFF** | 1 | 9 | `swing` — full RunningState held-state built, gated on `FP_SWING_STATEFUL` (currently BATCH live) |
| **BATCH-bounded (cheap, NOT yet folding)** | 10 | 66 | plain groups with a bounded `compute_latest` window (30–62m) — recompute a SHORT window/minute, not full history |
| **BATCH-fullbuffer-BACKFILL-ONLY (live `compute_latest` already fast)** | 12 | 90 | **RE-LABELED per #381 §6.1.** `reduce_buffer_minutes()==None` is a mechanical label on the BACKFILL `compute()` ONLY. The LIVE `compute_latest` of all 12 is bounded / cached / single-minute (`session_cumulative_agg` memoized ×3, `compute_latest_on_window(ctx,1)` ×5 tick, 75m slice for `momentum_run`, `SessionCache` memo for `edgar`, latest-row gather for `market_context`, session single-pass for `intraday_seasonality`). **No live full-buffer rebuild exists.** See `docs/LATENCY_EXHAUSTIVE_AUDIT.md` §6.1 for the source-traced per-group verdict. |
| **NO-GO reductions (Rust-only, stay BATCH)** | 8 | 176 | `ReductionGroup incremental_safe=False` — settled value-identical float-cancellation limit; see below |

(Totals: 13+19+1+10+12+8 = 63 groups; 99+288+9+66+90+176 = 728 features — verified against the live registry.)

**The quantified gap Ben asked for — "how many features still rebuild from full history every minute?"**
**CORRECTED per #381 §6.1: the answer is ZERO — no group rebuilds from the FULL trailing buffer every LIVE
minute.** The 12 groups previously labeled "BATCH-fullbuffer" carry `reduce_buffer_minutes()==None`, but that
flag governs the BACKFILL `compute()`, not the live form — every one of them overrides `compute_latest` to a
bounded / cached / single-minute pass (traced group-by-group in #381 §6.1 and listed by mechanism below). They
are correctly a BACKFILL-only full-buffer cost, not a live one. The 10 BATCH-bounded groups (66f) already slice
to a 30–62m window (parity-true by `compute_latest_on_window` semantics) but are NOT yet expressed as a declared
B-kind. The genuine "rebuilds every minute despite being B-ready" bucket is the 201f of armed reductions that run
BATCH-in-live only because `FP_INCREMENTAL` is off (the activation gap below) — the single largest live lever.

The 12 groups (still a worthwhile B-KIND migration backlog to make their bounded `compute_latest` a DECLARED
fold rather than a bespoke pass; their LIVE form is already fast, by mechanism):
- **session-cumulative running min/max/first** — `dumper_state` (6f, session min-low), `runner_state` (6f,
  session max-high), `gap_fill_state` (2f). **ALREADY DONE per #381 §2e:** their live `compute_latest` uses
  `session_cumulative_agg()` (memoized session min/max/first via `CumulativeState`, #284) — value-identical, one
  cached pass per snapshot. NOT a pending migration; ✅ no action.
- **universe/market gather** → run once in the reader gather phase: `market_context` (36f). **CORRECTED per #381
  §6.1/§2f:** its live `compute_latest` gathers only the LATEST row's index returns — it does NOT self-join /
  lag the full buffer per minute (the earlier "heaviest fullbuffer group" claim was wrong about the live path).
  Paid once in the gather phase, broadcast — not a per-bet cost.
- **tape/microstructure rolling** → B tick-ring (the one genuinely-new KIND): `inter_arrival` (3f),
  `large_print_burst` (3f), `microstructure_burst` (4f), `tick_runlength` (3f), `trade_size_dist` (3f).
- **rolling OLS / run-length** → B additive (OLS power-sums) once expressed declaratively: `momentum_run` (12f).
- **time-of-day rolling** → bounded window or A: `intraday_seasonality` (2f).
- **filing-window count** → A session-snapshot (the available_at gate is per-session-fixed): `edgar_filing_frequency` (10f).

(The 10 BATCH-bounded groups, for reference — already short-window, convert their bespoke `compute_latest` to a
declared kind: `breadth` 30f/60m, `cross_sectional_rank` 6f/60m, `draw_range` 3f/61m, `market_turbulence`
5f/60m, `peer_relative` 3f/30m, `print_hhi` 2f/61m, `sector_beta` 6f/62m, `sector_return` 8f/60m, `size_entropy`
2f/61m, `subminute_gap_fano` 1f/61m.)

### The NO-GO 8 (SETTLED — do not re-litigate)
`clean_momentum / distribution / market_beta / price_volume / range_expansion / residual_analysis /
return_dynamics / trend_quality` (176f). These ARE on the reduction contract but `incremental_safe=False`
because the incremental `Σx²−(Σx)²/n` form diverges from the batch rolling form by ~2e-8..3e-6 on the float
cancellation (independently re-measured 06-21; the centered-anchor #332 mechanism fixes `volume` but does NOT
generalize to corr/OLS-denominator straddles — see SYSTEM_LOG 06-21 "NO-GO reduction-group anchor-extension =
0 flips" and docs/ACCELERATION_ROADMAP.md). Their path to O(1) is the **centered-denom Rust OLS/corr kernel**
(extend `reduction_anchor` + `assemble_canonical`), NOT a Python incremental fold. They stay correctly on the
batch path under FP_INCREMENTAL; this is a numerical limit, not an un-built feature.

---

## ACTIVATION GAP — the ~10x lever is DORMANT in live equity, and not even recording parity evidence

The B-incremental machinery for the 15 armed reduction groups is BUILT, ON MAIN, and unit-parity-green
(tests/test_fp_incremental*.py) — but **it is OFF in the live `feature-computer`.** Measured 2026-06-21:

```
live feature-computer env:  FP_BUS=1  FP_WARM_START=1          # NO FP_INCREMENTAL*, NO FP_*PARITY
crypto-capture env:         FP_INCREMENTAL=1 FP_INCREMENTAL_PARITY=1 FP_INCREMENTAL_SLICE=1
```

Per `capture.py:_incremental_switches`, all three FP_INCREMENTAL switches DEFAULT OFF → the live equity path is
**byte-identical to the pure batch path**; the 15 armed groups run the slow rolling recompute every minute. The
~10x lever is 100% dormant on equity. Worse for de-risking: because `FP_INCREMENTAL_PARITY` is also off, equity
is **not even recording the live A/B divergence** (`feature_incremental_parity_breach_total` /
`feature_incremental_parity_tol_ratio` in metrics.py) that is the stated evidence gate. Only crypto runs
PARITY=1 — and on the SPARSE crypto tape, which prior soaks already flagged as +Inf null-mismatch noise, NOT
clean parity evidence.

### What the Monday flip does, and how to de-risk it (the gated click)
1. **Flip `FP_INCREMENTAL=1 FP_INCREMENTAL_PARITY=1` on equity fc** (NOT PARITY=0 yet). With PARITY=1 the batch
   form stays the WRITTEN TRUTH; the incremental form is computed alongside and compared each minute. Zero risk
   to emitted values — it only starts populating the breach counter.
2. **Soak one full RTH session** and read `feature_incremental_parity_breach_total` per `reduce_input` bucket.
   A breach = divergence > 10× tolerance (`_PARITY_BREACH_RATIO`). The 15 armed groups should show 0 material
   breaches (the +Inf null-mismatch class seen on crypto is sparsity, not a real divergence — split it out).
3. **Promote: PARITY=0** once the equity soak is clean → the incremental form becomes the emitted truth and the
   batch recompute is dropped → the live ~10x lands. `FP_INCREMENTAL_SLICE=1` is now parity-safe for sparse
   symbols (positional tail) and rides along.

This staged flip is the missing prerequisite the READINESS "FP_INCREMENTAL (15 armed)" row omits: **there is no
equity PARITY soak backing the Monday flip today** — flipping straight to FP_INCREMENTAL=1 without the PARITY=1
session is exactly the risk this design warns against. Same pattern for `swing` (`FP_SWING_STATEFUL=1` → morning
soak → verify value-identical). The flip itself is the Lead's gated click; the de-risk is collecting the soak.

---

## THE PATH TO "every feature is A or B"

1. **FLIP the dormant lever first (no new code).** Equity FP_INCREMENTAL PARITY-soak → promote. This alone moves
   the 15 armed groups (201f) from de-facto-BATCH-in-live to actually-B-in-live. Highest leverage, already built.
2. ~~Migrate the 3 session-cumulative groups to B.~~ **DONE per #381 §2e** — `dumper_state`/`runner_state`/
   `gap_fill_state` already use `session_cumulative_agg()` (memoized session min/max/first, `CumulativeState`
   #284), value-identical, one cached pass per snapshot. No live full-buffer recompute remains. Optional follow-up:
   express them as the declared `ExtremaState`/cumulative KIND so they ride the `fold==reseed` parity test, but
   this is a hygiene re-expression, not a latency fix.
3. ~~Cache the A-eligible BATCH groups.~~ **`edgar_filing_frequency` already self-memoizes per #381 §2g** (its
   `available_at` point-in-time join keys on its own `_cache`, compute-once-per-session). Any remaining
   per-session-fixed gather can still route through `SessionCache` like daily_beta/prior_day, but edgar is done.
4. **Express the bounded-window BATCH groups as declared B-kinds.** The 9 BATCH-bounded groups already prove their
   window is short; converting their bespoke `compute_latest` to the additive/extrema kind makes them fold instead
   of re-slice, and puts them under the single parity invariant rather than the generic `compute_latest` guard.
5. **Tape/microstructure → tick-ring kind.** The Layer-C bounded tick buffer (`inter_arrival`,
   `large_print_burst`, `microstructure_burst`, `tick_runlength`, `trade_size_dist`) is the one genuinely-new
   KIND still bespoke — build it once in the engine with its `fold==reseed` test, migrate all five.
6. **Land the centered-denom Rust corr/OLS kernel** for the NO-GO 8 (the only path that makes THEM fast without
   breaking parity) — Rust, not Python fold. Tracked in docs/ACCELERATION_ROADMAP.md; Lead-sequenced.

After 1–6, every group is A (cached) or B (folds) or rides a Rust kernel. **Per #381, no group rebuilds from the
full trailing buffer in the LIVE path TODAY** — steps 2–5 are KIND-expression hygiene (turning already-fast
bespoke `compute_latest` passes into declared folds under the single parity invariant), and the only first-order
LIVE wins are step 1 (the `FP_INCREMENTAL` flip) and step 6 (the Rust kernel). **Rule (unchanged): a group that needs state declares a KIND with the engine-owned backfill form —
never hand-rolled incremental bookkeeping.** A novel state shape adds a KIND to the engine (with its parity
test), not a one-off in the group. `swing` is the cautionary reference: it hand-rolled held-state, so it carries
its OWN `up_to_date`/`rebuild_from_history` and a bespoke flag — correct, but the kind it needs (leg-state)
should be promoted into the engine so the next such feature inherits it.

So: the abstraction's KINDS are largely built and parity-proven; the work is (a) FLIP the dormant `FP_INCREMENTAL`
lever with a PARITY soak (the largest live win), (b) land the centered-denom Rust kernel for the NO-GO 8, and
(c) optionally re-express the already-fast bounded/cached plain groups as declared KINDS (hygiene, not a latency
fix — their live form is not a full-buffer rebuild). Per #381 the un-doneness is ACTIVATION of built levers, not
an undiscovered per-group inefficiency. This is the engineering backlog — not more design.
