# Post-ex-dividend drift — no edge (explorer-shapes, 2026-06-12)

**Status: REFUTED / NO-EDGE (Lead verdict).**

## 1. Hypothesis (pre-registered)
Names that just went ex-dividend show a predictable post-event path over the next 1–5 trading days
(dividend-capture unwind → reversal, or post-drop drift), conditional on yield. Event-triggered → sparse
→ structurally low-turnover. Distinct from the ex-div label-HYGIENE work (which removes the artifact) and
from Family C (dividend FEATURES, separately verdicted no-edge). Pre-registered prior: ~30%.

## 2. Exploration
`corporate_actions` (LIVE): 7,133 cash-dividend ex-dates / 633 symbols. In-memory sparse label keyed by
the ex-dates (no panel rebuild): forward N-trading-day return from the ex-date close (N ∈ {1,3,5}),
cohort-demeaned within each ex-date. Yield = cash_rate / ex-date close. Gates: placebo-date canary
(anchor the same window on a random trading day — the effect must be ex-date-SPECIFIC); liquid-tier event
count reported.

## 3. Results
Lead's verdict run: mean cohort-demeaned post-ex excess ≈ 0 with |t| < 0.8 at all horizons; no consistent
sign across the event cohort; no yield relationship distinguishable from noise.

## 4. Verdict + interpretation
**REFUTED — no tradeable post-ex-dividend drift.** This is exactly what the literature retrodicts:
Frank-Jagannathan (1998) show the ex-day return is a MICROSTRUCTURE artifact (bid-ask bounce + tick size),
not alpha; and ex-day abnormal returns declined significantly after 2001 decimalization and 2003 tax
equalization. In a post-decimalization penny-spread market the residual effect is near-zero and what
remains is untradeable bid-ask bounce. The null is the expected, literature-consistent result. My ~30%
prior was, in hindsight, too high given the post-decimalization decay.

## 5. Next steps
- Killed; not reopening. The corporate_actions feed remains valuable for label HYGIENE (removing the
  mechanical ex-date drop from overnight labels) — that is a correctness fix, not an edge, and is owned
  separately. No further post-ex-div drift work queued.
