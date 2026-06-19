# Parity-promotion gate for invented features

> A definition-time checklist the FeatureInventor applies to each KEEP candidate so a feature that gets
> promoted to production is **parity-true BY CONSTRUCTION** (live `compute_latest` == backfill `compute`)
> rather than parity-true by luck. Authored by the Parity workstream (`~/.quant-ops/parity-ledger.md`).
>
> **One line:** invention should PREFER parity-GREEN forms; every KEEP feature gets a parity tag
> (GREEN / YELLOW / RED) in `backlog.md` — alongside its RT tag from `RT_COST_SCREEN.md` — before it goes
> to a promotion PR. The two screens are symmetric: RT asks *"can we afford to compute it live?"*; parity
> asks *"will the live value EQUAL the backfill value?"*. A promoted feature needs BOTH green.

## Why this exists

A promoted feature is computed two ways: the **backfill** path (`compute()`, rolling over every minute, the
modeling/source-of-truth form) and the **live** path (`compute_latest()`, aggregate-at-T, one row per
symbol, the production per-minute form). The nightly trust sweep (`validation_sweep.py` →
`trust_lifecycle.py`) grades every feature live-vs-backfill and QUARANTINES any that diverge. A feature that
is not parity-true by construction either (a) fails trust and never reaches the model, or worse (b) passes
on well-conditioned names and silently injects a **finite-vs-NULL defect** on the degenerate corner — the
exact `#122` / `#131` / `#139` class this project has paid for three times.

The good news, grounded in the real architecture: the platform already has a form that is **parity-true by
construction**. `quantlib/features/declarative.py` lets a group declare its reduction ONCE
(`reduced()` / `points()` / `assemble()`); the engine GENERATES both `compute()` (backfill, polars
`rolling_*_by`) and `compute_latest()` (live, Rust reduction kernels) from that one declaration. Both
materialise the SAME canonical aggregate columns (`__mean_<col>_<w>`, `__std_<col>_<w>`, `__sum_<col>_<w>`,
`__pt_<name>`) and then evaluate the SAME `assemble()` expressions. So they **cannot diverge by more than
the kernel-vs-rolling float noise** the parity test already tolerates — UNLESS a guard threshold lets the
two float paths land on opposite sides of a branch. This gate's job is to keep invention inside that form
and to bake in the guard pattern that closes the one remaining crack.

## The grounding: the shared emit path (read these in `quantlib/features/`)

| layer | file | what it is | parity property |
|---|---|---|---|
| **declaration** | `declarative.py` `ReductionGroup` | `reduced()`/`points()`/`regressions()`/`assemble()` → engine generates `compute()` + `compute_latest()` | **both forms share the kernel** — parity by construction (the GREEN target) |
| backfill emit | `declarative.py::compute` | `rolling_mean_by` / `rolling_std_by` / `rolling_sum_by` over every minute | source of truth |
| live emit | `declarative.py::compute_latest` | `rust_reductions` / `rust_windowed_sums` at T, one row/symbol | the production path |
| shared plan | `declarative.py::build_plan` | the union value-column plan SHARED by batch + incremental engine — both sum EXACTLY the same columns | guarantees identical aggregate inputs |
| incremental | `incremental.py` + `ReductionGroup.incremental_safe` | optional running-sum service of the same canonical columns (`FP_INCREMENTAL`) | safe by default; `incremental_safe=False` for large-magnitude variance/corr cancellation (see below) |
| custom | `base.py::FeatureGroup` (hand-written `compute`/`compute_latest`) | the "genuinely weird" fast lane — two parallel implementations | NOT parity by construction; must be separately verified (YELLOW/RED) |

The decisive fact (from `~/.quant-ops/parity-ledger.md`, the 06-19 same-path test `samepath.py`): when both
forms are fed the **same** input frame, a declarative feature agrees to **full float precision**
(`0.5284911839935234 == 0.5284911839935234`). Every confirmed live-vs-backfill divergence this project has
chased was either (a) an INPUT-coverage gap (capture-start / `FP_TICK_SYMBOLS` width — NOT a feature-code
bug), or (b) a **guard sign-at-threshold** crack on a degenerate window. (a) is out of an inventor's hands.
(b) is exactly what the inventor controls at definition time — hence the mandatory guards below.

