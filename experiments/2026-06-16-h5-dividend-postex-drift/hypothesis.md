# H5 — Dividend POST-EX drift, with the LIQUID-TRADEABILITY gate as the PRIMARY test

**Registered:** 2026-06-16 (before run). Data: `corporate_actions_pit` (35,713 cash dividends, ex_date
look-ahead-safe; NO declaration date → POST-EX drift only, not run-up/anticipation).

## The meta-pattern this MUST confront up front

Every signal that has cleared a shuffle canary on real data this cycle — vwap_dev reversion (H1) and 8-K
event drift (H10) — turned out to be **ILLIQUID-CONCENTRATED and DEAD in the liquid (tradeable) tertile**.
H10b's liquid-tertile extinction (OOS t ~0.3–0.5 vs full-universe t 2.7) is the definitive example. So for
H5 the liquid-tradeability gate is NOT a final-step afterthought — it is the PRIMARY test. A full-universe
dividend-drift result that doesn't ALSO hold in the liquid tertile is the same illiquid mirage and a KILL.

## Hypothesis

Stocks drift after their ex-dividend date (the documented post-ex / ex-dividend-day return effect), and —
the load-bearing claim — this drift is present and tradeable in the LIQUID tertile (top 1/3 by dollar-volume)
net of ~6 bps round-trip cost, over a multi-day horizon where cost is non-binding.

## Test design (liquid gate FIRST)

1. Event = a symbol's ex_date (from corporate_actions_pit, revealed on ex_date <= ts — look-ahead-safe).
   Entry = D+1 OPEN after ex_date (tradeable, never the ex-date print). Forward returns {1,3,5,10} trading
   days, close-to/open-from per the tradeable rule. Bars `ts` is genuine UTC (13:30 UTC = 09:30 ET — do NOT
   re-introduce the off-by-240 bug; convert/verify). Per-date cross-section: ex-div cohort vs same-date
   non-event controls; per-symbol-demean; 10-seed canary; day-clustered t.
2. **PRIMARY GATE — run the FULL battery on the LIQUID TERTILE first** (top 1/3 by median dollar-volume), not
   just full-universe. Report liquid-tertile demeaned alpha + t at every horizon as the headline. Full-universe
   is the secondary/context number. KEEP requires the LIQUID tertile to clear demeaned t≥2 net of 6 bps.
3. Walk-forward OOS (TRAIN first ~63d / OOS last ~63d, within-split demean) on the liquid tertile — the
   in-sample number is a candidate, the OOS number is the lead (standing hold-out rule).
4. Dividend-yield conditioning: high-yield payers have larger ex-drops/drifts; split by dividend size
   (cash_amount / price) — is the drift in the high-yield subset, and is THAT subset liquid?

## Prior

Post-ex drift + the ex-dividend-day return anomaly are documented (Elton-Gruber and successors), tied to
tax-clientele + price-pressure. Orthogonal to intraday price; low turnover (one event per payer per quarter).
BUT: like most calendar anomalies, it may be strongest in small/illiquid names (the same trap). The honest
test is whether ANY of it survives in liquid, tradeable names.

## Expected / confidence

- Confidence the LIQUID-tertile post-ex drift clears OOS demeaned t≥2 net of cost: **~20%.** The base rate
  this cycle for "clears canary AND survives the liquid gate" is 0/2 (H1, H10 both died liquid). I pre-commit
  to that low prior. A liquid-surviving dividend drift would be the first genuinely tradeable edge.
- KEEP-AS-LEAD: liquid-tertile OOS demeaned t≥2 at ≥1 horizon, net of 6 bps. AMBIGUOUS: full-universe holds
  but liquid is borderline (1.5≤t<2). KILL: liquid tertile dead (t<1.5) — the now-familiar illiquid mirage.

## Ordering

Dispatch alongside H4 (split post-ex drift — same design, 244 in-window splits, reverse=distress /
forward=attention). Two independent event families under the IDENTICAL liquid-tradeability gate: if EITHER
survives liquid, the event class has a real tradeable edge; if BOTH die liquid like H10, the standing
conclusion hardens — every canary-clearing signal in this universe is an illiquid artifact, and the edge (if
any) requires either a liquid-specific signal or accepting illiquid execution (a different cost model).
