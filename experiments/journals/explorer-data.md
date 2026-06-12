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

CORRECTION (next sub-wake): the illiquid concentration was a RECENT-WINDOW artifact — full-panel the reversal
is UNIFORM across liquidity (q4 liquid -0.020, t-9.8). So the dead-end note above is WRONG as stated: the
liquid tier carries the reversal just as strongly. The real constraint is cost, not signal location. (Kept
the strike-through for honesty re: my own correction trail.)

---

## 2026-06-12 — Wake 1, batch 2. Lead's 3 highest-value stares: regime, outliers, breadth.

All on the full v1.1.1 panel, ret_5m within-ts rank-IC vs fwd_30m, non-NaN, excl-open.

### OBSERVATION 4 — TIME-OF-DAY: reversal present at EVERY cadence, slightly stronger in the afternoon. Not a fragile time-of-day artifact.

| cadence ET | mod | mean_ic | t |
|---|---|---:|---:|
| 10:00 | 600 | -0.0254 | -3.8 |
| 10:30 | 630 | -0.0164 | -2.7 |
| 11:00 | 660 | -0.0089 | -1.4 |  ← midday lull, weakest |
| 11:30 | 690 | -0.0248 | -4.6 |
| 12:00 | 720 | -0.0154 | -2.7 |
| 12:30 | 750 | -0.0261 | -4.6 |
| 13:00 | 780 | -0.0216 | -3.9 |
| 13:30 | 810 | -0.0216 | -4.0 |
| 14:00 | 840 | -0.0174 | -2.9 |
| 14:30 | 870 | -0.0313 | -5.2 |  ← strongest |
| 15:00 | 900 | -0.0184 | -3.2 |

Every cadence negative; t -1.4 to -5.2. Strongest 14:30 / 12:30 / 11:30 / 10:00; weakest 11:00 midday.
No single cadence carries the effect — it's pervasive intraday. Robustness GOOD for a strategy (don't have
to time a specific minute). (Note: the 9:30 open cadence is excluded — ret_5m is 100% NaN there, OBS1.)

### OBSERVATION 5 — OUTLIER DAYS are recognizable MACRO-VOL EVENTS, but no single day dominates the average.

Top-20 most-negative daily IC (each is a full-day mean over ~11 cadences): -0.18 to -0.11 vs the -0.020
panel mean. The dates are a who's-who of realized-vol events:
- **2024-08-05** (-0.160) — the yen-carry-unwind / VIX-spike-to-65 crash day.
- **2025-03-11/12/13 + 2025-04-07/08/09/17** (-0.12 to -0.14) — the tariff-shock cluster (7 of the top-20).
- 2025-11-18 (-0.184, the single worst), 2026-06-10 (-0.140, recent), 2026-01-28, 2026-02-13, 2026-04-21.
On these days the cross-section violently mean-reverts (panic over-reaction → snapback). But 613 days, and
the top-20 are ~3% of days → the reversal is NOT a few-event artifact; it's a pervasive baseline + an
event-day amplification. The shuffle canary in the battery will arbitrate whether the event-day spikes are
real or in-sample selection.

### OBSERVATION 6 (the SURPRISE — overturns the naive read) — reversal is STRONGEST in LOW-dispersion (CALM) regimes, WEAKEST in high-dispersion. Monotone.

Bucketed days into quintiles by daily cross-sectional label dispersion (stddev of fwd_30m that day = realized-vol proxy):

| disp quintile | mean_day_ic | avg_disp | n_days |
|---|---:|---:|---:|
| 1 (calmest)  | **-0.0275** | 0.00397 | 123 |
| 2 | -0.0228 | 0.00449 | 123 |
| 3 | -0.0231 | 0.00500 | 123 |
| 4 | -0.0160 | 0.00557 | 122 |
| 5 (most volatile) | **-0.0150** | 0.00680 | 122 |