## The rubric — tag each candidate GREEN / YELLOW / RED from its DEFINITION

Ask, in order. The FIRST trait that fires sets a floor on the tag (you cannot be greener than the worst
trait your definition needs). This is the cheap definition-time filter; a real prototype is confirmed by the
generic parity test (below).

### PARITY-GREEN — parity-true by construction (target every invented feature here)
The feature is expressible as a `ReductionGroup`: it is a fixed `assemble()` function of bounded-window
**reductions** (`mean_` / `std_` / `sum_`) of per-bar (or per-trade-bucket) quantities, plus at-T points
(`pt_`). Concretely, any of:

- sum / mean / count / std / coefficient-of-variation / z-score over a bounded trailing window (a function
  of `__sum`, `__mean`, `__std` — i.e. of Σx and Σx²)
- a ratio of two such reductions (OFI = `sum_(signed_vol)/sum_(vol)`; block-share = `mean(p99)/mean(mean)`;
  body-efficiency = `sum_(body)/sum_(range)`; size-CV = `std_(size)/mean_(size)`)
- a recent-sub-window-vs-window ratio of two bounded reductions (accel/shift features)
- min / max / last over the window via `pt_`

These live in the shared `compute`/`compute_latest` emit path and **cannot diverge beyond float noise**.
Most of the current invent batch is GREEN-eligible (the entire strong-IC head — `f_size_tail_ratio`,
`f_avg_print_size`, `f_size_cv`, `f_block_share`, `f_body_efficiency`, `f_ofi_window`, `f_interarr_burst`,
the accel family — is a ratio/std/CV of bounded sums). **Prefer expressing a new idea in this form whenever
possible** — the same advice the RT screen gives, because the GREEN tier coincides: the cheap form IS the
parity-true form.

> GREEN is NOT a free pass for the degenerate corner. A GREEN feature that does a division, a std, or a
> z-score MUST still ship the mandatory guard below. GREEN means "the shared kernel guarantees the
> well-conditioned cell agrees"; the guard guarantees the DEGENERATE cell agrees too.

### PARITY-YELLOW — needs a custom incremental form that must be separately verified
Fires if the definition needs ANY of:

- a **windowed OLS / regression** (slope / corr / r² / residual stats per symbol per window) — declarable
  via `ReductionGroup.regressions()`, but its `_ols_stat_exprs` algebra is a difference of large near-equal
  sums (`denom_x = b·Σx² − (Σx)²`) whose SIGN at the defined-guard is machine-eps sensitive. This is the
  `#122`/`#131`/`#139` family. It is supported and shares the kernel, but it carries a **mandatory OLS guard**
  (below) and a per-group incremental-safety call (`incremental_safe`). Tag YELLOW, cite the guard.
- a **lag-k autocorrelation / serial-correlation** in the window (`corr(x, x.shift(1))`) — needs the ordered
  series, not just sums, so it is not a pure `ReductionGroup` reduction; it needs a custom `FeatureGroup`
  with two hand-written paths (`f_ret_autocorr1`, `f_vol_autocorr1`, `f_flow_autocorr1`).
- a **within-window quantile / median / rank / sort** (an order statistic) — same: ordered, custom path.
- a **run-length / consecutive-same-sign** form (`f_ret_runfrac`) — path-dependent state; a sign-change
  *count* is GREEN, the run-length form is a custom path.
- a **single cross-sectional gather at T** (rank / zscore / dispersion across the universe, the
  `market_context` / `liquidity_rank` shape) — bounded, but the live and backfill gather must cover the
  SAME universe set; a targeted backfill over a symbol subset computes a different-cardinality cross-section
  (the `return_dispersion` scope artifact in the ledger). Parity here is a SWEEP-SCOPE property, not a
  code property — flag it so the sweep grades it full-universe.

YELLOW is fine in moderation but is NOT parity-by-construction: it requires a **dedicated parity test**
(degenerate-window reproduction that fails-without / passes-with the guard, the `#139` pattern) before
promotion. The Parity workstream will write/confirm that test on the real prototype.

### PARITY-RED — research-only; will NOT reproduce live without a rewrite (flag to Lead)
Fires if the definition needs ANY of:

- a **full-history / unbounded recompute** every minute (a feature whose value at T depends on the whole
  session, not a bounded trailing window) — there is no bounded incremental form; the live path cannot
  reproduce it.
