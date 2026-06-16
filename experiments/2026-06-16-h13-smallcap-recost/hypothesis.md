# H13 — Re-cost the illiquid signals at Ben's $5–10K/position scale (+ a hard CAPACITY ceiling)

**Registered:** 2026-06-16 (before run). The cycle's "untradeable" verdicts (H1 vwap_dev, H10 8-K drift)
used an INSTITUTIONAL cost model (30–200 bps/side). Ben's real scale is ~$100K total, ~$5–10K/position — a
tiny fraction of even an illiquid name's daily volume. The illiquid premia big money structurally can't touch
are the classic durable SMALL-CAPITAL edge. This re-evaluates whether the illiquid signals net-survive at
Ben's size, with an explicit capacity ceiling so we never confuse "works at $5K" with "works at $100K".

## The honest cost reality (measured, with its limit stated)

- The top-2,504 quoted names have measured half-spreads ~1.5–3.4 bps and **$100K–226K resting at top-of-book**
  (F/AAL/SOFI/PLTR verified). A $5–10K order there fills at ≈ the half-spread with negligible impact — i.e.
  ~3–7 bps round-trip, NOT 30–200. So at the MID/upper liquidity tiers the institutional verdict was
  far too pessimistic for Ben's size.
- **THE HARD LIMIT:** the truly ILLIQUID bottom-tertile names — where H10's 8-K alpha actually concentrates —
  are NOT in the 2,504-name quote panel, so I CANNOT measure their half-spread directly. I will estimate it
  from a spread↔dollar-volume model fit on the names I DO have quotes for, extrapolated down — and FLAG this
  as an estimate, lower-confidence, likely a LOWER bound on cost (illiquid spreads can gap far wider than the
  fit predicts, and a single $5K order can be a large fraction of a thin name's whole-day volume). An honest
  re-cost must not hide this.

## Hypothesis

At ~$5–10K/position, the illiquid-tertile H10 8-K drift (OOS demeaned +1.8–3.6% at 1–3d) and/or the H1
vwap_dev illiquid reversion net-survive realistic small-size cost (measured/estimated per-name half-spread +
a participation-based impact term), with a CAPACITY CEILING ≥ a few $100K (enough for Ben's book).

## Test design (cheap — re-scores EXISTING panels; runs in parallel with H4/H5)

1. **Per-name small-size cost model:**
   - half_spread_bps: measured from `/store/raw/quotes` for quoted names; for non-quoted (illiquid) names,
     estimated from a `half_spread ~ f(log dollar_volume, 1/price)` fit + flagged as estimate.
   - impact_bps: a small square-root impact `k * sqrt(order_notional / ADV_dollar)` with order_notional =
     $5K and $10K; report both. ADV_dollar from the bars (close×volume, 20-day median per name).
   - round_trip_cost_bps = 2*(half_spread + impact). Compare to the per-name event/signal alpha.
2. **Re-score H10 illiquid + H1 illiquid** net of THIS small-size cost, walk-forward OOS, keeping the
   liquid-vs-illiquid split. Headline = illiquid-tertile OOS net alpha at $5K and $10K/position.
3. **CAPACITY CEILING (the gate that stops self-deception):** for the surviving cohort, compute how total
   deployed capital scales before net alpha decays to zero — sweep order size / number of concurrent names ×
   per-name ADV participation. Report the $ capital at which net alpha hits zero. A signal that nets positive
   at $5K/name but whose capacity ceiling is < ~$100K is NOT a $100K edge (it's a curiosity).
4. **Turnover/holding realism:** the multi-day event horizon means low turnover (cost paid ~once per event),
   which HELPS small-capital — but the per-name position must clear in a few days without moving the name;
   fold the ADV participation into the capacity number.

## Prior / confidence

- Confidence the illiquid H10/H1 signal nets POSITIVE at $5–10K/name AND has a capacity ceiling ≥ $100K:
  **~35%.** Higher than the liquid families' priors because the cost reduction at small size is large and
  measured at the upper tiers; the real risk is (a) the truly-illiquid spread is much wider than the
  extrapolation (cost underestimate) and (b) the capacity ceiling is too low to matter.
- KEEP-AS-LEAD: illiquid OOS net positive at $5–10K/name with capacity ≥ ~$100K AND the cost estimate is
  defensible (sensitivity-tested: still positive if the illiquid spread is 2× the fit). Then it's a
  small-capital edge → feature + a SMALL-CAPITAL illiquid strategy container, paper-first.
- AMBIGUOUS: positive at $5K but capacity < $100K, or only survives if the illiquid-spread estimate is
  optimistic.
- KILL: illiquid signal nets ≤ 0 even at $5K/name with the favorable (lower-bound) cost — then the illiquid
  signals are dead even at Ben's scale and we commit fully to the LIQUID hunt.

## Guardrail (do NOT let small-capital become a license to lower the bar)

The bar is the SAME rigor: walk-forward OOS, per-symbol-demean, a DEFENSIBLE measured/estimated cost with a
2× stress, and a real capacity ceiling. "It works at $5K if I assume zero impact" is NOT a KEEP. The
small-capital framing changes the cost INPUT, not the standard of proof.

## Ordering

Dispatch NOW, in parallel with H4/H5 (it's CPU-light — re-scores existing panels, no new heavy build). The
two open questions of the cycle: (a) does ANY event family live in LIQUID names (H4/H5)? (b) are the
illiquid signals harvestable at Ben's $100K scale (H13)? Both pre-registered, both honest.
