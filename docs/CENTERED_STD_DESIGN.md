# Centered power-sum std — the reduction-stability completion (design, PROVEN)

> Status: DESIGN, numerically PROVEN (2026-06-20). The shared-engine fix that un-gates the last
> incremental_safe=False std groups (volume, price_volume). Sibling: the corr-denom/SSR centering for
> market_beta + residual_analysis (PR B). Extends the existing OLS `rebase_time_axis` centering precedent
> from x-columns to value columns. NOT a bespoke patch — the coherent reduction-stability completion.

## The proven problem + fix (measured)

`volume`'s std is `sqrt((Σv² − (Σv)²/n)/(n−1))` on raw share volume ~5e6. `Σv²` and `(Σv)²/n` are both
~n·(5e6)² ≈ 1e15; their difference (the tiny variance) loses precision → the batch and incremental paths
disagree at ~3e-6 relative, which the strengthened breach test (#297) now catches (volume_zscore ~3e-5
apart, rel ~15x the parity ratio). Neumaier (#283) fixed the running-sum DRIFT but NOT this FORMULA
cancellation — verified: volume still breaches at intermediate variance.

PROVEN (microbench, n=60, base=5e6, vol_noise=1e-5):
| std form | rel_err vs the true (two-pass) variance |
|---|---|
| raw power-sum `Σv²−(Σv)²/n` | **3.1e-6** (the breach) |
| centered `Σ(v−c)²−(Σ(v−c))²/n`, c≈5e6 | **1.1e-16** (machine precision — breach GONE) |

Centering on a value `c` within ~order-of-magnitude of v makes the squared terms small → the cancellation
vanishes. The anchor does NOT need to be exact (round(base) suffices).

## The anchor MUST be per-symbol-scale (measured — a global constant fails)

| base | raw rel_err | global anchor 1e6 |
|---|---|---|
| 5e5 | 2.7e-6 | 4.0e-6 |
| 5e6 | 7.8e-6 | **4.4e-7** (helps) |
| 5e7 | 3.6e-6 | 3.6e-6 |
| 1e3 | 8.0e-8 | **2.0 (CATASTROPHIC)** — anchor 1e6 >> v=1e3 |

So a single global anchor is worse than useless (blows up small-volume symbols). The anchor MUST track each
symbol's volume magnitude — a PER-SYMBOL constant.

## The design (extend the rebase precedent to value columns)

The engine already centers OLS x-columns via `WindowedSumState.rebase_time_axis` (shifts x→x−Δ in the running
sums + buffered minutes in lockstep, origin-invariant, applied identically in both paths) precisely to keep
`b·Σxx−(Σx)²` conditioned. The value-std fix is the SAME mechanism on the value column:

1. A reduction group opts a column into centered std: `reduced()` declares `std_centered=True` + an `anchor`
   that is a PER-SYMBOL reproducible constant (e.g. log10-rounded typical volume, or a daily prior-volume
   reference — see anchor sourcing below).
2. The marshal stores, for that column, `Σ(v−a)` and `Σ(v−a)²` (the centered power sums) INSTEAD OF/ALONGSIDE
   `Σv²`. The RAW `Σv` is still summed (volume_ratio = v_T/mean needs the raw mean — so the centered std is a
   SECOND accumulated pair, not a replacement of the shared column).
3. `_canonical` / `_canonical_numpy` / the Rust `assemble_canonical` compute std from the centered sums:
   `var = (Σ(v−a)² − (Σ(v−a))²/n)/(n−1)` — value-identical to the raw var (shift-invariant) but conditioned.
4. The incremental engine sources the anchor the SAME way the OLS rebase sources its origin — from the
   running state, rebased when the anchor drifts past a scale threshold (so a symbol whose volume regime
   shifts re-anchors, exactly like rebase_time_axis re-pins the time origin).

## Anchor sourcing (the path-consistency crux — the one real design choice)

The anchor must be IDENTICAL in batch (`compute_reduction_batch`, full-window frame) and incremental
(folded running state). Options, ranked:
- **(A) Daily prior-volume reference** (a `daily` snapshot input, per-symbol prior-day mean volume,
  log10-rounded). Reproducible (the daily snapshot is fixed all session, read identically by both paths via
  the same broadcast mechanism daily_beta uses). CLEANEST + most robust; adds a daily input to volume/
  price_volume. RECOMMENDED.
- **(B) Seed-snapshot per-symbol anchor** rebased on drift (the pure rebase_time_axis analogue — anchor
  snapshotted at seed from the buffer, re-pinned when v drifts past ~2x). No new input, but the anchor must
  be threaded through the StatefulRegressor/rebase machinery for both paths; more engine surface.
- (C) A global per-symbol log10-bucket from the bar's own volume — REJECTED: differs between batch (full
  window) and incremental (fold) unless snapshotted, collapsing to (B).

## Why this is the proper fix, not a hack

It is the SAME centering the engine ALREADY does for OLS x (rebase_time_axis), generalized to value columns
— one coherent mechanism for the whole reduction-stability class (std-on-large-magnitude + the OLS/corr-denom
cancellations PR B addresses). It un-gates volume + price_volume (PR A) and, with the corr-denom centering,
market_beta + residual_analysis (PR B) → 23/23 reductions incremental-ready.

## Validation (when built)

- byte-equality on the VALUE path: centered-std `compute()` == the prior raw-std `compute()` to the declared
  tolerance (shift-invariant → identical value; only float conditioning changes), so trust is PRESERVED.
- the strengthened breach test (#297) FLIPS from `assert breached` to `assert not breached` — the proof the
  gate can drop. Then `incremental_safe=True` for volume/price_volume.
- fp UNCHANGED (a numerical-conditioning change, not a feature contract).

## Why a design doc, not the built PR (honest scoping)

PR A is a real shared-engine change: the anchor must thread through BOTH the batch marshal AND the
incremental running state path-consistently (the existing `prepare` is NOT called by the incremental engine,
and there is no per-symbol static slot today — so it needs either the daily-anchor input wired into both
paths' marshal, or a rebase-style state anchor). Building it correctly + validating end-to-end (byte-eq +
the breach flip + the full incremental suite) is a deliberate multi-step change, NOT a quick value-identical
PR — exactly the bug class (batch≠incremental) it must avoid introducing. This doc specifies it so the build
is correct-by-construction; recommend anchor option (A) (daily reference) as the cleanest path-consistent
mechanism.
