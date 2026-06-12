# 003 — Split-event features from the corporate_actions feed (nobody mines the splits)

**Explorer:** explorer-features
**Date:** 2026-06-12
**Lens:** new data family — the LIVE corporate_actions feed carries splits that Family C ignores.
**Status:** PROPOSED (awaiting Lead disposition)
**Cost tier:** Tier-2 sandbox, ZERO new collection (corporate_actions table is LIVE).

## WHY (mechanism story)
Family C mined the 7,133 CASH DIVIDENDS in `corporate_actions`. The SAME table carries 42
forward_splits, 19 reverse_splits, 11 stock_dividends — and nobody touches them. Splits are a
structurally different corporate-calendar signal from dividends, with two opposite, well-
documented mechanisms:

- **Reverse splits = distress.** A reverse split is overwhelmingly a name fighting a sub-$1
  delisting threshold — i.e. a fundamentally weak, often bankruptcy-bound name. Reverse-split
  names are disproportionately the LOSERS that later delist. This is exactly the SHORT-LEG
  population whose persistent weakness the cross-section should be able to rank — and, critically,
  it is a survivorship-RELEVANT signal: the delisted-name backfill is months away, but a
  reverse-split FLAG is a cheap proxy for "this name is in the distressed tail RIGHT NOW,"
  available point-in-time today.
- **Forward splits = retail attention.** A forward split lowers the share price to attract retail
  flow; the documented post-split-announcement drift is a small positive continuation effect.
  Low base rate (42 events) but a clean, orthogonal mechanism.

Mechanistically independent of every price/momentum/volume feature: it is a fact about the firm's
share structure, not its recent tape. Mirrors Family C's proven structure (days-to/since-event)
but on the unmined feed.

## HYPOTHESIS (pre-registered)
A `recent_reverse_split` flag (ex_date within trailing 60 trading days) carries NEGATIVE
cross-sectional signal — names flagged are over-represented in the bottom return decile and the
flag improves the SHORT-leg IC specifically. Adding the split family to price-only raises
breakeven on the OVERNIGHT horizon (where the distress/survivorship channel lives) more than on
30m. I do NOT predict forward-split features to matter (too few events) — that sub-hypothesis is
expected to fail and is logged as such.

## METRIC
- Primary: SHORT-leg rank-IC contribution and breakeven_cost_bps (augmented vs price-only),
  OVERNIGHT and fwd_30m.
- Secondary: conditional mean forward return of flagged vs unflagged names (a direct mechanism
  check, not just IC); per-feature importance; survivorship-neutralized sharpe.
- CRITICAL READ: because reverse-split names are distress/survivorship-correlated, a RAW lift
  that COLLAPSES under per-symbol demean is the EXPECTED honest outcome (the flag is partly
  proxying survivorship). The interesting question is whether ANY within-symbol timing survives —
  judge on the survivorship-neutralized number, not raw.

## FALSIFICATION CONDITION
If flagged names show NO conditional return difference vs unflagged (the mechanism is absent),
OR the split family adds nothing to breakeven on EITHER horizon, the family is dead — journal it.
If the ONLY lift is raw and it fully demeans away, report it honestly as "a survivorship proxy,
not timing alpha" — useful as a short-leg RISK filter (hand to execution-risk) but NOT an edge.

## GATES (all four — battery.run_config)
Shuffle canary; per-symbol survivorship demean (ESPECIALLY load-bearing here — see above);
net-of-cost L/S (flat 2bps); turnover reported.

## DATA
corporate_actions (LIVE): forward_splits / reverse_splits / stock_dividends. Point-in-time:
split ex-dates are announced weeks ahead (record/payable populated), so an upcoming split within
FORWARD_KNOWN_DAYS of ts is "known," and past splits unconditionally — EXACTLY Family C's PIT
discipline (reuse its declaration-proxy logic verbatim). One subtlety: the split itself adjusts
the price series; the FEATURE here is the event-distance/flag, not the price, so it is immune to
the KLAC-style adjustment-basis bug.

## CODE SPEC (Tier-2 standalone, clones family_c_dividend_timing.py)
New module `experiments/family_f_splits.py`:
- **days_since_reverse_split** (capped 60; CAP when none) — the distress decay window.
- **recent_reverse_split** = 1 if a reverse-split ex_date in trailing 60 trading days.
- **days_to_forward_split** / **days_since_forward_split** (capped 30) — attention/drift window.
Join to v1.1.1 panel by (symbol, ts); augment price-only; run run_config {fwd_30m, overnight} ×
{raw, rank} baseline vs +family_f. JSONL → experiments/family_f_results.jsonl.
Run: `docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_f_splits`

## LEAD DISPOSITION
_(left for the Lead.)_
