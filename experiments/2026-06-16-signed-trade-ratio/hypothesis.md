# signed_trade_ratio — pre-registration (Case-A feature)

**Registered:** 2026-06-16 (before any value/IC run)
**Author:** Modelling Agent
**Kind:** Case-A feature group (one file over EXISTING minute_agg columns; no aggregates/loaders change).

## What

A scale-free, cross-name-comparable normalization of the existing raw `signed_volume_{w}m` (shares):
`signed_trade_ratio_{w}m = sum(signed_volume, w) / sum(volume, w)` ∈ [−1, 1] for `w ∈ {5, 15, 30, 60}`.
Null when window volume is 0 (mathematically undefined, not 0). Universe-wide (trades cover all 7,668
names; this needs no quotes).

## Why (prior)

The raw `signed_volume_{w}m` is in shares, so it is dominated by high-ADV names and is NOT comparable in a
cross-section — a high-ADV name with mild imbalance can outrank a low-ADV name with extreme imbalance.
Normalizing net signed volume by total volume is the standard "trade-flow imbalance" of the microstructure
literature and is the natural cross-sectional ranker. It is the cheap, FULL-UNIVERSE complement to OFI
(quotes-only, top-2,504).

## Expected value (pre-committed)

- This is primarily an ENGINEERING/correctness deliverable (a parity-true, well-tested feature the platform
  lacked), NOT an edge claim. Trade imbalance is a weaker predictor than OFI.
- Numeric expectation if/when I IC-test it on the panel: modest standalone rank-IC (|IC| ~0.005–0.015),
  sign plausibly + (continuation) like OFI at 15m, with WIDE uncertainty. Confidence the normalization adds
  IC over raw `signed_volume_{w}m`: ~40%. Confidence it adds marginal lift over vwap_dev net-of-cost: ~15%
  (low — same liquid-tier cost wall that killed H1/H2-standalone).

## Definition of done (this work-unit)

1. `SignedTradeRatioGroup(ReductionGroup)` in `quantlib/features/groups/`, registered.
2. Unit test: hand-built minute_agg frame, exact values incl. the zero-volume→null edge.
3. Parity: `tests/test_fp_latest.py` (auto-discovered) green — `compute_latest == compute().filter(last
   minute)` cell-equal. Look-ahead guard green.
4. PR for Lead review. The IC/edge test is SEPARATE and gated on the OFI/H2 family result — the FEATURE
   merge stands on parity + correctness, not on a proven edge.

## Kill / scope

- This feature SHIPS on parity + correctness regardless of IC (it's a clean, cheap, universe-wide flow
  primitive the platform was missing). The EDGE question is deferred: if later IC shows it adds nothing over
  `signed_volume_{w}m` AND no marginal lift over vwap_dev, it stays an available-but-unused feature, not a
  trading signal. That is a fine outcome — features are inputs, not strategies.