- a **pandas-only / non-polars op** or a per-window Python/expr callback (`rolling().agg(list.eval ...)`)
  that exists only in the research screen's vectorized-over-all-days backtest and has no streaming twin.
- a **look-back that cannot be made incremental** (multi-pass over the ordered window, an O(N²) pairwise
  cross-symbol op, a sort-then-join gather).

A RED candidate computed in `invent_screen.py` (which is a vectorized backtest over many days at once — a
fundamentally different execution model from the per-minute live path) may show a great IC that **cannot be
reproduced live at all** without re-deriving an incremental form. RED is allowed ONLY when the IC is
exceptional AND no GREEN/YELLOW re-expression captures it; it then requires an explicit Lead-gated decision
and a from-scratch incremental rewrite + parity verification before any promotion. Tag RED, note the
offending trait, flag it — never silently promote.

## MANDATORY GUARDS — bake the hard-won lessons into the DEFINITION

These are not optional review nits; they are the exact cracks that produced `#122`, `#131`, `#139`. Any
candidate whose `assemble()` does a **division, a std, a z-score, a coefficient-of-variation, or an OLS**
MUST include the matching guard **in its definition**, or it is not promotable regardless of tag.

The root cause is always the same: a bare `> 0.0` (or `== 0.0`) guard on a quantity that is a
**catastrophic-cancellation difference of two large near-equal sums** (`Σx² − (Σx)²/n`, or `b·Σxy − Σx·Σy`).
On a near-flat / constant / zero-variance window that difference is ~machine-eps; the backfill rolling sums
and the live Rust-kernel sums accumulate in different orders, so they straddle the bare threshold — one path
emits a finite value, the other NULL. The fix is a **relative-epsilon floor**: require the quantity to be a
non-trivial fraction of its own scale, so a genuinely-degenerate window is NULL on BOTH paths.

### Guard 1 — std / z-score / coefficient-of-variation (the `#122` / `#139` class)

Copy the production pattern from `quantlib/features/groups/volume.py` (`_VOL_STD_REL_EPS`) — the canonical
relative-std floor:

```python
# module level — choose the scale (see note below)
_MYFEAT_STD_REL_EPS = 1e-9   # large-magnitude values (share volume ~1e3-1e4)
# _MYFEAT_STD_REL_EPS = 1e-6 # small-integer counts (trade-count etc.; see trade_freq_z._TFZ_STD_REL_EPS)

def assemble(self) -> dict[str, pl.Expr]:
    std = std_("myval", w)
    mean_w = mean_("myval", w)
    zscore = (pt_("myvalT") - mean_w) / std
    return {
        # std is null during warmup (<2 samples) -> stays null. A near-constant window gives a tiny std
        # that a bare `std > 0` lets through (and that diverges stream-vs-backfill); require std to be a
        # non-trivial fraction of the mean so BOTH paths emit NULL on the degenerate window.
        f"myfeat_z_{w}m": (
            pl.when(std > _MYFEAT_STD_REL_EPS * mean_w.abs())
            .then(zscore)
            .otherwise(pl.lit(None, dtype=pl.Float64))
        ),
    }
```

**Choosing the epsilon scale:** it must dominate the cancellation residual but sit far below real dispersion.
`volume` uses `1e-9` (values ~1e3–1e4, residual ~1e-13 relative). `trade_freq_z` needs `1e-6` because small
integer counts give a residual std/mean ~1e-8 at the degenerate cell. Rule of thumb: floor ≥ ~100× the
expected cancellation residual at your value's magnitude, and ≤ ~1/100 of the smallest real
coefficient-of-variation you care about. When in doubt, reproduce the degenerate (constant-value) window and
measure the residual.

### Guard 2 — plain ratio / division (`num / denom`)

