# Feature batch 1 — mined from the cycle-3 findings (parity-true FeatureGroups for the all-features model)

**2026-06-16.** Per Ben's core-mandate clarification: the compounding deliverable is GATHERING many weak,
parity-true, real-time features so a MODEL can exploit their COMBINATORIAL value. The bar for a FEATURE is
LOWER than a strategy: it must be a **real, parity-true, non-redundant signal (even weak), not pure
noise/overfit** — cost and standalone-tradeability do NOT gate it; the model + feature-importance decide its
combinatorial worth. A hypothesis KILLED as a standalone strategy can still yield a valuable feature.

This batch mines W1–W14 + the certified W11 for the signal-bearing quantities worth capturing as
`FeatureGroup`s (per `docs/ADDING_A_FEATURE.md`). They are BATCHED — proposed together for ONE coordinated
fingerprint bump (the Lead manages the deploy; the validation agent certifies parity/trust; the backfill agent
materializes the historical vectors).

## REDUNDANCY CHECK against the 610-feature registry (done — this SHARPENS the batch)
- **market_beta** already has `market_beta_{w}m` / `market_corr_{w}m` / `idio_vol_{w}m` (intraday-minute beta).
  → keep ONLY the NOVEL **overnight vs intraday beta SPLIT** (beta of close→open vs open→close), which does
  not exist.
- **volatility** (`realized_vol`, `parkinson_vol`) + **ohlc_vol** (`garman_klass_vol`, `rogers_satchell_vol`)
  cover vol-state richly. → vol-state feature DROPPED — fully redundant. W7's "rvol_10d dominates" VALIDATES
  the existing vol features (a model-importance result), it is NOT a new feature.
- **calendar_events** is CALENDAR-only (opex/witching/quarter-end) — NO 8-K/filings event-clock exists. →
  the 8-K event-clock is GENUINELY NEW and the highest-value candidate (the platform was designed for the
  EDGAR event-clock; 8-K data is live; W14/H10 showed 8-K conditions real drift).
- **prior_day** has `gap_open` (≈ overnight return). → overnight-return feature DROPPED — mostly redundant.
- **microstructure_burst** has `peak_trades_per_second` / `inter_arrival_cv` / `active_seconds` (intensity),
  but NOT a trailing trade-count z-score. → keep the NON-redundant normalized burst frequency.

## THE SHARPENED BATCH (3 genuinely-new, parity-true, non-redundant features)

### F4 — `trade_freq_z` — normalized activity-burst (SHIPPED this batch) ✅
`trade_freq_z_{5,15,30,60}m` = (n_trades − rolling_mean) / rolling_std — the trailing z-score of the
per-minute trade count (attention / info-shock proxy). From W14. Case-A over the existing `minute_agg`
n_trades. ReductionGroup → parity-true (test_fp_latest passed). 4 unit tests + parity green; ruff+mypy clean.
**Implemented + registered.**

### F1 — `beta_overnight` — the overnight/intraday beta SPLIT (the W11-certified signal) — SPEC
`beta_overnight_{w}d` / `beta_intraday_{w}d`: rolling-{30,60}d beta of the name's OVERNIGHT (close→open) and
INTRADAY (open→close) returns on SPY's, separately. The CERTIFIED W11 edge is exactly the overnight/intraday
beta split. Extends `market_beta` with the novel decomposition (plain beta is redundant; the split is not).
Parity: a rolling cross-name OLS with SPY as a broadcast regressor (the `StatefulRegressor(kind="broadcast")`
pattern in declarative.py already supports SPY-as-market — market_beta is the template). Daily-grain. Worth
implementing next; flagged to the Lead since it touches the broadcast-regressor path market_beta owns.

### F3 — `event_8k_clock` — 8-K event recency (the platform's designed-for feature; highest value) — SPEC
`minutes_since_8k`, `had_8k_within_{1,2,5}d`: the look-ahead-safe event clock keyed off `filings.available_at`
(minutes/days since the most recent 8-K with available_at ≤ T). From W14/H10 (the 8-K subset carried real
drift). GENUINELY NEW (calendar_events is calendar-only). **Needs a `filings` input wired into the feature
engine's input resolution** (a Case-B change touching engine/loaders — Lead-owned infra), so it's a SPEC for
the Lead to scope, not a groups/-only edit I can ship alone. Highest combinatorial value (a real
information-event conditioning variable).

## DROPPED (honest — redundant / pure noise, NOT proposed)
- W1 momentum LEVEL (per-name level artifact; `ret_*` already a feature). W13 sector momentum (sector is a
  feature; wrong-signed). W2 PEAD reaction-sign (captured by F3 + existing returns). vol-state (F2, redundant).
  overnight-return (F5, ≈ gap_open).

## Pipeline + going forward
1. F4 SHIPPED (this PR). F1 + F3 are SPECS handed to the Lead (F1 extends market_beta's broadcast path; F3
   needs the filings input wired — both touch infra beyond groups/).
2. BATCH: accumulate F4 (+ F1 when wired) → ONE coordinated fingerprint bump → validation-agent parity/trust →
   backfill materializes historical vectors → features enter the all-features dataset.
3. EVERY explorer now reports "feature candidate(s) this surfaced" alongside its verdict (added to the
   dispatch template) — so the feature-accumulation loop runs continuously, not just on this batch.
