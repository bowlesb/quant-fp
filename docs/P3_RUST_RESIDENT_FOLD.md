# P3 — Rust-resident fold for the sequential-hot B kinds (design)

> Status: DESIGN for gate-read (2026-06-20). Author: Latency. P3 of the unified state-execution abstraction
> (#278 spec, #280 Ben reconciliation). Target: the ~213ms per-bet ``other_emit`` floor — the per-group
> Python frame-build loop across the at-T groups, the dominant per-bet cost (reductions are only ~44ms;
> P2 made them EXACT but they were never the floor). P3 collapses the per-group Python fold/emit into a
> shared resident pass. Grounds in stateful.py (the working EMA/lag/extrema resident kinds + the
> ``seed(H);fold(m)==seed(H+m)`` invariant) and the ``quant_tick.swing_fold`` / ``swing_dc_fold`` Rust
> kernels (the oracle-pinned whole-buffer-fold == latest-row pattern).

## The floor we are removing

Per-bet bar→vector ≈ 461ms isolated; per-shard compute ≈ 305ms, of which ``other_emit`` ≈ 213ms is the
``for group in other_groups: group.compute_latest(BatchContext(...))`` loop — each group builds its OWN
polars frame, does its OWN tz/derive/group_by, emits. ~30+ groups each paying a fixed per-call frame-build
overhead. The reductions (additive-window) already share ONE Rust ``assemble_canonical`` pass + the
StatefulEngine already shares ONE coded-buffer fold for technical/candlestick/price_levels/price_returns.
The remaining bespoke ``FeatureGroup.compute_latest`` groups are the unshared residue.

## The principle (unchanged from #278 / STATE_ABSTRACTION.md)

Live and backfill differ ONLY in how the state is OBTAINED, never in how outputs are DERIVED. A group
declares a KIND + writes ``emit()`` once; the engine owns the live fold AND the backfill rolling-equivalent,
parity-gated by ``seed(H);fold(m)==seed(H+m)`` cell-for-cell. The Rust kernel is just a faster way to reach
the state — its WHOLE-BUFFER fold is the parity oracle, the live path takes the latest row (swing_dc_fold's
exact contract).

## ⭐ Fingerprint impact: NONE (the key enabling fact)

``BusSchema.from_registry`` fingerprints ``blake2b`` over the ordered ``group:feature:version`` lines —
purely the (group name, feature name, version) set. It does NOT depend on the base class or the execution
path. So porting a group from a bespoke ``FeatureGroup.compute_latest`` to a declared resident kind, KEEPING
its name/version/feature set identical, is **fingerprint-neutral by construction** — the same value-identical
contract as every shipped latency win. (Verified: the swing group already runs the swing_fold Rust kernel
and is in the live 728 set at its normal fingerprint.) → P3 ports do NOT require a coordinated fp deploy,
only the byte-eq ``test_fp_latest`` + the ``fold==reseed`` kind-parity gate.

## The new KIND: CumulativeState (the first P3 port)

The session-cumulative groups — ``runner_state`` (running cum-max high, cum-sum dollar, session-open),
``dumper_state`` (cum-min low, cum-sum dollar, session-open), ``gap_fill_state`` (session-open anchor) —
are the cleanest sequential-hot B kind: a per-(symbol, ET-session) running max / min / sum / first, RESET
at the session open. Today they are bespoke ``FeatureGroup``s; the prior latest-only PRs (#266/#267/#269)
already reduced each ``compute_latest`` to a per-session ``group_by`` aggregate (a single-pass max/min/sum/
first over the current session). P3 promotes that to a DECLARED ``CumulativeState`` kind in StatefulEngine:

```
@dataclass(frozen=True)
class CumulativeSpec:
    alias: str          # output state column
    source: str         # per-minute input column (high / low / close*volume / open)
    reduce: str         # "max" | "min" | "sum" | "first"
    reset: str = "et_session"   # the partition that resets the accumulator (per ET-session-date)

class CumulativeState:
    # one running value per (symbol, spec), reset when the ET-session-date rolls over.
    def seed(self, history): ...   # replay fold over the buffer's RTH bars
    def fold(self, minute):  ...   # update each accumulator (max/min/sum/first), reset on new session
    def emit(self):          ...   # read the accumulators -> the group's canonical columns
```

PARITY GATE: ``seed(H); fold(m) == seed(H+m)`` — the running max/min/sum/first at the LAST bar of a session
equals the session's max/min/sum/first by construction (the same identity PR #267 proved cell-for-cell with
its adversarial oracle: multi-session, sparse-absent-at-T, first-bar-of-session, run-then-pullback). So the
CumulativeState fold is parity-true by the same argument, now expressed as the universal kind invariant.

RUST-RESIDENT FORM: the fold is O(1)/symbol/minute (a few comparisons + an add), so the win is removing the
PER-GROUP polars frame-build, not the arithmetic. STAGE 1 (this design's first port): a numpy-resident
CumulativeState in StatefulEngine — runner/dumper/gap_fill share ONE coded-buffer pass (like the extrema/lag
kinds), emitting all three groups' columns from the one resident fold. STAGE 2 (follow-up): a
``quant_tick.cumulative_session_fold`` kernel if the numpy-resident pass is still per-bet-hot (mirrors
swing_fold). No new kernel is needed for Stage 1 — the win is the SHARED single-pass fold replacing 3
bespoke compute_latest frame-builds.

## The chunk / path-geometry KIND (flagged, NOT built this phase)

From the Ben reconciliation (#280): the swing / swing_dc / draw_range family is a path-segmentation kind
(backward-from-latest, bounded directional chunks, per-chunk fit). The Rust ``swing_fold`` (live: ``swing``)
and ``swing_dc_fold`` kernels ALREADY implement it; ``draw_range`` is a Python latest-only group that could
adopt it. Promoting these to a declared ``ChunkState`` kind is a later P3 step.

⭐ FP-IMPACT FLAG on the chunk-kind (per the Lead's note): ``swing_dc`` is DE-STAGED (un-registered, #270,
fp 802→728). Adopting the chunk-kind for the swing family is fingerprint-neutral for the ALREADY-REGISTERED
members (swing, draw_range — same name/version/features), BUT RE-INCLUDING swing_dc as a chunk-kind member
WOULD re-add its 74 features → fingerprint change (728→802) → a coordinated fp deploy = the Lead's sequenced
call, dovetailing with swing_dc's planned re-inclusion. NOTE only; swing_dc is NOT re-registered here.

## Phasing within P3

- P3.1 (the first port, parity-pinned): CumulativeState kind in StatefulEngine; port runner/dumper/gap_fill
  to declare it; ONE shared numpy-resident fold replaces the 3 bespoke compute_latest. fp-NEUTRAL; gated on
  byte-eq test_fp_latest[runner_state/dumper_state/gap_fill_state] + the fold==reseed kind-parity test +
  the #267 adversarial oracle re-run. Measure the other_emit delta (3 frame-builds → 1 shared pass).
- P3.2: a Rust ``cumulative_session_fold`` kernel if Stage-1 numpy is still hot.
- P3.3: ChunkState kind for the swing family (swing/draw_range; swing_dc re-inclusion = the Lead's fp deploy).
- P3.4: fold the remaining bespoke at-T groups (momentum_run's run-length, intraday_seasonality) into kinds.

## Measured grounding

other_emit ≈ 213ms/shard is the per-bet floor. Reductions 44ms (P2-exact), gather 7ms, fold/tick ~23ms.
The win comes from collapsing the ~30 bespoke per-group frame-builds into shared resident passes — P3 is the
only lever that touches the 213ms floor, hence the only path toward <100ms per-bet.
