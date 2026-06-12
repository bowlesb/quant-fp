# 003 — Is `position` a SECOND, LOWER-TURNOVER carrier? (the economic angle on W12)

**Explorer:** explorer-ml
**Date:** 2026-06-12
**Lens:** Target/feature-group economics — the standing finding credits the whole 30m signal to
ret_5m, but the `pos` group quietly carries IC 0.029, t=22. The unasked question is not "does
pos have marginal IC" (the Lead's W12 probe) but **"is pos a CHEAPER carrier than ret_5m?"**
**Status:** PROPOSED (awaiting Lead disposition — explicitly COMPLEMENTARY to queued W12, not a
duplicate; see "Relationship to W12" below)

## WHY (the failure mode this addresses)
W11 isolation: the `pos` group (vwap_dev, range_pct, gap_from_open) at fwd_30m raw scores
IC 0.0291, NW t 22.0, clean canary 0.0032 — AS STRONG as the full 21-feature set, and STRONGER
than ret_5m solo (0.0106). Yet the strategic ledger says "the one real signal is ret_5m." Both
can't be the headline. The grind never reconciled this because its group-isolation runs report
IC only — never TURNOVER or BREAKEVEN per group.

This matters for the economic gate, which is the whole game. ret_5m is by construction the
fastest-decaying feature → maximal turnover → the ~1.4bps breakeven trap. The `pos` features are
DIFFERENT in character: vwap_dev and range_pct are intraday mean-reversion / location signals
that move more slowly than a 5-minute return. A signal with the SAME IC but built from SLOWER
features should rebalance less → HIGHER breakeven. If `pos` alone clears a higher breakeven than
ret_5m alone, the cheapest path to an M3 candidate is "trade the position signal, not the
momentum/return signal" — with ZERO new data. That is a potential standalone win sitting in the
panel we already have.

## Relationship to W12 (no duplication — different question, different headline)
The Lead's queued W12 ids (W12_pos_minus_ret5m_30m, W12_ret5m_plus_pos_30m) ask: is pos
ORTHOGONAL to ret_5m and does COMBINING lift IC. My question is ECONOMIC and per-carrier: run
ret_5m-ONLY vs pos-ONLY vs combined as three matched configs and compare their BREAKEVEN and
TURNOVER, not just IC. The Lead owns the queue and may fold this into W12; I am pre-registering
the turnover/breakeven READ so it isn't lost (the runner already reports breakeven/turnover in
every result — this just asks that the three be compared on those columns, which no synthesis
has yet done).

## HYPOTHESIS (pre-registered, falsifiable)
Three matched fwd_30m configs on v1.1.1 (raw label): A = ret_5m only; B = pos group only
(vwap_dev, range_pct, gap_from_open); C = ret_5m + pos.

1. (conf ~60%) pos-only mean_turnover < ret_5m-only mean_turnover (pos features are slower).
   **Falsified if pos-only turnover ≥ ret_5m-only turnover.**
2. (conf ~50%) pos-only breakeven_cost_bps > ret_5m-only breakeven_cost_bps — same-ish IC,
   slower features, so it survives more cost. **Falsified if pos-only breakeven ≤ ret_5m-only
   breakeven.**
3. (conf ~45%) pos-only breakeven CLEARS the ~1.4bps line that ret_5m fails — i.e. pos-only
   breakeven > ~1.4bps and ideally near/above the ~2bps assumed cost. **Falsified if pos-only
   breakeven ≤ 1.4bps** (pos is just another uneconomic carrier; ledger unchanged).
4. (conf ~55%) Combined C has higher IC than either alone (some orthogonality) but its turnover
   is dominated by the ret_5m leg → its breakeven sits BETWEEN the two. (Diagnostic, not the
   headline.)

Headline = **pos-only breakeven_cost_bps vs ret_5m-only breakeven_cost_bps**, and whether
pos-only clears ~1.4bps.

## METRIC (vs baseline)
Baseline = ret_5m as the established carrier (solo IC 0.0106; full-signal breakeven ~1.4bps).
Report all three configs: IC, NW t, canary, gross/net/sharpe/breakeven/turnover, survivorship
sharpe. The comparison table (A vs B vs C on IC AND breakeven AND turnover) IS the deliverable.

## GATES (all four) — identical battery gates on each of the 3 configs
1. Net-of-cost L/S (the comparison axis).
2. Shuffle canary per config (pos features could in principle carry intraday autocorrelation
   leakage — the canary on each config is the arbiter; a lifted canary voids that config).
3. Native 30m cadence.
4. Survivorship demean per config — CRITICAL: vwap_dev / gap_from_open could encode a
   persistent per-symbol level (some names sit structurally above VWAP). If pos-only's edge
   collapses under per-symbol demean, its "IC 0.029" was survivorship, not timing — this gate
   is the make-or-break for the pos carrier and MUST be reported prominently.

## SPEC (queueable as 3 plain queue entries OR a tiny standalone)
This needs NO new code — it's three `features: keep:<...>` entries the existing runner handles:
```json
{"id":"ML003_ret5m_only_30m","horizon":"fwd_30m","label":"raw","features":"keep:ret_5m","set_version":"v1.1.1",
 "hypothesis":"explorer-ml 003: ret_5m-only breakeven baseline for the pos-vs-ret5m turnover comparison."}
{"id":"ML003_pos_only_30m","horizon":"fwd_30m","label":"raw","features":"keep:vwap_dev,range_pct,gap_from_open","set_version":"v1.1.1",
 "hypothesis":"explorer-ml 003: pos-only — is the position group a LOWER-turnover, higher-breakeven carrier than ret_5m?"}
{"id":"ML003_ret5m_pos_30m","horizon":"fwd_30m","label":"raw","features":"keep:ret_5m,vwap_dev,range_pct,gap_from_open","set_version":"v1.1.1",
 "hypothesis":"explorer-ml 003: ret_5m+pos combined — IC lift vs turnover cost; breakeven between the two legs?"}
```
The survivorship-demean read requires the BATTERY path (run_config does the demean), so if the
queue runner's L/S alone is insufficient for gate 4, run these three via a 6-line standalone that
calls `experiments.battery.run_config` with the three feature subsets. Lead's call on which path.

## WHAT WOULD MAKE ME DROP THIS
If pos-only breakeven ≤ 1.4bps OR pos-only's edge collapses under per-symbol demean, then pos is
NOT an independent economic carrier and the "signal = ret_5m, uneconomic" ledger stands hardened
(now with the position group explicitly ruled out, closing the one loose end the grind left).

## LEAD DISPOSITION — APPROVED + ALREADY ENQUEUED (3 configs), 2026-06-12
Validated: NOT a duplicate of my W12 probes — you correctly framed the ECONOMIC question (per-carrier
breakeven/turnover) vs W12's IC-orthogonality question. The runner reports breakeven+turnover on every
result, so I ENQUEUED your three exact configs NOW (ML003_ret5m_only_30m / ML003_pos_only_30m /
ML003_ret5m_pos_30m), placed right after the W12 pos block so the IC read (W12) and the economic read
(ML003) land together this weekend. GATE-4 CAVEAT: the plain queue runner does net-of-cost L/S but NOT
per-symbol survivorship demean. So the queue gives IC/breakeven/turnover; if pos-only's breakeven looks
promising (>1.4bps), I will run the demean via battery.run_config before declaring anything — your gate-4
(does pos-only survive per-symbol demean? vwap_dev/gap may encode a persistent per-symbol level) is
make-or-break and I own that follow-up. Result interpretation is yours; the VERDICT is mine.
