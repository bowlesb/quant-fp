# Incremental-readiness table — what compute can still move into running state

> 👉 **Reading this at a glance?** See [`docs/FEATURE_LATENCY_EXPECTATIONS.md`](FEATURE_LATENCY_EXPECTATIONS.md)
> — the human-readable front door (per-group KIND in A/B/Rust framing + measured ms + readiness, plus the
> honest e2e bar→vector picture). THIS doc is the deeper working detail (kind/state/lever + the PARKED
> corr-denom-straddle write-up).
>
> The accountability surface (auto-generated from the registry + the P0/P1/P3 classification). For
> each of the 63 feature groups: its KIND, whether it already rides SHARED RUNNING STATE (and which
> win put it there), and the remaining migration LEVER. Pairs with docs/latency_budget.yaml (the
> per-group budget gate) — this table says WHERE the compute lives; the budget says how much it costs.
>
> Regenerate after any group add/migration. The two REAL remaining latency levers are Lead/Ben-gated:
> the P2 FP_INCREMENTAL enablement flip (now **20 of 23** reductions ready → live incremental) and the
> Rust-resident emit kernel (the only thing that moves the ~289ms isolated per-bet floor toward <100ms).
>
> ⭐ REDUCTION INCREMENTAL-READINESS: declared **20/23 incremental_safe=True**, but a REAL-DATA A/B soak
> (2026-06-17, 30 symbols × 779 graded post-warmup minutes, production `_incremental_parity` self-check,
> `slice_derive=True`) finds **15/20 actually CLEAN**. The synthetic degenerate stream (test_fp_incremental_
> features) only triggers `distribution`; the real gappy/sparse tape triggers **5 rare degenerate-cell
> guard-straddle breachers** the synthetic stream cannot reproduce — the SAME root cause as the parked
> corr-denom class, just rarer (0.4–7.8% of minutes). These 5 are NOT yet GO for FP_INCREMENTAL:
>
> | group | breach freq (real A/B) | worst ratio | worst cell | straddle |
> |---|---|---|---|---|
> | `range_expansion` | 61/779 (7.8%) | inf (null-flip) | range_expansion_5_30m | trailing-mean RATIO denom `>0` guard |
> | `trend_quality` | 21/779 (2.7%) | 1683× | price_r2_5m | OLS R² cov²/(var·var) (the parked corr-denom class) |
> | `clean_momentum` | 12/779 (1.5%) | 620× | clean_momentum_score_5m | moment/std power-sum cancellation |
> | `return_dynamics` | 4/779 (0.5%) | inf (null-flip) | autocorr_2_10m | autocorrelation denom straddle |
> | `distribution` | 3/779 (0.4%) | 10404× | ret_kurt_10m | kurtosis higher-moment cancellation |
>
> **GO (15)** — clean across the whole soak: count_fano, efficiency, liquidity, momentum, momentum_consistency,
> ohlc_vol, quote_spread, realized_range, signed_trade_ratio, trade_flow, trade_freq_z, volatility, **volume**,
> volume_exhaustion, volume_leads_price. (`volume` is clean ONLY when the centering anchor is per-MINUTE scale,
> see ⚠ below.)
>
> ⛔ **#386's 4 time-axis groups (trend_quality / clean_momentum / residual_analysis / price_volume) do NOT
> expand this set.** The DataIntegrity real-tape promotion gate (2026-06-21, see "REAL-TAPE PROMOTION GATE"
> below) measured `FP_CENTERED_TIME=1` vs `=0` on real /store tape and found the breach UNCHANGED (1683× /
> 620× / inf, identical ON vs OFF) — the flag conditions the OLS x-axis (slope is value-identical) but the
> self-check trips on the price_r2 / score near-perfect-fit y-side SSR cancellation, which it does not touch.
> NET: relaunch flip set stays **15** (NOT 19); the 4 stay correctly on the batch path under FP_INCREMENTAL.
>
> ⚠ **VOLUME ANCHOR SCALE (action item).** `volume`'s centered-std anchor comes from `daily.volume` =
> the prior-day DAILY-BAR total (~`backfill_daily`), but the reduction centers PER-MINUTE volume (~390× smaller).
> At that ~2-order scale mismatch the centering only PARTIALLY conditions → `volume` still breaches ~0.4% (worst
> 13.7×). With a per-MINUTE-scale anchor (daily-total / ~390, or per-symbol mean-minute volume) the soak measures
> **0/779 breaches (worst 0.0)**. The anchor source should be the per-minute volume scale, not the daily total.
>
> **3 PARKED** (price_volume / market_beta / residual_analysis) — a DISTINCT, harder corr-denom-straddle problem
> the centering abstraction does NOT reach; they stay correctly on the batch path under FP_INCREMENTAL (no
> correctness loss, just no incremental acceleration). See §"Parked: the corr-denom-straddle class" below.
>
> **NET: GO to flip FP_INCREMENTAL=1 PARITY=1 for the 15 clean groups** (their incremental==batch holds on
> real data); KEEP the 5 above + the 3 parked on the batch path until the guard-straddle fix lands (the
> per-group `incremental_safe=True` should be revoked for the 5, OR FP_INCREMENTAL must run the parity
> self-check live and the breach metric must stay below threshold — the 5 would trip it on real data). The
> 5 are the same engineering fix as the parked class (a cancellation-free / consistently-guarded reduction
> denom), now with a sized, prioritized real-data target list.

