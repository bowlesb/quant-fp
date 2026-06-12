# explorer-data journal (data archaeology)

Append-only. Lens: stare at the ACTUAL data, generate hypotheses FROM observations.
DB access is READ-ONLY. Pre-register before any OOS test. Flag oddities to QA (their lane).

---

## 2026-06-12 — Wake 1. First dig into the v1.1.1 panel.

Orientation: read EXPLORATION_PIPELINE, INSPECT, QA_LEDGER, MISSION, ROADMAP, EXPERIMENTS.
Panel inventory confirmed:
- v1.1.1 = 5,525,040 rows / 613 days / 785 syms / 2024-01-02..2026-06-11 (the M1 verdict panel).
- v1.2.0 = 25 feats (adds ofi_5m/15m/30m/signed_vol_z_30) but only 1,516 rows / 3 days / 50 syms — noise, not yet investigable.
- Feature contract (1-based): 1 ret_5m, 2 ret_15m, 3 ret_30m, 4 ret_60m, 5 vol_30m, 6 vol_60m,
  7 vol_z_30, 8 vwap_dev, 9 range_pct, 10 gap_from_open, 11 rel_ret_30m, 12 minute_of_day,
  13 day_of_week, 14-21 mom_1d..mom_10d_rel.

### OBSERVATION 1 (data-integrity, → flagged to QA) — intraday-return features are 12-20% NaN panel-wide, contradicting the QA ledger's "NaN 0.000% on all 21 features".

Per-feature NaN over the full 5.5M-row v1.1.1 panel:

| feat | pct_nan (all) | pct_nan (excl 9:30 open) |
|------|--------------:|-------------------------:|
| ret_5m       | 13.44 | 5.76 |
| ret_15m      | 13.52 | 5.84 |
| ret_30m      | 12.38 | 4.61 |
| ret_60m      | 20.06 | 12.96 |
| vol_30m/60m/vol_z_30 | 16.87 | 9.50 |
| rel_ret_30m  | 12.38 | 4.61 |
| vwap_dev, range_pct, gap_from_open, minute_of_day, day_of_week, mom_1d..mom_5d | 0.00 | 0.00 |
| mom_10d, mom_10d_rel | 0.01 | 0.01 |

Two mechanisms:
1. **9:30 ET open (minute_of_day=570) = 100% NaN for every intraday-RETURN feature** (no 5/15/30/60-min
   lookback at the open). 450,208 rows — the first cadence of every day. The open cross-section is ranked
   on ONLY vwap_dev/range_pct/gap_from_open/calendar/momentum. Correct-by-construction but it means the
   open cadence is effectively a *different, feature-degraded model*.
2. **Mid-session 5-13% NaN is NOT warmup** — it is missing 5/15/30/60-min-ago bars in THIN names.
   Concentrated in high-nominal-price, thin-trade S&P names: NVR 60.7%, LFUS 50.6%, GWW 48.0%, CW 47.0%,
   TPL 45.6%, TDY 45.1%, MUSA 44.9%, AEIS 43.4%, MTD 41.8%... ~173 names (deciles 2+) >10% mid-session NaN,
   611 names <10%. ret is NaN when the lagged bar doesn't exist (a minute with zero trades).

How the harness treats it: `quantlib.research.load_panel` loads `None→math.nan` straight into X (line 48);
LightGBM handles NaN natively (learned default split direction). So NaN is neither dropped nor imputed — the
model trains on "this feature is missing" as a (weak) signal, and the IC/verdict were computed *including*
these rows. NOT necessarily wrong, but the M1 "no edge" verdict ran on a panel where the top intraday features
are missing on 13-20% of rows, biased toward thin/high-price names and the open. Worth QA knowing the
"0.000% NaN" claim is about something else (or stale).

### OBSERVATION 2 (hypothesis seed) — ret_5m is a univariate REVERSAL signal that lives in the ILLIQUID tier.

Within-(ts × liquidity-quartile) rank-IC of ret_5m vs fwd_30m, non-NaN, excl-open, 2026-04-01..06-11
(liquidity = per-symbol median dollar-volume from bars_1m, ntile 4):

| liq_q | mean_ic | t_naive | avg_names/ts |
|-------|--------:|--------:|-------------:|
| 1 (illiquid) | -0.0231 | -2.32 | 73 |
| 2 | -0.0086 | -1.17 | 176 |
| 3 | -0.0052 | -0.70 | 226 |
| 4 (liquid)   | -0.0049 | -0.54 | 228 |

Read: ret_5m alone is short-horizon REVERSAL (negative IC), and the effect MONOTONICALLY concentrates in
the illiquid tier (-2.3% IC) and is ~zero in the liquid tier. This is the classic microstructure tension:
the univariate signal is strongest exactly where (a) it's most NaN-degraded, (b) spreads are widest, (c) it's
most likely bid/ask-bounce reversal rather than tradeable alpha. The liquid quartile — where task #5 hopes
ret_5m is cheaply tradeable — shows ~no standalone ret_5m signal in this window. (Univariate, recent window
only; sign-stability-over-time check running next before any claim.)

