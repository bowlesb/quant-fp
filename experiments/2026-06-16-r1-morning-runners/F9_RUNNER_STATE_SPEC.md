# F9 `runner_state` — feature spec (batch-1b)

Family: PRICE / regime. Owner: modeller. The parity-true, point-in-time detector of the small-cap
morning-runner regime characterized in results.md (the runner FADES; this feature is the
conditioning variable a model uses for that reversal). NOT a daily broadcast and NOT a simple
reduction — it is a **running intraday state since the session open**, so it is well-defined and
correct at EVERY minute (not only after 10:00). Parity-true by construction: `compute_latest`
reruns the identical running computation on the latest minute.

## Inputs
- `daily` (symbol, date, close) → prior-day close (the $2–20 base + the early_move denominator).
- `minute_agg` (symbol, minute, high, close, volume) → the intraday running state.

## The point-in-time design (no look-ahead)
At minute *t* of the session, using ONLY bars from session-open through *t*:
- `run_high_so_far(t)`  = max high from open through t.
- `run_vol_so_far(t)`   = cumulative RTH volume from open through t.
- `minutes_since_open(t)` from `ctx.timestamp` (NEVER wall clock).

The Stage-1 study used the FIRST-30-MIN high; the live feature generalizes it to a running
since-open high so it is defined pre-10:00 too. At/after 10:00 ET the running high equals the
f30 high used in the study — so the study's calibration applies directly at the 10:00 mark.

## Features (all Float64 unless noted; nan_policy=warmup until prev_close + first bar exist)
| name | definition | range |
|---|---|---|
| `runner_early_move` | run_high_so_far / prev_close − 1 (running max gain off prior close) | (−1, 30) |
| `runner_gap_open` | session_open / prev_close − 1 (overnight gap leg) | (−1, 30) |
| `runner_pullback_from_high` | last_close / run_high_so_far − 1 (give-back so far; the fade signal — NEGATIVE as it bleeds off the high) | (−1, 0.5) |
| `runner_dollar_vol_so_far` | cumulative (close*volume) since open, log1p | ≥0 |
| `runner_in_band` (Int8) | 1 if prev_close ∈ [$2,$20] else 0 (the small-cap base gate) | {0,1} |
| `runner_is_active` (Int8) | 1 if `runner_in_band` AND `runner_early_move` ≥ 0.30 else 0 (the regime flag) | {0,1} |

Notes:
- `runner_pullback_from_high` is the live analogue of the study's close-vs-f30-high (−17.8% median
  by EOD) — the model reads the fade as it develops.
- Continuous features carry the signal; the two Int8 flags are the regime indicators (both 0 and 1
  occur in-sample → valid per the golden-set binary rule).
- NO dead-band returns / NO `.get` / required fields required — let it raise if prev_close missing
  (that's a data bug, per house rules).
- `runner_early_move`/`runner_gap_open` cap range at 30 (=+3000%) — CWD hit +2127%, so the band is real.

## Build discipline
- WORKTREE + PR into the batch-1b accumulation branch. NEVER edit `quantlib/features/groups/`
  in the live checkout (bumps the bus fingerprint → breaks deployed strategy containers).
- Add unit tests (running-max correctness, pre-10:00 definedness, band/active flags both-valued,
  prev_close join) and the `test_fp_latest` parity guard MUST pass.
- Update the feature-count assertion + a new feature-set version is the Lead's coordinated deploy.

## Why ship (vs the strategy being only promising)
Per the core mandate a feature has a LOWER bar than a strategy: real + parity-true + non-redundant
+ not-noise. F9 is all four — it encodes a regime (small-cap runner) NO existing group captures,
with a large, consistent, characterized forward sign. The model exploits its combinatorial value
even while the standalone short awaits its execution-reality certification (Stage 2).
