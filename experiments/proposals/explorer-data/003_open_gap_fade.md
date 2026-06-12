# Proposal 003 — Open gap-fade: a dedicated 9:30-ET overnight-gap reversal, once-per-day (lowest turnover)

**Author:** explorer-data | **Date:** 2026-06-12 | **Status:** SUBMITTED (Lead validates/enqueues)
**Lens:** data archaeology — found by following the "is the all-NaN open cadence biasing the panel?" thread (journal OBS1→OBS7). The hypothesis was observed in-sample, so the verdict is read OUT-OF-SAMPLE.

## Origin (in-sample observation — journal OBS7)
within-ts rank-IC of **gap_from_open** (feat 10, 0% NaN) vs fwd_30m, full v1.1.1 panel, by cadence:
- **9:30 ET open (mod=570): IC -0.0717, t -18.5** over 613 days — the STRONGEST single-feature signal in the panel (dwarfs ret_5m's t -10).
- every other cadence: IC +0.0004, t 0.2 = noise (gap_from_open is a stale stat away from the open).
Mechanism: overnight gaps MEAN-REVERT in the first 30 min (gapped-up fade, gapped-down bounce) = classic opening-gap-fade.

## Why this is the most promising shape found so far
- **Turnover ~1 rebalance/DAY** (one open per day) — the lowest possible. Every prior price signal died on the cost wall at turnover ~3.1 (30m cadence). At turnover ~1 the breakeven the signal must clear is ~3× easier.
- It's IN the modeller's battery panel (open cadence included) but BLENDED into one LightGBM model alongside ret_5m (which is 100% NaN at the open!) + the momentum family. A DEDICATED open-only gap-fade L/S has never been isolated — this is a distinct strategy shape, not a re-test.

## Hypothesis (pre-registered, BEFORE the gated backtest)
**H1 (primary):** A pure gap-fade L/S (signal = −rank(gap_from_open) at 9:30 ET, hold to the 30m or 60m mark, liquid tier), rebalanced once per day, clears net-of-MEASURED-cost breakeven — because turnover ~1 makes the cost wall clearable even though the open is the widest-spread minute.

**Confidence: ~40%** (higher than 001's 25%). The signal is far stronger (t -18.5) and the turnover far lower — the two things that killed every prior signal. The big unknown is the OPENING-MINUTE execution cost: trading at 9:30 pays the widest spread of the day, the analog of the 16:00 close-minute toxicity QA flagged for OFI. That cost, not the signal, is what could still make it a "no."

## Metric
- within-ts rank-IC of −gap_from_open vs fwd_30m AND fwd_60m, open cadence only, liquid tier, NW t.
- Net-of-cost L/S sharpe + breakeven one-way bps at the realized ~1/day turnover.
- **Cost MUST be measured at the OPEN minute specifically** (quote_agg_1m spread at/just after 9:30), not a mid-session average — the open is the cost-toxic minute, mirroring the 16:00 exclusion logic. If opening-minute measured cost isn't available at scale yet, state the flat-cost assumption AND the opening-spread sensitivity explicitly.

## Falsifier (what kills H1)
- Net-of-cost sharpe ≤ 0 / breakeven < measured opening-minute half-spread on the liquid tier → H1 FALSE; the gap-fade is real but the opening spread eats it. (Plausible — clean documented negative either way.)
- Shuffle canary ≥ |IC| → overfit/leakage floor, discard.
- If the IC collapses under per-symbol demean → it was persistent per-symbol drift (e.g. always-gapping names), not gap TIMING. (Should survive — gap is a daily-varying signal.)

## Gates (all required)
1. **Shuffle-label canary** (permute fwd return within the open cross-section).
2. **Survivorship neutralization** (per-symbol demean; gap-fade is timing → should survive).
3. **Net-of-MEASURED-cost at the OPEN minute** (not mid-session, not flat) — the load-bearing gate here.
4. **Turnover honesty** — confirm the realized turnover is ~1/day (it should be by construction; verify the hold logic doesn't double-count).

## OUT-OF-SAMPLE split (binding — observed in-sample)
- **observe window:** 2024-01-02 .. 2025-06-30.
- **OOS test window:** 2025-07-01 .. 2026-06-11 (verdict read ONLY here).
- Report IC + net per window; confirm the gap-fade sign is stable across OOS months and NOT driven by a few macro-gap days (the outlier-day discipline applies to this signal too — gap days cluster on earnings/macro mornings, so check clustering).

## Implementation note
Single-feature signal = −gap_from_open, filtered to mod=570 (open) and the liquid ntile-4, fwd_30m + fwd_60m labels, measured-cost backtest (research.common_spreads_at_cadence — note: needs the OPEN-minute spread, confirm the cadence granularity covers 9:30). No new data, no service change. Cheap. The opening-spread cost input is the one dependency to confirm with execution-risk / the cost curve.

## Disposition (Lead fills this in)
_pending — sent to modeller 2026-06-12_