NOTE: this is the single-feature read, NOT the LightGBM multivariate IC (0.027 headline). The headline IC is
a multi-feature model; this isolates ret_5m's marginal direction/location. The reversal sign + illiquid
concentration is the texture the aggregate 0.027 hides.

### CORRECTION (same wake) — the "illiquid-only" pattern was a RECENT-WINDOW ARTIFACT. Full-panel: reversal is UNIFORM across liquidity tiers and STABLE across 30 months.

The 2026-04..06 window above was small-sample. Re-ran two things:

(a) **Sign stability — ret_5m within-ts IC vs fwd_30m by MONTH, full panel (non-NaN, excl-open):**
29 of 30 months NEGATIVE. The lone positive is 2026-06 (+0.013) with only 99 ts (partial current month).
Many months t < -3: 2024-02 (-0.030, t-3.9), 2024-08 (-0.034, t-3.5), 2024-10 (-0.029, t-3.7),
2025-03 (-0.041, t-3.1), 2025-04 (-0.051, t-3.8), 2025-05 (-0.031, t-3.6), 2025-12 (-0.030, t-3.4).
The strongest months are the 2025 Mar-May tariff-vol period. This is NOT a regime artifact — it is a
stable short-horizon reversal across the whole 2.5-yr panel.

(b) **By liquidity tier, FULL 613-day panel** (two independent tier definitions — full-history median
dollar-vol AND May-2026-only ADV — agree, so robust to tier construction):

| liq_q | mean_ic (full-hist tier) | t | mean_ic (May-ADV tier) | t |
|-------|------:|----:|------:|----:|
| 1 (illiquid) | -0.0249 | -10.9 | -0.0225 | -8.7 |
| 2 | -0.0187 | -9.1 | -0.0226 | -12.8 |
| 3 | -0.0209 | -11.7 | -0.0198 | -10.5 |
| 4 (LIQUID)   | -0.0203 | -9.8 | -0.0197 | -9.0 |

=> The reversal is ROUGHLY UNIFORM across liquidity (~-0.020, t -9 to -13 in every tier), NOT
illiquid-concentrated. My recent-window read was wrong; the full panel is the truth. The LIQUID tier
(q4) carries IC -0.020 at t ≈ -10 over 613 days. This is ~2× the magnitude the modeller's task-#5
liquid-50 MULTIVARIATE model reported (+0.009, opposite sign because their model blends ret_5m's
reversal with momentum's continuation). Clean univariate ret_5m on the liquid tier = a strong, stable,
SHORT reversal signal.

### OBSERVATION 3 — the reversal is NOT bid-ask bounce: it PERSISTS to 60m (retains ~58% of IC).

ret_5m within-ts IC (2025-09..2026-06, pooled non-NaN excl-open):
  vs fwd_30m: -0.0159 (t -4.8) | vs fwd_60m: -0.0092 (t -2.6)
Pure within-minute bid-ask bounce would be ~0 by 60m. The reversal retains 58% of its magnitude at
the 60m horizon => a genuine multi-minute reversal with a half-life on the order of 30-60 min. THIS IS
THE KEY TEXTURE: a reversal that survives to 60m can be traded on a LOWER-TURNOVER (60m-hold) cadence —
exactly the turnover-cut the modeller flagged as "the result that would change everything" but never
built FOR REVERSAL specifically (task #5 tested a 30m-cadence model where turnover ~3.1 binds breakeven).

### Where this leaves the edge hunt (honest framing for the Research Lead)

The modeller's task-#5 verdict ("ret_5m+position NOT tradeable on the liquid tier, breakeven 0.82-1.47bps
< measured ~3bps liquid half-spread") stands FOR THE 30m-CADENCE MULTIVARIATE MODEL. What it did NOT test
and my archaeology surfaces:
1. ret_5m is a SHORT REVERSAL (sign), stable 29/30 months, uniform across liquidity, t≈-10 on the liquid tier.
2. It PERSISTS to 60m (58% retained) => a 60m-hold reversal cuts rebalances ~2× vs 30m => roughly HALVES
   turnover-driven cost. Their breakeven was ~1.4bps at turnover 3.1; a 60m-hold variant at turnover ~1.5
   roughly DOUBLES the breakeven the signal must clear — potentially over the ~1.4-3bps liquid-spread line
   for the tightest names.
3. The honest counter (why this might still be "no"): halving turnover also roughly halves the per-period
   alpha captured if the signal decays, AND the modeller's measured liquid half-spread (~3bps median) is
   still above even a doubled breakeven for most of the 50. The escape is narrow: top-decile-LIQUIDITY +
   60m-hold + reversal-sign, net of MEASURED (not flat) cost. That precise config is untested. → proposal 001.

DEAD-END NOTE (logged so it's not re-tread): the illiquid-tier "stronger reversal" is NOT exploitable —
it's where spreads are 3-10bps and ret_5m is up to 60% NaN. Any reversal strategy must live on the liquid tier.

