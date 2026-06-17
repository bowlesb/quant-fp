# R4 — Intraday-seasonality-adjusted activity (time-of-day normalization), PRE-REGISTERED

## Idea / meta-invariant fit
The platform has time-of-day LABELS (minute_of_day_et, minutes_since_open) but NO feature that says
"is this name's current volume/volatility ANOMALOUS for THIS time of day?". Intraday activity is
strongly U-shaped (open + close spikes, lunch lull), so a raw volume/range z-score conflates the
time-of-day seasonal with the genuine name-specific shock. A 2% move at 09:31 is normal; at 14:00 it
is a tail event. Removing the deterministic time-of-day seasonal isolates the real shock — the same
demeaning logic that made return_dispersion / peer_relative non-redundant, applied to the TIME axis.

## Pre-registered measurements (bars, all 379d)
For the LIQUID tier (top-1500 by adv$):
1. Quantify the intraday seasonality strength: per ET minute-of-day bucket (e.g. 30-min buckets
   09:30-16:00), the cross-symbol MEDIAN of per-minute volume and per-minute |return|. Is the
   open/close-vs-midday ratio large (>2-3x)? If seasonality is weak, normalization buys little -> no feature.
2. Stability: is the per-(minute-of-day) volume/range profile STABLE across days (so a trailing
   per-tod baseline is predictive, not noise)? Measure the rank-correlation of the tod-profile between
   the first and second half of the sample.

## Falsification / feature decision
- If intraday seasonality is weak OR unstable across days -> a tod-normalized feature is noise -> NO feature.
- If seasonality is strong AND stable -> build `intraday_seasonality` features: vol/volume normalized
  by the trailing per-(symbol or tier, minute-of-day) baseline (a STATIC nightly lookup table, parity-
  true like the behavioral-cluster map). Ship only if real + non-redundant (vs raw vol/volume and the
  tod labels) + not-noise.

## Parity note (the design that makes it feasible)
The per-(minute-of-day) baseline must be a FROZEN nightly lookup (trailing-N-day median volume/range
by minute-of-day bucket), identical in stream and backfill -> the intraday compute is just
current / baseline[minute-of-day], deterministic + parity-true. No intraday state. (Same pattern as
behavioral_clusters_v1.parquet for C1.)

## Output
Dual: a regime read (when is activity genuinely anomalous) + an intraday_seasonality FEATURE candidate.
