# 004 — Sector-relative / sector-neutralized returns (SPEC, gated on the sector map landing)

**Explorer:** explorer-features
**Date:** 2026-06-12
**Lens:** new data family — sector membership, which the panel has NO access to today.
**Status:** PROPOSED — PRE-SPEC, BLOCKED on the sector map (brief: "sector map landing").
**Cost tier:** Tier-2 once the map lands (no panel rebuild); the map itself is the cost (medium).

## WHY (mechanism story)
Today every label is "excess return vs the UNIVERSE median" and every momentum feature is
relative to the whole universe. But a tech name's 30-minute move is mostly **sector beta** — when
semis rip, every semi name ranks high together, and the cross-sectional model is partly ranking
*which sector is hot today*, not which NAME is mispriced. That sector-common component is:
1. **Crowded and cheap-to-arb** (everyone sees it) — low residual alpha.
2. **The dominant confound** in any universe-relative ranking. Family B tried to strip a STATISTICAL
   universe-beta from the return vector; that is a noisy 4-point regression and it DIED under
   survivorship demean. A *categorical sector membership* gives a far cleaner neutralization:
   demean each name's return by its SECTOR's contemporaneous mean.

The hypothesis worth real money: **the SECTOR-NEUTRAL idiosyncratic return is the purer cross-
sectional alpha target** — it removes the crowded sector trade and isolates the name-specific
move, which is exactly the dislocation a short-horizon ranker should harvest. Plus a **sector-
momentum conditioner** (is this name's sector trending) as a regime gate. This is the single
highest-VALUE medium-cost family I can see, and it is completely unexplored because the data
isn't wired yet. Pre-specced so it fires the moment the map lands — no idle wake.

## HYPOTHESIS (pre-registered, BEFORE the map exists)
Sector-NEUTRALIZED returns (ret_30m minus the name's sector-mean ret_30m within the ts) carry
within-ts rank-IC vs fwd_30m that is COMPARABLE to raw ret_5m but with LOWER turnover-cost
because the sector-common churn is removed — i.e. **breakeven_cost_bps RISES** vs the raw price-
only baseline even if IC is similar. Sector-momentum (sector-mean mom_5d) acts as a regime
conditioner that improves the survivorship-neutralized sharpe. If sector-neutralization is
genuinely orthogonal to the existing universe-demean, it changes the breakeven, not just the IC.

## METRIC
Primary: breakeven_cost_bps and survivorship-neutralized sharpe (augmented/sector-neutralized vs
raw price-only). Secondary: within-ts IC vs fwd_30m/fwd_60m, canary, turnover (expected LOWER for
the sector-neutral variant — that's the cost-wall attack), per-feature importance.

## FALSIFICATION CONDITION
If sector-neutralized returns produce the SAME breakeven and turnover as raw returns (no cost
benefit) and no survivorship-sharpe improvement, then sector structure is already implicitly
captured by the universe-relative features and adds nothing — kill it. If sector-momentum has no
conditioning value (sub-sample IC flat across sector-trend regimes), drop that half. Honest
negative either way; it would sharpen "the universe demean already does the job."

## DATA — BLOCKER (the reason this is a SPEC, not a runnable proposal)
Requires a (symbol → GICS sector / industry) map, point-in-time enough that membership is stable
(sector rarely changes, so a current snapshot is ~fine for a research read; flag any reclassified
names). asset_metadata has NO sector column today. The brief says a sector map is "landing" —
this proposal is the consumer waiting for it. **Action for the Lead/Manager: confirm who owns the
sector map and its ETA; this fires the same day it lands.** Source options: FMP profile endpoint
(already keyed), or the standard GICS mapping.

## CODE SPEC (Tier-2, runnable when the map lands)
New module `experiments/family_g_sector.py`. Load (symbol→sector). Per ts cross-section:
- group rows by sector; **sector_mean_ret_30m** = mean ret_30m within (sector, ts).
- **sector_neutral_ret_30m** = ret_30m − sector_mean_ret_30m (the idiosyncratic move).
- **sector_neutral_ret_60m** likewise.
- **sector_mom_5d** = sector-mean mom_5d (regime conditioner; constant within sector×ts, acts via
  interaction like calendar — note this honestly, it is not a name-discriminator alone).
Two variants to compare against raw price-only baseline: (a) ADD the sector features; (b) REPLACE
ret_30m/60m with their sector-neutral versions (the cleaner test of "is the idiosyncratic
component the better ranker"). Run run_config {fwd_30m, fwd_60m} × {raw, rank}. JSONL →
experiments/family_g_results.jsonl.

## DISTINCTNESS FROM FAMILY B
Family B removed a STATISTICAL universe-beta (noisy, 4-horizon regression, died under demean).
This removes a CATEGORICAL sector mean (clean, no estimation noise). Different construction,
different failure mode — worth the independent test even though B failed.

## LEAD DISPOSITION
_(left for the Lead — and please confirm sector-map ownership/ETA.)_
