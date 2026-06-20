# Incremental-readiness table — what compute can still move into running state

> The accountability surface (auto-generated from the registry + the P0/P1/P3 classification). For
> each of the 63 feature groups: its KIND, whether it already rides SHARED RUNNING STATE (and which
> win put it there), and the remaining migration LEVER. Pairs with docs/latency_budget.yaml (the
> per-group budget gate) — this table says WHERE the compute lives; the budget says how much it costs.
>
> Regenerate after any group add/migration. The two REAL remaining latency levers are Lead/Ben-gated:
> the P2 FP_INCREMENTAL enablement flip (the 23 reductions → live incremental) and the Rust-resident
> emit kernel (the only thing that moves the ~289ms isolated per-bet floor toward <100ms).

**63 groups / 728 features**: 23 ReductionGroup, 4 StatefulGroup, 36 hand-written FeatureGroup.

## ReductionGroup (23 groups, 377 feat)

| group | feat | running state today | remaining lever |
|---|---|---|---|
| `clean_momentum` | 12 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `count_fano` | 1 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `distribution` | 20 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `efficiency` | 18 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `liquidity` | 15 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `market_beta` | 21 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | P2-followup: corr-denom stable form, then enable |
| `momentum` | 22 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `momentum_consistency` | 18 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `ohlc_vol` | 12 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `price_volume` | 70 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | P2-followup: corr-denom stable form, then enable |
| `quote_spread` | 21 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `range_expansion` | 2 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `realized_range` | 3 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `residual_analysis` | 6 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | P2-followup: corr-denom stable form, then enable |
| `return_dynamics` | 15 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | P2-followup: corr-denom stable form, then enable |
| `signed_trade_ratio` | 4 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `trade_flow` | 23 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `trade_freq_z` | 4 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `trend_quality` | 30 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `volatility` | 15 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `volume` | 23 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | P2-followup: corr-denom stable form, then enable |
| `volume_exhaustion` | 10 | shared running-sum (WindowedSumState) | P2 enablement flip (Lead) — incremental==batch parity-green |
| `volume_leads_price` | 12 | shared running-sum (WindowedSumState); FP_INCREMENTAL gated | P2-followup: corr-denom stable form, then enable |

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