This LOOKS to contradict OBS5 (extreme days = strongest), but reconciles cleanly: a HANDFUL of extreme
high-vol days have enormous reversal (event snapback), but the high-dispersion quintile AS A WHOLE is
WEAKER on average — because high-vol days are a MIX of panic-snapback days (strong reversal) AND
trend/momentum-continuation days (reversal breaks down or flips). The MEAN reversal is most RELIABLE in
CALM, low-dispersion conditions. ACTIONABLE: a reversal strategy should size UP in calm regimes and DOWN
(or filter out) high-dispersion days — the opposite of "trade the vol events." The event days are
high-MEAN but high-VARIANCE; the calm days are the steady edge. → folds into proposal 001 as a regime filter.

### Synthesis of wake-1 (for the Lead)
ret_5m reversal is the most structurally robust price signal in the panel: stable across 30 months,
uniform across liquidity, pervasive across the session, persists to 60m, and — the new texture — most
RELIABLE in calm regimes. It is NOT yet shown tradeable (cost wall, task #5). The single highest-value
test is proposal 001 (liquid × 60m-hold × measured-cost × OOS), now with a regime-filter variant
(restrict/upweight low-dispersion days) as a second arm. 3 formalized items for the Monday bar:
001 (reversal-60m), 002 (NaN flag→QA), and this regime/outlier characterization feeding 001's regime arm.

---

## 2026-06-12 — Wake 1, batch 3. The open-cadence question turned up the BIGGEST signal in the panel.

Followed my own OBS1 thread: is the all-NaN-return 9:30 open cross-section silently BIASING the panel?
Tested gap_from_open (feat 10, the one feature that's 0% NaN AND meaningful at the open = the overnight gap).

### OBSERVATION 7 (the headline) — OPEN GAP-FADE: gap_from_open is the single strongest signal in the panel, but ONLY at the 9:30 open.

within-ts rank-IC of gap_from_open vs fwd_30m, full panel, by cadence group:
  - **9:30 OPEN (mod=570): IC -0.0717, t -18.5** over 613 days.
  - every OTHER cadence pooled: IC +0.0004, t 0.2 = PURE NOISE.

Interpretation: the overnight gap MEAN-REVERTS hard in the first 30 min — names that gapped up most fade,
names that gapped down most bounce. Classic opening-gap-fade. t -18.5 DWARFS ret_5m's t -10; this is the
strongest single-feature signal I've found. It's open-only because gap_from_open is only meaningful at the
open — by mid-session it's a stale distance-from-open stat with zero predictive content (hence IC~0 elsewhere).

WHY THIS REFRAMES OBS1: I flagged the 9:30 open cross-section as "feature-DEGRADED" (all return features NaN,
ranked on a smaller subset). The truth is the OPPOSITE — the open cadence carries the BEST signal in the
panel, via gap_from_open. Excluding the open (as my reversal analysis did) throws away the strongest signal.
My OBS1 framing was incomplete; the open isn't degraded, it's DIFFERENT — a distinct gap-fade regime.

WHY IT MATTERS FOR THE EDGE HUNT — this is the lowest-turnover signal possible:
- ONCE PER DAY (one open per day) → turnover ~1 rebalance/day, the floor. The cost wall that killed every
  30m signal (turnover ~3.1, breakeven ~1.4bps) is FAR easier to clear at turnover ~1.
- It's IN the modeller's battery panel (they include the open cadence) but BLENDED into one LightGBM model
  alongside ret_5m (NaN at the open!) and the momentum family. A DEDICATED open-only gap-fade L/S has never
  been isolated. This is a distinct STRATEGY SHAPE, not just a feature.

CAVEATS to gate (running / pre-registered in proposal 003):
- Is it tradeable at the open? The open is the WIDEST-spread minute of the day (price discovery). A gap-fade
  that needs to trade AT 9:30 pays the opening spread — which may eat the edge. The honest test is net-of-
  measured-cost at the open specifically (the close-minute exclusion analog: the OPEN minute may be as
  cost-toxic as the 16:00 close).
- Liquid-tier + 60m-persistence + monthly-stability: running now.
- Survivorship: gap-fade is a TIMING signal (today's gap), should survive per-symbol demean — gate it.