**63 groups / 728 features**: 23 ReductionGroup, 4 StatefulGroup, 36 hand-written FeatureGroup.

## ReductionGroup (23 groups, 377 feat)

| group | feat | running state today | remaining lever |
|---|---|---|---|
| `clean_momentum` | 12 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `count_fano` | 1 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `distribution` | 20 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `efficiency` | 18 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `liquidity` | 15 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `market_beta` | 21 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | PARKED — corr-denom-straddle (see §Parked); centering does NOT apply (regressors small) |
| `momentum` | 22 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `momentum_consistency` | 18 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `ohlc_vol` | 12 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `price_volume` | 70 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | PARKED — corr-denom-straddle on the RETURN regressor (see §Parked); centering volume does NOT fix it (measured) |
| `quote_spread` | 21 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `range_expansion` | 2 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `realized_range` | 3 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `residual_analysis` | 6 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | PARKED — near-perfect-fit SSR cancellation (see §Parked); already mean-centered, anchor N/A |
| `return_dynamics` | 15 | shared running-sum (WindowedSumState) | READY — un-gated by P2 Neumaier (#283/#294); incremental==batch parity-green |
| `signed_trade_ratio` | 4 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `trade_flow` | 23 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `trade_freq_z` | 4 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `trend_quality` | 30 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `volatility` | 15 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `volume` | 23 | shared running-sum (WindowedSumState) + centered-std (#307) | READY — un-gated by the centered-power-sum std; incremental==batch parity-green |
| `volume_exhaustion` | 10 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `volume_leads_price` | 12 | shared running-sum (WindowedSumState) | READY — un-gated by P2 Neumaier (#283/#294); incremental==batch parity-green |

## StatefulGroup (4 groups, 87 feat)

| group | feat | running state today | remaining lever |
|---|---|---|---|
| `candlestick` | 12 | resident StatefulEngine (EMA/lag/extrema fold) | DONE — resident |
| `price_levels` | 21 | resident StatefulEngine (EMA/lag/extrema fold) | DONE — resident |
| `price_returns` | 40 | resident StatefulEngine (EMA/lag/extrema fold) | DONE — resident |
| `technical` | 14 | resident StatefulEngine (EMA/lag/extrema fold) | DONE — resident |

## FeatureGroup (hand-written) (36 groups, 264 feat)

| group | feat | running state today | remaining lever |
|---|---|---|---|
| `asset_flags` | 4 | consolidated point-in-time pass (one shared frame) | DONE — consolidated |
| `breadth` | 30 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `calendar` | 4 | consolidated point-in-time pass (one shared frame) | DONE — consolidated |
| `calendar_events` | 7 | consolidated point-in-time pass (one shared frame) | DONE — consolidated |
| `cross_sectional_rank` | 6 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `daily_beta` | 3 | SessionCache daily memo (P1 #281) | DONE — cached; broadcast-consolidation = NET NEGATIVE (measured) |
| `draw_range` | 3 | own latest-only window agg (#257) | DONE — latest-only; chunk-kind candidate (P3.3, fp-gated) |
| `dumper_state` | 6 | shared session-cumulative pass (P3.1 #285) | DONE — could promote to declared CumulativeState kind |
| `edgar_filing_frequency` | 10 | SessionCache filings snapshot; intraday available_at<=minute gate | hybrid EVENT-kind (cache+invalidate-on-filing or leave; cheap) |
| `gap_fill_state` | 2 | shared session-cumulative pass (P3.1 #285) | DONE — could promote to declared CumulativeState kind |
| `inter_arrival` | 3 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `intraday_seasonality` | 2 | own latest-only session agg (P3.2 #286) | DONE — latest-only; CumulativeState kind candidate |
| `large_print_burst` | 3 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `liquidity_rank` | 2 | SessionCache daily memo (P1 #281) | DONE — cached; broadcast-consolidation = NET NEGATIVE (measured) |
| `market_context` | 36 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `market_turbulence` | 5 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `microstructure_burst` | 4 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `momentum_run` | 12 | own latest-only (skew+streak, #243/#245/#246) | ASSESSED — irreducible OLS; Rust kernel = marginal (deferred) |
| `multi_day_returns` | 28 | consolidated daily-broadcast pass (one merged-daily join) | DONE — consolidated |
| `multi_day_vwap` | 10 | consolidated daily-broadcast pass (one merged-daily join) | DONE — consolidated |
| `overnight_beta` | 3 | SessionCache daily memo (P1 #281) | DONE — cached; broadcast-consolidation = NET NEGATIVE (measured) |
| `overnight_intraday_split` | 3 | SessionCache daily memo (P1 #281) | DONE — cached; broadcast-consolidation = NET NEGATIVE (measured) |
| `peer_relative` | 3 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `print_hhi` | 2 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `prior_day` | 10 | consolidated daily-broadcast pass (one merged-daily join) | DONE — consolidated |
| `return_dispersion` | 10 | SessionCache daily memo (P1 #281) | DONE — cached; broadcast-consolidation = NET NEGATIVE (measured) |
| `round_levels` | 3 | consolidated point-in-time pass (one shared frame) | DONE — consolidated |
| `runner_state` | 6 | shared session-cumulative pass (P3.1 #285) | DONE — could promote to declared CumulativeState kind |
| `sector` | 12 | consolidated point-in-time pass (one shared frame) | DONE — consolidated |
| `sector_beta` | 6 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `sector_return` | 8 | universe gather (once in reader phase, ~7ms — NOT per-bet) | N/A — gather-phase, not a per-bet cost |
| `size_entropy` | 2 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `subminute_gap_fano` | 1 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `swing` | 9 | resident quant_tick.swing_fold Rust kernel | DONE — Rust-resident |
| `tick_runlength` | 3 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |
| `trade_size_dist` | 3 | hand-written compute_latest | candidate: latest-only / shared-pass / kind migration |

## Parked: the corr-denom-straddle class (price_volume / market_beta / residual_analysis)

> A DISTINCT reduction-stability problem, PARKED (Lead decision 2026-06-20): the 3 remaining gated reductions
> are NOT the centering class. They stay correctly on the batch fresh-sum path under FP_INCREMENTAL (no
> correctness loss, no incremental acceleration). 20/23 ready is the win; this captures the last 3 as a
> ready-to-pick-up backlog item, not lost knowledge.

**THE PROBLEM (why centering does NOT apply).** volume's gate (#307) was a MAGNITUDE cancellation: the std
power sum `Σv²−(Σv)²/n` on raw share volume ~1e6 — closed by centering on a per-symbol anchor (`Σ(v−a)²`,
shift-invariant, machine precision). The remaining 3 are a DIFFERENT root cause: a **corr/OLS DEFINED-GUARD
sign-flip on degenerate cells** — the guard threshold (`denom > eps·(Σz)²`, the #122/#131 sign-at-threshold
class) lands on OPPOSITE sides between the batch FRESH window sums and the incremental RUNNING sums when the
regressor collapses to near-constant over a gappy window. Incremental emits a value where batch NULLs (or the
reverse) → a null/non-null parity breach. There is no large-magnitude regressor to center it away.

| group | the degenerate cell | why the anchor can't fix it |
|---|---|---|
| `price_volume` (pv_correlation) | sparse symbol's one-minute RETURN regressor `x≈0` over the window → `denom_x = b·Σx²−(Σx)²` straddles the guard floor | MEASURED: centering the volume `y` regressor on the anchor leaves the breach (it is in `denom_x`, the small RETURN, not the volume magnitude). |
| `market_beta` (market_corr/idio_vol) | gappy satellite vs a dense SPY whose return is near-constant over the few paired bars → corr `denom` straddle (real 06-18 MO/SLB: corr=±1 / idio_vol=0 where batch NULLs) | BOTH regressors are small returns (~1e-3) — nothing large to center; the straddle is the guard threshold, not a magnitude term. |
| `residual_analysis` (resid_std) | near-perfect intraday fit → SSR = `noise/noise` (r²≈1) where the centered power sums round past the breach ratio | already MEAN-centered in the formula (`sxx_c = sxx − sx²/b`); the residue is the perfect-fit cancellation, not a per-symbol-anchor magnitude. |

**THE BREACH, CHARACTERIZED (2026-06-20 measurement).** It is NOT a null/non-null flip — it is a VALUE
divergence on a NORMAL corr: at the worst price_volume cell, `pv_correlation_5m` = batch −0.23502 vs
incremental −0.23497 (~5e-5 absolute, ratio ~40× the 1e-6 tolerance). At that cell (b=5) the RETURN regressor
is near-constant, so `denom_x = b·Σx² − (Σx)²` is a catastrophic cancellation (~3.23e-17). The raw OLS sums
(`sxx`, `sx`) are bit-identical or differ ~1 ULP between paths — the cancellation AMPLIFIES that ~1-ULP
difference into ~0.04% of `denom_x` → ~5e-5 in the corr. Inherent to subtracting large near-equal sums.

**BOTH CANDIDATES EVALUATED — NEITHER cleanly shippable (re-parked 2026-06-20):**
1. **Consistent guard-threshold / floor-widening** — FAILS THE BAR. Measured across 12 seeds × all windows:
   the conditioning ratio `denom_x/sx²` does NOT separate divergent from good cells — BREACH cells reach
   3.21e-12 and OVERLAP good cells (min 1.00e-12). No floor nulls all breach cells without ALSO nulling good
   cells → it perturbs GOOD cells (disqualified).
2. **Center the regressor (shift-invariant)** — does NOT perturb good cells (PROVEN: centering the X=RETURN
   regressor on a per-symbol return anchor conditions `denom_x` to machine precision, 6e-5 → 1.8e-16), BUT
   there is NO reproducible per-symbol RETURN anchor. Unlike volume (a stable daily-volume anchor), returns
   center on a per-symbol drift with no daily/static source; centering on the in-window first/mean is not
   reproducible as the window slides (the engine expires it, backfill recomputes a new one → they diverge).
   The `rebase_time_axis` precedent only applies to `kind="time"` x-slots (origin-invariant + identically
   applied); a plain return regressor has no clean origin backfill ALSO uses. NOT path-consistent.

**WHAT A REAL FIX WOULD NEED (for whoever picks this up):** either (1) a reproducible per-symbol RETURN
anchor wired into BOTH paths — a NEW designed reference (no natural daily source), or (2) a fundamentally
cancellation-free corr-denom — compensated/Kahan on the SUBTRACTION `b·Σx²−(Σx)²` itself (not just the sums),
computed identically in the batch + the numpy twin + the Rust `assemble_canonical` kernel. Both are real
engine work, not a quick value-identical fix. market_beta is the SAME class (SPY-return regressor near-constant
on gappy windows); residual_analysis is the perfect-fit-SSR variant.

VALIDATION when picked up: the gate tests `test_gappy_denom_group_still_breaches_gate_load_bearing[price_volume]`
+ `test_market_beta_breaches_on_real_gappy_spy_regressor` FLIP from breach→clean; full-set byte-eq; fp unchanged.

**UPDATE 2026-06-21 — candidate (2) (cancellation-free Kahan/compensated denom) MEASURED + REFUTED; the
ACTUAL root cause re-characterized (the prior "RETURN regressor" framing above was the wrong cell).** A
Dekker TwoProduct difference-of-products (FMA-free — py3.12 has no `math.fma`) was built for
`b·Σx²−(Σx)²` and `b·Σxy−Σx·Σy`, verified accurate to the EXACT denom of each path's sums (0 rel-err vs
`Fraction`), and run on the 8 parked groups (force-`incremental_safe`) through the real-data soak
(`scripts/incremental_realdata_soak.py`, 2026-06-17, 779 graded minutes). Findings:

- **dop does NOT close the breach.** At the worst material cell (`trend_quality` `price_r2_5m` PFE/T,
  `clean_momentum_score_5m`): corr naive Δ = 7.997e-5, corr **dop Δ = 8.291e-5** (marginally WORSE). The
  breach is NOT the subtraction's own rounding — it is that the batch and incremental paths feed
  DIFFERENTLY-CONDITIONED input sums, and compensated arithmetic on the subtraction cannot reconcile two
  differently-rounded operand sets.
- **The material breaches are the TIME-AXIS regressions, not the return regressor.** The `kind="time"` /
  OBV (`kind="cumulative"`) regressions (`trend_quality.trend`, `clean_momentum`, `price_volume.obv`) form
  `cov_n = b·Σxy − Σx·Σy` on a RAW epoch-minute axis where `Σxy ~ 1e12` (catastrophic cancellation), while
  the incremental engine uses a small REBASED origin (axis ~tens, `Σxy ~ tens`, well-conditioned). OLS is
  origin-invariant mathematically, so `denom_x` comes out bit-identical (rel 0) across the wildly-different
  raw sums — but `cov_n`/`corr` round the SAME quantity differently because one path is ill-conditioned and
  the other is not. MEASURED: re-centering the BATCH time axis shifts its corr by ~5e-6–9e-6 (the same order
  as the cross-path Δ) toward the incremental value — i.e. the conditioning IS the axis origin scale.
- **The `pv_correlation` (return-vs-volume) and `market_beta` breaches are the tiny-denom Class B, not value
  bugs.** Their input sums differ by ~1 ULP (~1e-20) and the corr Δ is 5e-16…5e-13 — BELOW the 1e-6
  tolerance. The parity-self-check ratio metric trips because the TRUE denom is near-zero (genuinely flat
  window), not because the value is wrong; dop neither helps nor hurts (no material divergence to fix).

**THE REAL FIX (re-aimed for the picker-upper): condition the TIME AXIS identically on BOTH paths.**
Generalize `rebase_time_axis` (already applied in the incremental engine) to the BATCH marshal —
`compute_reduction_batch` should center / origin-shift the `kind="time"` x-column (and the OBV cumulative
slot) before forming the windowed sums, so the batch computes `cov_n`/`denom` on the SAME small-magnitude,
well-conditioned axis the incremental path uses. Origin-invariant ⇒ value-preserving on good cells; it
removes the ill-conditioning at its source (the operand scale) instead of trying to repair it after the
subtraction. This is adjacent to RustIncremental's reduction_anchor work (task #67) — coordinate. The
cancellation-free-denom candidate (2) is CLOSED (measured-refuted); candidate (1) (a per-symbol RETURN
anchor) is moot for the material breaches (they are time-axis, not return-regressor). NO code shipped this
pass (the investigation was measure-first and the approach did not clear the bar); fp UNCHANGED.

**UPDATE 2026-06-21 — the time-axis batch conditioning BUILT behind `FP_CENTERED_TIME` (default OFF, fp
unchanged).** `compute_latest` / `compute_reduction_batch` / `build_plan` now pin a `kind="time"` regression's
x to the incremental engine's exact anchor origin (`latest − _TIME_ORIGIN_LAG·60`, the shared constant now
defined in `declarative.py`) so the live-batch OLS operand sums coincide with the incremental axis at the
anchor minute. MEASURED value-identical: the OLS operands shrink ~70× (Σxx 4.38e6→6.34e4 on a deep
trend_quality window) while `denom_x`/`cov_n` (origin-invariant) are bit-identical, and a 1298-cell ON-vs-OFF
sweep over the 4 time-axis groups diverges ≤ 2.3e-10 relative with ZERO null-flips. The full
declarative/incremental/latest/parity suite passes ON and OFF (`tests/test_fp_centered_time.py` locks scope +
value-identity + the operand-shrink-with-denom-identical proof). This is exactly the live self-check the
`incremental_safe` gate runs (`capture.py` `_incremental_parity` compares `compute_reduction_batch` vs
`IncrementalEngine` — both now conditioned identically), so it is the value-identical mechanism that lets the
time-axis groups be promoted once the gate is re-measured CLEAN on real tape.

TWO honest residuals (NOT shipped here, surfaced for the picker-upper):
1. **Backfill `compute()` is NOT conditioned by this PR** (kept on its `epoch.min()` rolling form). A single
   rolling pass CANNOT give a per-window-local small x: `rolling_sum_by((epoch − rolling_min_by(epoch))²)` sums
   terms built from each row's OWN window-min, not the window-end row's — MEASURED WRONG (it produced null-flips
   + garbage r2 0.04 vs 0.9999). And the recenter-after-roll (`Σxx − (Σx)²/b` from raw rolled sums) re-cancels
   the same way. Backfill is already accurate to ~3.4e-12 vs an exact (Fraction) reference at x≤day-span, so it
   is NOT the breach source; the gate compares `compute_reduction_batch` (now conditioned) vs incremental, so
   the latest-path conditioning is what the gate needs. A correct backfill conditioning needs a per-window
   group-by (bounded follow-up), only if grading shows backfill-vs-incremental drift on real near-perfect cells.
2. **The non-time corr-denom groups are genuine VALUE breaches, NOT sub-tolerance** (revises the §Parked claim
   above): an independent real-harness re-measure found `distribution.ret_kurt` (31–86×), `market_beta`
   (inf null-flip + 568×, beta≈0.048), `return_dynamics.autocorr` (37×, corr≈0.76) ALL show genuine
   normal-magnitude value divergences — class (c) corr-denom / higher-moment cancellation that CENTERING the
   value column (translation-invariant central moments) closes to ~1e-16 in microbench. The obstacle is the
   same as volume #307: a REPRODUCIBLE per-symbol return / SPY-return anchor wired into both paths (no natural
   daily source). `range_expansion` (mean of non-negatives — no cancellation) and `residual_analysis`
   (already mean-centered; its lever is the time axis above) are NOT centering problems. So the return-anchor
   sibling fix (not this PR) would un-park distribution + return_dynamics + market_beta; this PR un-parks the
   time-axis class (trend_quality / clean_momentum / residual_analysis / price_volume.obv) value-identically.

**⛔ UPDATE 2026-06-21 (DataIntegrity) — REAL-TAPE PROMOTION GATE for the 4 #386 groups: the synthetic
"value-identical" claim is REFUTED on real tape. FP_CENTERED_TIME does NOT close the parity breach for
trend_quality / clean_momentum; it is a NO-OP for price_volume.obv (already clean); only residual_analysis is
clean (and was clean OFF too). NET PROMOTABLE FROM THIS PR: 0 of 4 (the relaunch flip set stays 15, NOT 19).**
Reproduce: the offline real-store replay below (`/store/raw/bars/2026-06-17`, 30 syms, 779 graded post-warmup
minutes, the EXACT production self-check `capture._incremental_parity` = `compute_reduction_batch` vs
`IncrementalEngine.step(slice_derive=True)`), run twice — `FP_CENTERED_TIME=1` and `=0`. The 4 groups were
force-`incremental_safe=True` in the probe ONLY (no prod flag flipped; fp UNCHANGED — offline script).

| group | worst tol-ratio ON (=1) | worst tol-ratio OFF (=0) | flag effect | verdict |
|---|---|---|---|---|
| `trend_quality` | **1683×** (price_r2_5m, NKE) | **1683×** (identical) | none — breach unchanged | **NO-GO** |
| `clean_momentum` | **620×** (clean_momentum_score_5m, NKE) | **620×** (identical) | none — breach unchanged | **NO-GO** |
| `price_volume` | **inf** (pv_correlation null-flip) | **inf** (identical) | n/a — obv_slope clean both, pv_corr breaches | **NO-GO (group)** |
| `residual_analysis` | 0.59× (clean) | 0.59× (clean) | none — clean both | GO, but NOT a #386 win |

**WHY THE FLAG DOESN'T CLOSE IT (root cause, measured at the worst cell).** At NKE `price_r2_5m`,
incremental=0.9456719506 (flag-independent — incremental never read the flag) vs batch ON=0.9424033698 /
batch OFF=0.9424033025. The conditioning moves the BATCH value by ~6.7e-8 while the actual batch↔incremental
divergence is **~3.3e-3** — five orders of magnitude too small to matter. Decomposing the OLS outputs at that
cell: `price_slope_5m` is value-identical (diff 1.1e-13 — the TIME-AXIS x term the flag conditions is already
well-conditioned, slope matches to machine precision), but `price_r2_5m` diverges 3.8e-3. The breach lives in
the **R² goodness-of-fit y-side** (`1 − SSR/SST` on a near-perfect-fit window, r²≈0.94 → catastrophic
SSR/SST cancellation in the RESIDUAL/y-variance term), NOT the time-axis x cov term. #386 conditioned x; the
breach is in y. `clean_momentum_score` is the same SSR-fit cancellation (r²-derived). So the #386 mechanism is
correct for what it targets (the slope/cov on the raw-epoch axis) but does not reach the price_r2 / momentum
near-perfect-fit residual cancellation that the real-tape self-check actually trips on.

**price_volume nuance.** Per-column isolation shows `obv_slope_{3..120}m` is bit-identical (tol-ratio 0.0)
ON AND OFF — the #386-targeted cumulative time axis was never the price_volume breach source and needs no
conditioning. The price_volume group still cannot promote: its breach is entirely `pv_correlation_{3,5,10,20}m`
(inf null-flips), the parked return-vs-volume corr-denom class (§387 — not a time-axis problem).

**residual_analysis** is clean (worst 0.59×) — but it is clean with the flag OFF too, so it is NOT promoted
*by* #386. If the Lead wants to promote residual_analysis it can ride the 15-set flip on its own real-tape
clean record (its lever per the table is the time axis, but the gate shows no time-axis breach to fix here).

**WHAT A REAL FIX WOULD NEED.** The r²/score breach is the near-perfect-fit SSR/SST cancellation (the same
class the §Parked table flags for `residual_analysis.resid_std`): batch and incremental form `SST − SSR`
(or `cov²/(var·var)`) from differently-conditioned running vs fresh y-sums, and origin-shifting x does not
touch it. The fix is a cancellation-free R²/residual kernel (centered SSR accumulation computed identically
in both paths), the same Rust corr/OLS kernel named for §387 — NOT a time-axis conditioning. So #386 should
NOT expand the relaunch flip set; the 4 stay on the batch path under FP_INCREMENTAL (correct, just not
accelerated). The relaunch flip set is the 15 Parity-12 GO groups, real-tape-verified, unchanged by #386.

**UPDATE 2026-06-21 — the VALUE-column-centering follow-up (residual #2 above) was BUILT-AS-PROBE,
MEASURED, and the naive return-anchor framing is REFUTED. No clean value-identical centering promotion
exists for these 3 (the FP_CENTERED_VALUE sibling does NOT ship). Reproduce: `scripts/value_centering_
feasibility.py`.** Centering the value column on the per-symbol WINDOW MEAN is genuinely value-identical and
conditions the kurtosis / autocorr / market-corr cancellation to ~1e-11..1e-16 (translation-invariant central
moments / corr-denom — the microbench claim above is correct). BUT three measured obstacles block a clean
FP_INCREMENTAL promotion, and they are why this is NOT the volume-#307 case:

  (A) **NO reproducible static per-symbol RETURN anchor exists.** The volume #307 anchor works because the
      per-minute volume SCALE is stable day-to-day (a daily-snapshot constant). A RETURN anchor has no such
      source: a prior-day-derived anchor (`prior_daily_drift / 390`) is uncorrelated with today's intraday
      window mean and off by ~100 std in the breach regime → it does NOT condition. MEASURED over 3000
      breach-regime cells (tol 1e-4): raw breaches 476×, the prior-day anchor breaches **487×** (worst 1.14e2,
      no better than raw); only the per-window-mean anchor reaches 0 breaches — and that mean SLIDES with the
      window, so it is path-divergent (the engine expires it, backfill recomputes a different one).

  (B) **Rebase-after-the-fact re-introduces the cancellation** (so the #386 time-axis trick does NOT transfer).
      The time axis is conditionable because the incremental engine ACCUMULATES on the already-small rebased x
      (it controls the per-fold origin BEFORE adding). A value anchor applied by binomially rebasing the raw
      power sums `Σ(rᵏ)` under `r→r−Δ` re-runs the SAME large-near-equal subtraction (`s4 − 4Δs3 + 6Δ²s2 −
      …`) → conditioning is LOST (MEASURED: rebased rel 5.8e-7 vs direct-accumulated 2.1e-16). Conditioning
      only survives if `(r−a)ᵏ` is ACCUMULATED element-wise, which needs a static `a` known before
      accumulation → back to obstacle (A).

  (C) **For the OLS/corr groups (market_beta, return_dynamics.autocorr), centering MOVES the defined-guard
      boundary** so it is not even value-identical on the straddle cells. The production guard is
      `denom_x > eps·(Σx)²`; centering x changes `Σx` (from ~b·spy_base to ~0), so the guard RHS changes and a
      near-flat-window straddle cell can FLIP null↔non-null (MEASURED: ≥1 flip / 5000 near-flat cells). A flip
      changes the feature output and the fingerprint — disqualifying for a value-identical promotion.

So `distribution.ret_kurt` is centering-conditionable in PRINCIPLE but has no reproducible accumulate-time
anchor; `market_beta` / `return_dynamics.autocorr` additionally hit the guard-perturbation wall. The REAL
fix for all three is a **cancellation-free reduction kernel** (the Rust corr/OLS/higher-moment kernel already
named as future engine work — accumulate the centered cross/auto/central-moment products directly so neither
path forms a large-near-equal subtraction), NOT a centering anchor. `range_expansion` (mean of non-negatives,
no cancellation) and `residual_analysis` (time-axis class, handled by #386) remain NOT centering problems.
This update CLOSES the "return-anchor sibling" backlog item as measured-refuted; fp UNCHANGED (no code path
changed — the probe is an offline script).

