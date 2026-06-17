# W3 — 13D activist-stake drift on LIQUID targets: **KILL**

(Written by the Director from the completed `run_w3.log` — the run COMPUTED all results but hit a transient
API rate-limit before writing this file. Numbers are the explorer's, verified against the log.)

## Verdict: KILL

The pre-registered decisive criterion: **LIQUID-tier OOS cohort net-of-cost per-trade bootstrap CI > 0.**
Across the ENTIRE run — every liquid-tier cell, every horizon {1,3,5,10,20,40,60}d, both 13D (new stake) and
13D/A (amendment) — **ZERO cells have an OOS net CI excluding zero on the positive side** (`excl0>True` count
= 0 in the whole log). The liquid 13D activist drift does not clear the gate.

## The evidence (13D new-stake, the headline)
- **LIQUID tertile (PRIMARY), 1832 event-syms:** OOS long net straddles zero at EVERY horizon — H1 +1.19%
  CI[−0.54,+4.29], H3 +0.64% [−0.93,+3.16], H5 +0.64% [−1.14,+3.14], H10 +1.14% [−1.41,+4.87], … H60 −4.19%
  [−7.33,−0.86]. Full-sample demeaned dm% is near-zero / inside the canary at every horizon (|t| ≤ 0.93).
- **Top-300 sub-cut:** a faint longer-horizon HINT (40d dm +9.8% t=1.79, 60d +6.2%) but OOS CI straddles and
  n is tiny (52–82 events) — directionally suggestive, NOT a clear signal; underpowered.
- **Illiquid tertile (context):** strong NEGATIVE drift (H5 dm −5.6% t=−4.18, OOS net −5.8% CI excludes 0
  NEGATIVE) — the now-familiar H10b illiquid-concentration pattern: a real effect that lives in untradeable
  names with the WRONG sign for the long-activist thesis here.

## Why KILL (not AMBIGUOUS)
The data + gates are clean (real TRAIN/OOS split off the backfilled filings, per-symbol demean, canary,
per-trade bootstrap on non-overlapping round-trips, 1832 liquid event-symbols — well-powered for the liquid
tier). The liquid-tier drift is simply absent (full-sample inside canary, OOS straddles zero at every
horizon). The documented activist-announcement premium does NOT show up as a tradeable LIQUID-tier drift in
this universe/window with a D+1-open entry — likely the announcement pop is already priced by D+1-open in
liquid names (the most-watched targets), and/or the effect (like most event anomalies) concentrates in the
illiquid tail (where we see it, negatively-signed and untradeable). Same structural wall as the other event
families.

## FEATURE CANDIDATE (per the mandate — a killed strategy can yield a valuable feature)
**`days_since_13d` / `had_13d_within_{5,20}d`** — an activist-event-recency clock, the 13D analog of F3's
8-K event-clock. Even though the standalone liquid drift is null, a 13D-recency CONDITIONING feature lets the
all-features model find activist-event interactions (a real, less-crowded information event). It SHARES F3's
`filings`-input infra (the filings table is the source for both 8-K and 13D recency), so it is a SPEC for the
**batch-2 / EDGAR-content family** — added once the `filings` feature-input is wired (one infra piece serves
8-K, 13D, 10-Q, etc.).

## Disposition
KILL for the strategy backlog (no tradeable liquid edge). The activist-event-recency feature is a batch-2
EDGAR-content spec. Re-test gate for any future revival: a longer multi-year history (more liquid 13D events
at the long horizons where the faint hint appeared) + an entry-timing study (is anything left after the
D+1-open pop). Until then, do not re-dispatch raw W3.
