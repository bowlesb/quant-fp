# Batch-2 — EDGAR-content feature family (all share the filings/XBRL input infra)

These are the feature candidates mined from the event/fundamental findings (W14/H10, W3, W12). They are NOT
groups-only — they all need a NEW feature-engine INPUT (the `filings` table and/or the SEC XBRL companyfacts)
wired into input resolution + loaders (parallel to how `daily`/`reference` are delivered), respecting the
look-ahead-safe `available_at` / filing-date on BOTH stream + backfill for parity. The Lead scopes that
**ONE input-infra piece**, which then unlocks this whole family. Batched separately from batch-1 (groups-only).

## The shared infra (one piece, unlocks all below)
1. **A `filings` input frame** — for a (symbol-universe, time-range), the look-ahead-safe rows
   (symbol, form_type, available_at) with available_at ≤ each minute T, for source='stream' (live: seen-so-far)
   and source='backfill' (≤ historical T). DB query exists; the work is exposing it as an InputSpec.
2. **An XBRL shares-outstanding input** (for net-issuance) — point-in-time shares per symbol over time from
   SEC companyfacts, keyed by the filing's public date. (Could be a periodic materialization into a table the
   engine reads, rather than a live API call — Lead's design.)

## The features (each a small group once its input exists)

### F3 — `event_8k_clock` (from W14/H10) ★ highest value
`minutes_since_8k`, `had_8k_within_{1,2,5}d` — minutes/days since the most recent 8-K (available_at ≤ T). The
platform's designed-for event-clock; W14 showed 8-Ks condition real multi-day drift. Needs the `filings` input.

### F6 — `event_13d_clock` (from W3) — the activist-event analog
`days_since_13d`, `had_13d_within_{5,20}d` — recency of the most recent Schedule 13D (activist >5% stake).
W3's standalone liquid drift was KILLED, but a 13D-recency CONDITIONING feature lets the model find
activist-event interactions (a real, less-crowded information event). Same `filings` input as F3 (just
form_type LIKE 'SC 13D%'). Trivial once F3's input exists.

### F7 — `net_share_issuance` (from W12) — the documented big-stock fundamental
`net_issuance_1y` = log(shares_t / shares_{t−1y}), split-adjusted, point-in-time from XBRL. W12's standalone
buyback-long/issue-short L/S was KILLED (2025 melt-up regime inverted issuance⟂momentum on 5 rebalances), BUT
net-issuance is a real, Fama-French-big-stock-confirmed fundamental, and the MODEL can learn the
issuance×momentum interaction the standalone L/S couldn't control for. Needs the XBRL-shares input. A
low-turnover, slow-moving feature — high combinatorial value.

## Why batch these (not one-at-a-time)
F3/F6 share the `filings` input exactly; F7 needs the XBRL-shares input. Once the Lead scopes the
filings-input infra (one PR), F3 + F6 are two tiny groups; the XBRL-shares infra (a second piece) unlocks F7.
So: infra PR(s) first → then F3/F6/F7 groups land in a coordinated feature-batch + fingerprint bump. This is
the first EDGAR-CONTENT feature family — cycle-1 used only the 8-K event FLAG; this mines the filings + XBRL
CONTENT the platform was built to deliver.

## Priority (for the Lead)
1. `filings` input infra → F3 (8-K clock) + F6 (13D clock) — two features, one infra piece, highest-value
   (F3 is the designed-for feature; the event class is where the less-crowded signal might live).
2. XBRL-shares infra → F7 (net-issuance) — a second infra piece, a documented slow fundamental.
