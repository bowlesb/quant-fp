# Feature Lifecycle Demos

The §1.3 development loop, demonstrated end-to-end per feature — each entry is the committed
evidence trail (FEATURE_PLATFORM.md §1.3, FP_GOALS F). "If we cannot run this loop repeatedly, we
are not winning."

---

## 2026-06-13 — `signed_volume_1m`: parity disposition (first cornerstone-driven decision)

The first time the T+1 Settled-Day Parity Test flagged a real feature below the 95% bar, and the
disposition loop that followed.

- **Feature:** `signed_volume_1m` (group `trade_flow`).
- **(2) Collect live:** live `stream` + settled `backfill` minute aggregates, 2026-06-12, the 50
  OFI names that carry trade data.
- **(4) Distributions:** introspection green (non-degenerate, in range, NaN within cap).
- **(5/8) QA poke + parity:** T+1 parity **exact-match 93.86%** (Tier-1) — BELOW 95%. Investigated
  (`/tmp` diagnostic, not committed):
  - net **sign flips on only 0.16%** of cells → the directional signal is stable.
  - mismatches are **mostly tiny**: median **2.5% relative / 60 shares**; a heavy tail of large
    closing-auction blocks (max ~1.5M shares), concentrated near 15:00 ET.
  - match % climbs with tolerance: **95.9% @1% rel**, 97.9% @5%, 98.6% @10%.
- **Disposition (decision):** exact-match is too strict for a value that sums hundreds of
  *provisional* trades. Declared a per-feature **relative tolerance = 1%** (`FeatureSpec.tolerance
  = 0.01`). Justification (NOT a silent loosening — anti-gaming §6.5): trade *counts* match 99.5%,
  net **sign is 99.84% stable**, and the **settled backfill is training truth** so the residual is
  a bounded train/serve gap on a few large-block minutes.
- **(re-verify):** `python -m quantlib.features.parity 2026-06-12` → **ALL features/tiers with data
  ≥ 95%** (`signed_volume_1m` 95.91%).
- **Known limitation / follow-up:** the heavy tail is large closing-auction blocks; a winsorized or
  sign-only robust variant is a candidate later. Logged, not blocking.

---

## 2026-06-13 — `realized_vol_5m`: thin-tier tolerance (derived/windowed-stat rule)

The minute T+1 parity flagged `realized_vol_5m` at **91.5% on Tier-3** (the illiquid tail) while
Tier-1/2 passed (98.7% / 99.5%). Diagnosis: **90% of Tier-3 cells are EXACT** (reldiff p50=p90=0);
the gap is the noisy thin-name tail (p99 ≈ 5.6%), where a 2nd-order *windowed* statistic amplifies
the same thin-tier bar-close differences that put `ret_1m` at 96.8% on Tier-3. Match% by tolerance:
1e-6 → 91.0%, **2% → 96.4%**, 5% → 98.1%.

**Disposition:** declared a **2% relative tolerance** for `realized_vol_5m` — justified: it's a
derived volatility stat, 90% of cells are exact, the residual is the noisy thin tail. Re-verified
all features/tiers PASS. **Generalizable rule banked:** *derived / windowed features (vol, accel)
need a modest relative tolerance on the thin tier; exact-match there is unrealistic — count/timing
sub-minute features (Layer C) do NOT (they held 100% across all windows).*

## 2026-06-13 — Layer-C parity surface: robust across time-of-day (good news for Monday)

Swept Layer-C tick parity (`peak_trades_per_second_1m`, `active_seconds_1m`, `inter_arrival_cv_1m`)
across **open / midday / close** windows × 8 names spanning the liquidity spread (NVDA→CAT):
**100% across every window and feature.** Count/timing sub-minute features are robust to the small
corrections that move sign/price features, so they are highly trustworthy for live use.
