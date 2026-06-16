# Verdict — vwap_dev baseline at depth + H1-recheck

Judged against the PRE-REGISTERED expected result (hypothesis.md, committed before running).
Panel actually processed: **629 symbols × 126 days, 26.7M symbol-minutes, ~47k/45k cross-sections**
(H=15/H=30), day-clustered over 125 days. CPU-only sandbox, READ-ONLY /store, no engine edits, no PR.

## B1 — powered vwap_dev baseline: **CONFIRMED (with a magnitude note)**
Pre-reg: NEGATIVE and SIGNIFICANT, ~−0.02 to −0.04.
Result: **IC = −0.0581 (t=−32.3) at H=15, −0.0657 (t=−27.6) at H=30**, day-clustered over 125 days.
Sign and significance are emphatically confirmed (canary clean: mean ≈ 0.0001, std ≈ 0.048). The
magnitude is somewhat **stronger** than the pre-registered −0.02 to −0.04 band — but the pre-reg's
key falsifier was "a near-zero or positive baseline would contradict the standing carrier story";
that did NOT happen. The standing vwap_dev reversion carrier is real and powered at 126 days.
(The previous under-powered 3-day −0.0044 inside-canary read is now superseded.)

## B2 — H1-recheck (is the illiquid-skew a 1-day artifact?): **H1 STAYS KILLED — illiquid-skew HOLDS, in fact STRENGTHENS at depth**
Pre-reg: ratio > ~1.5 ⇒ kill holds (NOT a single-Monday artifact); ratio ≈ 1 ⇒ re-open H1.
Result: **illiquid/liquid |IC| ratio = 6.37× (H=15), 9.70× (H=30)** — far above 1.5, and far above
the single-Monday 2.06×/4.01×. The reversion is overwhelmingly an **illiquid-name** phenomenon:
- illiquid IC −0.111/−0.134, mid −0.052/−0.050, **liquid only −0.017/−0.014** (t=−5.8/−3.5, weak).
The single-Monday illiquid skew was NOT an artifact — it understated the real concentration.
**H1 does NOT re-open.** The reversion does not live in a tradeable liquid subset.

## B3 — economics (does any tier clear cost net?): **HOLDS where it matters — the tradeable (liquid) tier does NOT clear cost**
Pre-reg: NO tier clears its ~2bps cost net ("real but uneconomic at turnover").
Result, honestly read: the illiquid (+77/+124bps net) and mid (+10/+13bps net) tiers "clear" only
because of the **forward-filled-stale-close artifact** — in thin names the t+1 "entry close" is a
fictional non-traded price, so that book is untradeable (see method.md COST CAVEAT). The **liquid
tier is the only one priced on real trades, and it FAILS the 8bps/period cost: net −5.4bps (H=15),
−7.0bps (H=30).** So in the only economically meaningful tier, B3 holds: vwap_dev reversion is real
but does not clear cost. The apparent illiquid "edge" is an artifact, not an H1 re-open.
(Caveat acknowledged in pre-reg: 629 alive-today names are survivorship-tilted, and the cost model is
crude — this is a ballpark, not an edge claim.)

## Number the H2-RETEST must orthogonalize against
The trustworthy, powered vwap_dev baseline IC for the OFI lift measurement:
- **H=15: IC = −0.0581 (day-clustered t = −32.3)**  ← primary
- **H=30: IC = −0.0657 (day-clustered t = −27.6)**
Pooled over 629×126, ~47k/45k cross-sections, tradeable t+1→t+H entry, day-clustered.
The H2-RETEST should measure OFI's marginal lift as the IC improvement of (vwap_dev ⟂ OFI residual,
or vwap_dev+OFI combo) OVER this baseline, NOT over the under-powered 3-day −0.0044. To isolate a
tradeable lift, also condition on the **liquid tier** (baseline IC there is only −0.017/−0.014) —
that is where OFI would have to add value to matter economically.

## One-line
Baseline B1 confirmed (−0.058/−0.066, t≈−30); H1 stays dead and the illiquid-skew strengthens at
depth (6–10×), not an artifact; no tradeable (liquid) tier clears cost; H2-RETEST orthogonalizes
against IC = −0.0581 (H=15) / −0.0657 (H=30).
