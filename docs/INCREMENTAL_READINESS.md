# Incremental-readiness table — what compute can still move into running state

> The accountability surface (auto-generated from the registry + the P0/P1/P3 classification). For
> each of the 63 feature groups: its KIND, whether it already rides SHARED RUNNING STATE (and which
> win put it there), and the remaining migration LEVER. Pairs with docs/latency_budget.yaml (the
> per-group budget gate) — this table says WHERE the compute lives; the budget says how much it costs.
>
> Regenerate after any group add/migration. The two REAL remaining latency levers are Lead/Ben-gated:
> the P2 FP_INCREMENTAL enablement flip (now **20 of 23** reductions ready → live incremental) and the
> Rust-resident emit kernel (the only thing that moves the ~289ms isolated per-bet floor toward <100ms).
>
> ⭐ REDUCTION INCREMENTAL-READINESS: **20/23 ready** — 17 always-safe + return_dynamics + volume_leads_price
> (P2 Neumaier #283/#294) + volume (centered-std #307). **3 PARKED** (price_volume / market_beta /
> residual_analysis) — a DISTINCT, harder corr-denom-straddle problem the centering abstraction does NOT
> reach; they stay correctly on the batch path under FP_INCREMENTAL (no correctness loss, just no incremental
> acceleration). See §"Parked: the corr-denom-straddle class" below.

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

