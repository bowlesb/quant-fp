# W12 — Net share issuance / buyback L/S (LIQUID) — VERDICT

## KILL (with an honest underpowered/regime caveat)

Pre-registered prior: ~35% the liquid net-issuance L/S clears net-of-cost OOS with bootstrap CI > 0.
Pre-registered KILL rule: "no spread beyond canary OR net ≤ 0."

**Observed:** net per-rebalance mean = **−7.6%** (CI [−23.4%, +5.2%], p(>0)=0.18); OOS net = **−8.3%**
(CI [−36.5%, +8.7%]); canary ≈ 0. The signal is the **wrong sign** and its mean sits **inside the canary
envelope**. Both KILL conditions are met: no spread beyond canary AND net ≤ 0. The decisive criterion
(OOS net CI > 0) fails.

## Why KILL and not AMBIGUOUS
The data and gates are clean (482 names, ~458/rebalance, clean PIT instant-shares tag ~85%, split-adjusted,
canary ≈ 0, no leakage), so this is a real measurement — not a data failure to defer. The mechanism the
hypothesis bets on (buyback-long / issue-short earns a positive premium) produced a **negative** L/S over
this window. That is a genuine null/negative, so KILL is correct.

## The honest caveat (do NOT over-read the KILL)
This is NOT strong evidence the Fama-French net-issuance anomaly is dead. The window is pathological for it:
- Only **5 non-overlapping quarterly rebalances** — time-underpowered (every CI spans zero anyway).
- **Two of the five rebalances sit inside the 2025 spec-tech melt-up**, where high-issuance dilutive growth
  names (quantum / EV / crypto-miner / SPAC: OPEN +1498%, LCID +815%, QUBT, SMR…) were the biggest WINNERS.
  Net-share-issuance and "hot serially-dilutive momentum name" were positively correlated in this regime —
  the opposite of the long-run relationship. The short leg got run over.
- The three NON-melt-up rebalances are all mildly POSITIVE (+2.9%, +8.7%, +3.0%), which is faintly
  consistent with the documented effect surviving in calmer regimes — but n=3 proves nothing.

## Disposition
- **KILL for the live paper-container backlog** — there is no tradeable edge here on the available data, and
  the signal is dominated by an uncontrolled regime factor (issuance ⟂ momentum in 2025).
- **Re-test gate for any future revival:** multi-year bars (≥4–5y, ≥16–20 quarterly rebalances spanning ≥1
  bear/neutral regime) AND a momentum/beta-neutralized construction (the 2025 confound says raw issuance L/S
  is really long-value/short-momentum in disguise here). Only then is the Fama-French claim testable on our
  stack. Until that data exists, do not re-dispatch raw W12.

## Prior calibration
Pre-committed 35% → outcome NEGATIVE. The prior was reasonable (the anomaly is well-documented) but the
18-month-bar / 5-rebalance / single-regime constraint was the binding risk flagged in the pre-registration,
and it bit exactly as feared.
