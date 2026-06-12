# Volume-shock overnight overlay — no edge (explorer-shapes, 2026-06-12)

**Status: REFUTED / NO-EDGE (Lead verdict).** Closes the overnight label as a shape.

## 1. Hypothesis (pre-registered)
A volume shock today (vol_z_30 high) signals liquidity-driven over-extension that partly reverses
overnight. Trade the overnight book only on shock name-days → sparse, low-turnover. Reuses the existing
overnight label + the existing vol_z_30 feature (cheapest shape to test). Honest pre-registered prior:
~20% (LOW — the overnight label is survivorship-dead full-book across everything tested; sparsity was the
one untested lever).

## 2. Exploration
Existing overnight cross-sectional excess label (v1.1.1). vol_z_30 shock gate at thresholds {2σ, 3σ};
both directions tested (reversal and continuation). Gates: shuffle canary; survivorship per-symbol
demean (the make-or-break gate, since the full-book version was survivorship-driven); net-of-cost.

## 3. Results
Lead's verdict run: survivorship-neutralized net Sharpe NEGATIVE at every sparsity threshold. Gating on
the volume shock does not lift the shock-cohort overnight book above the (negative) full-book baseline.

## 4. Verdict + interpretation
**REFUTED — sparsity does not rescue the overnight label.** The overnight label is survivorship-dead
full-book AND in the volume-shock sub-cohort. This cleanly closes the overnight cross-sectional label as
a tradeable shape for this lens. Literature note: the documented volume-shock overnight effect
(Quantitativo; abnormal-trading-volume reversal lit) is actually CONTINUATION (high volume → positive
close-to-open), not the reversal I pre-registered, AND it dies on costs in the broad universe — surviving
only in a concentrated liquid-volatile subset (Nasdaq biotech, Sharpe 1.52). So even the
correctly-signed version is a broad-universe-cost-killed effect.

## 5. Next steps
- Overnight label as a shape: CLOSED. Not reopening the reversal version.
- **One documented revival path** (declined — blocked): the lit's surviving version is CONTINUATION on a
  concentrated liquid-volatile sector subset. We have no sector map yet (task #8), so a sector-concentrated
  continuation test is blocked. Logged; revisit only if the sector map lands AND there's a specific
  high-gap-volatility sector to target. Low priority given the broad-universe cost reality.
