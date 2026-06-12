# Conditional open-gap fade — the first honest edge candidate (explorer-shapes, 2026-06-12)

**Status: M3 CANDIDATE — passes all four M3-style gates including measured open cost. Escalated to the
Research Lead for promotion review.** This is the strongest lead the research effort has produced.

## 1. Hypothesis (pre-registered)
The overnight gap (today's 09:30 open vs the prior 16:00 close) does not resolve neutrally: it either
FADES (reverts toward the prior close intraday) or FOLLOWS (continues), and which one dominates is
CONDITIONAL on first-30-min volume. Mechanism: light-volume gaps are retail-attention/noise gaps that
overshoot and revert (Berkman et al. 2012 price-pressure reversal); heavy-volume gaps carry information
and continue. Pre-registered prior the gap axis shows a sign-coherent conditional effect: ~35%. After
the literature review (overnight reversal is documented but usually illiquid/cost-killed), the prior
that it SURVIVES measured open cost was lowered to ~30%.

## 2. Exploration (what was built, data, method, gates)
- **Data**: `research.common_daily_session_price` (helper-000, materialized once from `bars_1m` backfill
  to avoid per-experiment bar scans). 634 trading dates, 2023-12 → 2026-06. Liquid-50 cut for the cost
  test (the names with measured open-spread data).
- **Label**: `open_to_close` = close_1600/open_0930 − 1, cross-sectionally demeaned within each date
  (built in-memory, no panel rebuild).
- **Signal**: gap = open_0930/prior_close − 1; conditioning = first-30-min volume z-score (trailing
  20-session baseline).
- **Method**: equal-weight L/S decile book on the gap signal, one open→close round-trip/day, split by
  the volume-z median into low-vol and high-vol regimes.
- **Gates applied**: (a) within-date rank-IC, (b) shuffle canary (permute the excess within each date),
  (c) survivorship neutralization (per-symbol demean), (d) **walk-forward OOS** (learn the regime split
  + fade/follow direction on TRAIN, apply to TEST, horizon-purged folds), (e) **net of MEASURED open
  half-spread** (not flat 2bps), sweeping entry minute.

## 3. Results (numbers)

In-sample exploratory split (full universe and liquid-50):

| cut | regime | gap IC | real fade Sharpe@2bps | canary fade | surv-neutral fade |
|---|---|---|---|---|---|
| all | low_vol | −0.0866 | +4.12 | −0.76 | +3.82 |
| all | high_vol | +0.0195 | (follow +1.24) | (follow −1.49) | (follow +0.92) |
| liquid50 | low_vol | −0.0911 | +3.10 | −0.39 | +3.13 |
| liquid50 | aggregate | −0.0059 (t −0.55) | — | — | — |

Walk-forward OOS on the liquid-50, net of the Lead's MEASURED open half-spread (09:30=12.6 / 09:33=7.5 /
09:35=6.7 / 09:40=6.0 bps half; close exit 2.7bps half):

| regime | OOS dates | gross OOS Sharpe | net @09:30 (RT 15.3bps) | net @09:35 (9.4) | net @09:40 (8.7) |
|---|---|---|---|---|---|
| **low_vol FADE** | 517 | **+3.40** | **+2.62** | **+2.92** | **+2.96** |
| high_vol FOLLOW | 522 | −0.95 | −1.45 | −1.25 | −1.23 |

**Leakage canary (20-seed null, corrected — see update below):** shuffling the open_to_close excess
within each date and re-running the full walk-forward pipeline 20× gives a null Sharpe distribution of
**mean 0.36 / std 0.69 / max 1.62**. The real low_vol gross Sharpe **+3.40 sits ~4.3 std above the null
mean and well above its max** → no leak. (A single-seed canary on 517 dates is noisy — individual draws
ranged −0.72 to +1.29 — so the multi-seed null is the honest statement; the candidate clears it
comfortably.)

**Second arm — 10:00-entry (does the open-spread dodge exist?): NO.** Entering at 10:00 instead of the
09:30 open (to pay ~2.7bps cadence spread instead of 12.6bps open) collapses the gross OOS Sharpe to 0.36
(net +0.03). The tradeable open→close excess is FRONT-LOADED — the fade is realized in the first 30 min,
so the IC-persistence to 60m does not transfer to the book. The candidate MUST trade at the open and eat
the open spread — and still clears at +2.62. There is no cheaper-entry escape hatch on the liquid-50.

## 4. Verdict + interpretation
**CONFIRMED as an M3 candidate.** The conditional low-volume gap-fade on liquid US equities:
- carries real within-date structure (the aggregate gap IC of −0.027 was TWO opposite effects cancelling
  — light-volume fade vs heavy-volume follow — which is why it never surfaced as a plain signal);
- survives a clean shuffle canary — the walk-forward OOS gross Sharpe +3.40 is ~4.3 std above a 20-seed
  null (mean 0.36 / max 1.62) — it is NOT a leak;
- survives survivorship neutralization (per-symbol demean barely moved it) and walk-forward OOS (direction
  learned per-fold, not assumed) — it is TIMING alpha, not survivor selection;
- is **positive net of the MEASURED open-minute spread** (+2.6 to +3.0 Sharpe) on the tradeable liquid
  tier — clearing the exact cost wall (open spreads 2–4× the 10:00 cadence) that the literature and the
  Lead expected to kill it. The high-vol follow side correctly dies OOS, so the edge is specifically the
  light-volume gap reversion, the cleaner mechanism.

This is the first result in the effort to pass within-ts structure + canary + survivorship + positive
net-of-MEASURED-cost together. The literature (Berkman, Baltussen-Da-Soebhag, Della Corte-Kosowski) says
overnight reversal is usually an illiquid, cost-fragile, open-spread-killed effect — so its survival on
the liquid-50 at measured cost is surprising and warrants the Lead's independent re-run before promotion.

## 5. Next steps
- **ESCALATED** to the Research Lead for promotion review per his verdict rule (positive net at measured
  open cost after walk-forward → legitimate M3 candidate). Verdict is his.
- **Caveats that must travel with the candidate** (real, not blockers): (1) the open-spread cost is
  measured on ~3 days of `quote_agg_1m` — needs more settled sessions to firm; (2) paper-stage. The
  entry-price-decay caveat is now largely RESOLVED by the 10:00-entry arm (above): entering later doesn't
  help — the fade is front-loaded — so the conservative +2.62 at the 09:30 open (full spread, full fade)
  is the operative number, not an optimistic one.
- **Follow-up — liq2/liq3 inverted-U cut** (explorer-data archaeology: the fade is strongest at
  mid-liquidity and weakest at mega-cap, so the liquid-50 may be the WEAKEST tier and the candidate could
  be STRONGER on liq2/liq3). Blocked on `research.common_liquidity_tier` (builder delivered:
  `experiments/builders/common_liquidity_tier.sql`; the inline dollar-volume scan timed out, so it must be
  materialized once). `experiments/shape_gap_1000entry.py` auto-runs the liq2/liq3 cut the moment the tier
  table lands.
- **Verdict** is the Research Lead's; an independent re-run (ideally with the multi-seed canary) is
  warranted before promotion given how surprising a liquid-name survival is against the literature.
