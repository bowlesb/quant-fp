# F1 + F3 — exact infra each needs (for the Lead to scope + sequence)

Both are genuinely-new, non-redundant, high-value features but touch infrastructure beyond `groups/`, so each
needs an infra PR FIRST, then the FeatureGroup PR can land in a batch. Written precisely so the Lead can scope.

---

## F1 — `beta_overnight` (overnight vs intraday beta SPLIT — the CERTIFIED W11 signal)

**The feature:** `beta_overnight_{w}d` and `beta_intraday_{w}d` — the rolling-{30,60}-day beta of the name's
OVERNIGHT (close→open) return and INTRADAY (open→close) return, each regressed on SPY's corresponding
overnight/intraday return. (Plain `market_beta_{w}m` already exists — the NOVEL part is the
overnight/intraday DECOMPOSITION, which is exactly the certified W11 quantity.)

**Why infra (not groups-only):** market_beta uses SPY as a broadcast regressor — the
`StatefulRegressor(kind="broadcast")` pattern (declarative.py): each minute the engine reads SPY's per-minute
return at the index symbol's row and broadcasts it to all symbols. F1 needs the SAME broadcast machinery but
on the DAILY overnight/intraday return components, not the minute return. So the infra question is:

1. **The regressors F1 needs as broadcast values:** SPY's overnight return (SPY_open/SPY_prev_close − 1) and
   SPY's intraday return (SPY_close/SPY_open − 1), per DAY. The current broadcast path provides SPY's
   per-MINUTE return (close/close.shift(1)−1); F1 needs the two DAILY components.
2. **What's needed:** extend the broadcast-regressor sourcing so a group can declare an overnight / intraday
   SPY-return regressor (the daily open/prev_close and close/open of the index symbol). The math is then a
   standard rolling OLS (the existing `regressions()` + windowed-OLS path), just on the new regressor + the
   name's own overnight/intraday return as the y. The per-symbol overnight/intraday return is a short-lag
   daily column (slice-derivable), so only the SPY-broadcast side is the new infra.

**Scope estimate (Lead's call):** moderate — it reuses the OLS + broadcast paths; the new work is sourcing two
daily index-return components as broadcast regressors. Once the broadcast side exposes them, the F1 group is a
~50-line ReductionGroup with a `regressions()` declaration (like market_beta). Parity holds by construction
(the broadcast pattern is already parity-true across live/backfill/incremental).

**Value:** HIGHEST — it's the certified edge's signal as a feature; the model learns the beta×overnight
interaction directly. Strongly recommend sequencing this infra.

---

## F3 — `event_8k_clock` (8-K event recency — the platform's designed-for feature)

**The feature:** `minutes_since_8k` (minutes since the most recent 8-K with `available_at` ≤ T, look-ahead-
safe), `had_8k_within_1d` / `_2d` / `_5d` flags. The event-clock the EDGAR collection was BUILT for
(docs/EDGAR_INGESTION.md: `available_at` is the point-in-time field every event feature keys off).

**Why infra (not groups-only):** the feature engine resolves named INPUT frames (`minute_agg`, `daily`,
`reference`, `universe`) via `loaders.py` + `engine.py` input resolution. There is currently **no `filings`
input frame** delivered to groups. F3 needs:

1. **A `filings` input frame** wired into the engine's input resolution + the loaders — for a given
   (symbol-universe, date-range), the look-ahead-safe set of (symbol, form_type, available_at) 8-K rows
   (filtered to form_type LIKE '8-K%', available_at ≤ each minute T). The DB query already exists (the
   filings table is populated, indexed on (symbol, available_at)); the work is exposing it as a feature-engine
   InputSpec the same way `daily`/`reference` are, for BOTH source='stream' (live: the rows available as of
   now) and source='backfill' (the rows with available_at ≤ the historical minute).
2. **The PIT contract:** the feature for minute T must use only filings with available_at ≤ T. The
   `available_at` field is designed for exactly this; the input frame must respect it on both paths (live:
   only-seen-so-far; backfill: filtered ≤ T) so live==backfill parity holds. The `available_at_source` flag
   (atom_feed vs submissions_accepted) is day-precise for backfill rows — FINE for a minutes/days-since clock
   (the feature is robust to intraday-minute fuzziness on historical rows).

**Scope estimate (Lead's call):** moderate-to-larger — it's a NEW input source for the engine (parallel to
how `daily`/`reference` are loaded), which is the bigger piece; once the `filings` input is delivered, the F3
group is a straightforward "minutes since max(available_at ≤ T)" computation. The look-ahead test
(test_fp_lookahead) + the T+1 parity test are the guards.

**Value:** HIGH and STRATEGIC — it's the first EDGAR-content feature, a real information-event conditioning
variable the model can combine with everything else (W14/H10 showed 8-Ks condition real drift). It also opens
the path to the rest of the EDGAR-content feature family (days-since-13D, days-since-10-Q, etc.) once the
`filings` input exists. Recommend scoping the `filings`-input infra as its own PR — it unlocks a whole feature
family, not just F3.

---

## Sequencing recommendation (for the Lead)
1. The `filings`-input infra (unlocks F3 + the EDGAR-content family) — highest strategic value.
2. The SPY overnight/intraday broadcast-regressor infra (unlocks F1 — the certified-edge feature).
Each as its own infra PR; then the F1/F3 FeatureGroups land in the next coordinated feature-batch + fingerprint
bump. Meanwhile I keep accumulating groups-only feature candidates (over existing inputs) into batch-1.
