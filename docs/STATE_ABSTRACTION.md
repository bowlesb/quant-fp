# In-process feature state — the general abstraction (design)

> Status: DESIGN (2026-06-14). Answers: "do all feature groups share ONE flexible abstraction for the
> in-process state they need for fast compute, with backfill engaging it consistently?" Today: clearly
> yes for the windowed-additive class (`ReductionGroup` + `WindowedSumState`), NOT generalized to the
> other state kinds. This is the generalization.

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

- **Done & proven:** additive-window kind (`WindowedSumState`, parity-exact, 0.49ms fold). V2 (in
  flight) makes its `emit()` read running sums directly — the reference implementation of this interface.
- **Not yet generalized:** the interface above is not extracted; cumulative / extrema / lag / recursive
  / tick-ring are still bespoke or on the slow path. Each needs its `fold`/`emit` + the
  `fold==reseed` parity test.
- **Sequencing:** land V2 (additive emit-from-state) first — it crystallizes the interface against a
  working kind. THEN extract `FeatureState` from it and migrate the next-highest-value kind
  (cumulative, then extrema — the price-levels 240m hot path). Each migration is gated on its
  `fold==reseed` parity test and the validation ledger on real data.
- **What to watch:** the temptation to let a group write bespoke incremental state (re-introduces the
  parity-drift risk the abstraction exists to remove). Rule: if a group needs state, it must express it
  as a declared KIND with the engine-owned backfill form — never hand-rolled. A genuinely novel state
  shape means adding a KIND to the engine (with its parity test), not a one-off in the group.

So: clearly designed for one kind, now generalized on paper; the build is the V2-first sequence above.
We had NOT thought the general case through before — this doc is that thinking made explicit.
