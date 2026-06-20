# swing_dc MAGNITUDE-FEATURE framing — VERDICT (completes the swing_dc evaluation)

**Date:** 2026-06-20 · The feature-utility framing (the Lead's one-cycle completion after the directional
null). Same panel as the directional eval (74-feat Olsen DC ladder + Fib, 40 backfill dates 2026-04-23→
06-18, top-200 liquid, 7,850 rows), target = **|forward 30m return| = the move-MAGNITUDE** (NOT a tradeable
claim — the news-H1 reframe applied to swing_dc). READ-ONLY. fp-dev-swingdc image (the kernel is not on main).

## TL;DR — swing_dc is a directional NULL but a MAGNITUDE-FEATURE WIN. The DC threshold-response / roughness signature predicts forward move-magnitude, NET of own-vol. → swing_dc should RETURN as a magnitude feature (with the kernel), flagged for confirmatory replication.

This is the FIRST non-null in the engineered-feature track, and it is exactly the axis the design intended:
the parameter-free THRESHOLD-RESPONSE / ROUGHNESS fingerprint, not direction.

## The result — multiple structural features survive the own-vol control, OOS, and shuffle

| feature | raw IC | partial-IC (own_vol+size) | partial-IC (+ dc_sigma30) | collapse | verdict |
|---|---|---|---|---|---|
| **dc_resp_chunk_slope** | +0.187 | **+0.143** (t 13.5) | **+0.142** (t 13.4) | 0.76 | ⭐ net-new (the headline) |
| dc_resp_nlegs_slope | −0.187 | −0.151 (t 11.2) | −0.127 (t 9.6) | 0.81 | net-new |
| dc_resp_roughness | +0.195 | +0.106 (t 7.6) | +0.094 (t 7.2) | 0.55 | net-new |
| dc_minutes_since_dc_s4 | −0.115 | −0.121 (t 12.8) | −0.104 (t 10.3) | 1.05 | net-new (DC timing) |
| dc_last_leg_dur_s1 | −0.078 | −0.087 (t 8.9) | −0.101 (t 9.2) | 1.12 | net-new |
| dc_os_to_dc_s4 | +0.098 | +0.111 (t 8.2) | +0.114 (t 8.8) | 1.13 | net-new (overshoot/DC ratio) |

**Feature-utility gate: 25 of 74 features pass** (FDR-survive on the partial-t + |raw IC| ≥ 0.02 + raw/partial
sign-consistent — the CORRECTED gate from the news hunt that rejects near-zero-denominator collapse
artifacts). The headline `dc_resp_chunk_slope` is OOS-CONSISTENT (early/late +0.133/+0.149, no flip) and
**shuffle-z 13.3**.

## The decisive control — it is NOT just a better vol proxy
The obvious skeptic's objection: "swing_dc's structure is just a better own-vol estimator." REFUTED: I
controlled for `dc_sigma30_bps` (the group's OWN richer vol estimate, which is **0.94 correlated with my
own_vol** — so it IS essentially own-vol) IN ADDITION to own_vol + size. The structural features barely
move (`dc_resp_chunk_slope` 0.143 → 0.142; roughness/nlegs/timing all hold) — so the THRESHOLD-RESPONSE /
ROUGHNESS structure is genuinely NET-NEW vs even a sophisticated vol proxy. The signal is in the SHAPE of how
the directional-change decomposition scales with the threshold, not the vol level.

## Why this differs from the 11 prior nulls (and the news magnitude tier specifically)
The EDGAR/news magnitude tiers COLLAPSED under own-vol (partial-IC ≈ 0, sign-flips = near-zero-denominator
artifacts). Here the raw ICs are SUBSTANTIAL (0.10–0.19), the partials are SUBSTANTIAL (0.09–0.15) with
collapse < 1.1 (a genuine modest reduction, NOT an explosion), sign-consistent, OOS-stable, shuffle-robust,
and survive even the group's-own-vol control. This is a real own-vol-independent magnitude signal — the
parameter-free roughness fingerprint the multi-scale DC design was built to capture.

## Disposition — RETURN swing_dc as a magnitude feature; FLAG for confirmatory replication
- swing_dc DIRECTION: null (prior verdict, VERDICT.md).
- swing_dc MAGNITUDE: a clean WIN — the response-signature / roughness / DC-timing features are net-new,
  own-vol-independent forward-magnitude features. PROMOTION = trustworthy NET-NEW magnitude FEATURES for the
  model (vol-aware sizing / the battery's magnitude family), NOT a standalone tradeable strategy.
- ⭐ Per the pre-committed discipline (a survivor → flag BEFORE excitement): this is the FIRST non-null in 12
  evaluations, so it gets the HIGHEST scrutiny. **FLAGGING the Lead for a CONFIRMATORY REPLICATION on a
  DISJOINT window** before swing_dc is re-promoted — the panel is only 40 backfill dates, and a t-13 over 40
  days is strong but a deeper/disjoint window must reproduce it. Recommended next: a deeper swing_dc backfill
  (the kernel must also land on main — the #242 defect) + re-run on a non-overlapping period.

## Caveats
- SHORT panel (40 dates) — the partial-t is high but a disjoint replication is the gate to full promotion.
- The kernel-not-on-main defect (VERDICT.md) MUST be fixed for swing_dc to ship at all — this magnitude
  result is the reason to fix it (the directional null alone wasn't).
- A magnitude FEATURE's value is realized inside a model (vol-aware sizing), not as standalone P&L — no
  tradeable-edge claim is made.

## Method
Per-feature daily rank-IC vs |fwd 30m return| + own-vol/size partial-IC + collapse, with the corrected gate
(non-trivial raw IC + sign-consistency, rejecting the near-zero-denominator artifact). Decisive control adds
dc_sigma30_bps. Shuffle (100-iter label permute) + OOS purge split on the headline feature. Run on the
fp-dev-swingdc image. magnitude_results.csv has all 74.
