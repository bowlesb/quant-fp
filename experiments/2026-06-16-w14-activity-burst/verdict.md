# W14 — verdict: **KILL**

## Decision
**KILL.** The pre-registered decisive condition — *LIQUID-tier OOS 2-day NO-CATALYST burst-drift net-of-cost,
per-trade bootstrap CI > 0, AND the no-catalyst subset carries it* — FAILS on every count, at every k∈{2,3,4}.

## Why (against the pre-registered gates)
1. **The no-catalyst subset does NOT carry a 2-day drift.** No-catalyst 2-day demeaned diff = −5.8 bps
   (k2, t=−0.20), −9.6 (k3), −6.0 (k4) — flat, indistinguishable from zero. The novel attention/activity
   signal the hypothesis bets on is simply absent.
2. **Shuffle canary FAILS at all k.** The real no-catalyst diff sits inside the date-shuffled band — it is
   noise, not signal.
3. **Sign does not replicate OOS.** Train diff negative → OOS diff flips positive at all k. An overfit cell.
4. **Net-of-cost is decisively negative** with the bootstrap 95% CI entirely below zero (k2 net −93 bps,
   CI [−159, −30]). The CI excludes zero on the LOSING side — the opposite of the KEEP condition.

## What the data actually shows (the honest re-label)
The only sizeable multi-day drift lives ENTIRELY in the **CATALYST (8-K) subset**: a violent burst with an
8-K within ±1 day reverses −43 bps over 2 days (k2) and −77 to −120 bps over 5 days (t up to −1.7),
strengthening with burst violence. Burst days are 2.5–3.4× enriched for an 8-K vs the 12.3% base rate, and
the enrichment rises with k. So the "activity-burst → multi-day drift" effect, to the extent it exists at
all, is **re-labelled news/PEAD over-reaction-reversal** on the catalyst subset — the known effect, not a new
signal — and even that is a fade of public news (out of W14's scope). Per the pre-registration, "if the only
drift is catalyst-driven, W14 is just a re-labelled PEAD/news effect." That is exactly the finding.

## Friction-wall verdict
The 2-day low-turnover design DID remove the cost wall that killed HF01–03 (round-trip cost ~6.6 bps is
trivial vs the −5 to −20 bps gross drift). The signal still dies — not to friction this time, but to the
**absence of a real no-catalyst edge** (canary-noise + sign-instability) and to the confound resolving the
multi-day drift into the catalyst subset. Friction-favorable design, no edge underneath.

## Guaranteed deliverable (regardless of verdict): the horizon-decay curve
5min −4.4 → 30min −2.2 → 1d +6.2 → **2d −5.8** → 5d +5.0 bps (k2, NO-catalyst): small, mixed-sign, noise.
Catalyst subset: 1d −5.7 → **2d −43.2** → 5d −76.8 bps: a clear, strengthening multi-day reversal — the
news-reaction family. Full curve in `horizon_decay.csv`; primary gates in `primary_2d_nocatalyst.csv`.

## Disposition
No paper container, no deeper certification. Catalog the catalyst-subset 8-K-burst reversal as a known-family
observation (not a W14 lead). Burst-event detection (`day_activity_z`, `day_intensity_z`) is a reusable,
honestly-built primitive if a future hypothesis needs an attention/info-shock event flag.