A ratio whose **denominator is a plain non-negative sum** (e.g. `sum_(body)/sum_(range)`, `Σsigned/Σvol`) is
**sign-robust** — the denominator is a sum of non-negative terms, not a cancellation difference, so its sign
never flips between paths (see `volume_exhaustion` in the ledger: 0 breaks on zero/sparse/constant volume).
For those, a `pl.when(denom > 0.0).then(num/denom).otherwise(None)` is sufficient and correct (see
`efficiency.py:59`, `overnight_intraday_split.py:83`). But ALWAYS guard the zero denominator — never emit a
raw `num / denom` that can divide by zero on an empty/all-null window (that yields `inf`/`nan`, which is a
finite-vs-NULL divergence against the other path's guarded NULL). The bb `±Infinity` defect
(`bb_position_20m`, epsilon-std div-by-zero on near-flat illiquid names) is exactly this: a division whose
guard wasn't robust.

If the denominator is itself a **cancellation difference** (a variance, a covariance, anything of the
`Σx² − (Σx)²/n` shape), it is NOT a plain ratio — use Guard 1's relative floor, not a bare `> 0`.

### Guard 3 — OLS / regression (`regressions()`, the `#131` / `#139` class)

If you declare a `regressions()`, the engine's `_ols_stat_exprs` already applies the relative floors
`_OLS_DENOM_X_REL_EPS` / `_OLS_DENOM_Y_REL_EPS` (1e-12) on `denom_x = b·Σx² − (Σx)²` and
`denom_y = b·Σy² − (Σy)²` — so a regression declared the standard way inherits the guard. Your obligations:

1. Use `regressions()` / the `slope_`/`corr_`/`r2_` accessors — do NOT hand-roll an OLS in a custom
   `FeatureGroup`, which would bypass the floors.
2. If the feature's value-side (the regressor `x` or the column it divides) can go **near-flat** for a real
   symbol (a constant-signed-flow window — the `kyle_lambda` corner), confirm the standard floor covers your
   value magnitude; a non-standard scale may need a wider floor (the Parity workstream confirms on the
   prototype).
3. Set `incremental_safe = False` on the group if its canonical algebra is a variance/correlation of
   **large-magnitude raw values** (raw share volume), where the running add/subtract rounds differently
   from the batch fresh sum past the parity ratio at a near-degenerate cell — keep it on the batch
   fresh-sum path under `FP_INCREMENTAL` until a stable-summation rewrite lands (see the `incremental_safe`
   docstring in `declarative.py`).

### The is_finite backstop (defense-in-depth, all tags)

After the relative-floor guard, a promoted feature should never emit a non-finite value. The guards above
make NULL the degenerate output; as a belt-and-suspenders backstop the inventor may wrap the final
expression so any `inf`/`-inf`/`nan` that slips through becomes NULL on BOTH paths identically:

```python
value = pl.when(denom_ok).then(expr).otherwise(None)
safe  = pl.when(value.is_finite()).then(value).otherwise(None)   # is_finite() backstop
```

This is identical on the rolling and the kernel path (both evaluate the same `assemble()` expr), so it never
creates a divergence — it only converts a stray non-finite into the agreed NULL. Use it on any
division/std/log feature as cheap insurance.

## Decision checklist (paste into a candidate's backlog row)

```
Parity-tag:  GREEN | YELLOW | RED
- expressible as a ReductionGroup (reduced/points/assemble)?  (yes → GREEN-eligible)
- needs OLS/regression?                                        (yes → YELLOW, use regressions(), inherits OLS floor)
- needs autocorr / quantile / sort / run-length in-window?     (yes → YELLOW, custom FeatureGroup + dedicated parity test)
- needs a cross-sectional gather at T?                         (yes → YELLOW, sweep must grade full-universe)
- needs full-history / pandas-only / non-incremental look-back?(yes → RED: rewrite before promotion, flag Lead)
GUARDS (mandatory if the assemble() does any of these):
- std / z-score / CV?        → relative-eps floor (Guard 1, copy volume.py _VOL_STD_REL_EPS pattern)
- plain ratio (denom = sum)? → guard denom > 0 → NULL (Guard 2); never raw num/denom
- ratio with variance denom? → relative-eps floor (Guard 1, NOT bare > 0)
- OLS?                       → use regressions() (inherits denom_x/denom_y floors); incremental_safe call (Guard 3)
- is_finite() backstop on any division/std/log feature
Parity-note: <the single trait that set the tag + which guard(s) the definition ships>
```

## How the inventor uses this (backlog convention)

1. When authoring `backlog.md`, add a `Parity-tag:` line to each KEEP row (next to the `RT-tag:` line from
   `RT_COST_SCREEN.md`). A KEEP feature carries BOTH tags before any promotion PR.
2. Prefer the GREEN re-expression of an idea: a sum-ratio instead of a per-window quantile; a sign-change
   *count* instead of a run-length; a `ReductionGroup` instead of a custom `FeatureGroup`. The GREEN form is
   both the cheapest (RT) and the parity-true (Parity) one — the two screens agree.
3. A **YELLOW** tag is fine but is a parity line item: it needs a dedicated degenerate-window parity test
   (fails-without/passes-with the guard) on the real prototype before promotion — the Parity workstream
   writes/confirms it. A **RED** tag is a STOP: flag it to the Lead with the IC justification; it needs an
   incremental rewrite + verification, it cannot ride a routine fingerprint deploy.
4. Once a candidate exists as a real quantlib prototype, parity is CONFIRMED — not assumed — by the generic
   latest-parity test the suite already runs on every group:

   ```bash
   docker run --rm -v /home/ben/quant-fp:/app -w /app fp-dev \
     python -m pytest tests/test_fp_latest.py -k <group_name> -q
   ```

   plus a dedicated degenerate-window test for any YELLOW feature (the `#139` pattern: build a constant /
   near-flat / single-bar window, assert live `compute_latest` == backfill `compute().last`, and PROVE it
   fails when the guard is reverted). The rubric is the cheap definition-time filter; this is the
   confirmation.

## Applied to the current invent batch (preliminary, from `backlog.md` definitions)

Tagging from the screened definitions (the screen computes these in a vectorized all-days backtest; these
tags describe the **per-minute live parity** of the same algebra). The strong-IC head is all GREEN — the
parity screen agrees with the RT screen that the best ideas are also the safest to promote.

| candidate | Parity-tag | trait + required guard |
|---|---|---|
| `f_size_tail_ratio`, `f_avg_print_size`, `f_block_share`, `f_body_efficiency` | **GREEN** | ratio of bounded sums; denom is a plain sum → Guard 2 (`denom>0→NULL`) + is_finite backstop |
| `f_size_cv`, `f_vol_cv`, `f_interarr_burst` | **GREEN** | std/mean coefficient-of-variation → **Guard 1 mandatory** (relative-eps std floor) |
| `f_vol_accel`, `f_close_loc_shift`, `f_print_size_accel`, `f_intensity_accel`, `f_flow_accel` | **GREEN** | sub-window-vs-window ratio of bounded sums → Guard 2 |
| `f_ofi_window`, `f_signed_notional_imb` | **GREEN** | Σsigned/Σtotal; denom non-negative sum → Guard 2 (sign-robust like `volume_exhaustion`) |
| `f_vwap_dev_mean`, `f_vwap_dev_now`, `f_close_loc_mean`, `f_wick_asym` | **GREEN** | means of per-bar shape primitives → Guard 2 on any divisor |
| `f_ret_autocorr1`, `f_vol_autocorr1`, `f_flow_autocorr1` | **YELLOW** | lag-1 autocorr → ordered series, custom `FeatureGroup`, needs dedicated parity test (none is a top-IC survivor) |
| `f_ret_runfrac` | **YELLOW** | run-length / path-dependent → custom path (the sign-change *count* re-expression would be GREEN) |

**Read-out: no RED in the batch, and every top-IC vol predictor is GREEN.** The four GREEN
coefficient-of-variation / std features (`f_size_cv`, `f_vol_cv`, `f_interarr_burst`, and the z-score-shaped
ones) are the ones that MUST ship Guard 1 before promotion — that is the single actionable obligation this
gate puts on the batch. The three autocorr features and `f_ret_runfrac` are YELLOW (custom path → dedicated
parity test), and none is a top-IC survivor, so the inventor can promote the strongest features GREEN with
only the standard guards. The parity screen confirms, like the RT screen, that the best ideas are also the
cheapest to make parity-true.

---

_Cross-linked from `RT_COST_SCREEN.md` (the symmetric RT-cost gate) and `experiments/BACKLOG.md` (Modeller
portfolio). Parity workstream; see `~/.quant-ops/parity-ledger.md`. Guard patterns:
`quantlib/features/groups/volume.py` (`_VOL_STD_REL_EPS`), `trade_freq_z.py` (`_TFZ_STD_REL_EPS`),
`declarative.py` (`_OLS_DENOM_X_REL_EPS`/`_OLS_DENOM_Y_REL_EPS`); cases `#122` / `#131` / `#139`._
