# Research pitfalls — methodology notes for explorers (read before writing a panel script)

Append-only. Each entry is a concrete bug that produced (or nearly produced) a false result, and the rule
that prevents it. Single-writer = the MA.

## 1. UTC-vs-ET off-by-240 — the PARITY-INVISIBLE timezone trap (2026-06-16, H11)

**What happened.** An H11 explorer built session-time logic with `utc_minute = ts.hour()*60 + ts.minute()`
and constants `09:30 ET = minute 570`. But `/store/raw/bars` `ts` is **genuine UTC** (13:30 UTC = 09:30 ET).
So 09:30 ET is really minute **810**, not 570 — every constant was off by +240. The entry grid landed on the
09:30 OPEN PRINT, and a `>=09:35` "tradeable-entry" gate (575) became a NO-OP (every real bar is >=810). The
result was a +28 bps "momentum edge" that was actually an open-anchored artifact. Caught only because the
gate's output was identical to the ungated output (the tell), and a hand re-derivation of the entry minute
exposed the off-by-240.

**Why it is dangerous.** This bug class is PARITY-INVISIBLE: if it crept into a PRODUCTION session-time
feature, live and backfill would both be wrong the SAME way and MATCH each other, so the
compute==compute_latest parity gate would never catch it. (It would still be caught by golden-set validation
of the calendar features, and by the no-look-ahead test for the entry-anchoring half — but the raw
time-of-day error itself is silent to parity.)

**The rule.**
- NEVER compute ET session minutes by reading `.hour()`/`.minute()` off a UTC timestamp. ALWAYS convert
  first: `ts.dt.convert_time_zone("America/New_York")` (DST-aware — June is EDT = UTC−4, but don't hardcode
  the offset; let the tz database handle DST). This is what production `quantlib/features/groups/calendar.py`
  does correctly (and it even comments the Int8 `hour()*60` overflow trap — cast to Int32 first).
- VERIFY your RTH filter against real bars before trusting it: print a few `ts` and the derived session
  minute; confirm 13:30 UTC maps to "market open" and 20:00 UTC to "close".
- A tradeable-entry gate that does not CHANGE the result is a no-op bug until proven otherwise. An entry gate
  is supposed to exclude the open print; if raw == gated, the gate didn't fire — investigate before trusting.

## 2. The tradeable-entry trap (standing, pre-2026-06-16)

A return must be booked from a TRADEABLE entry price (≥09:35 ET, never the 09:30 print) and cost must be the
MEASURED open spread — not a flat charge on a 09:30 return. Open-anchored labels / 09:30-print fills are the
platform's #1 false-edge source (killed the gap-fade, open-cadence gap, open-anchored momentum). Pitfall #1
above is a NEW way to accidentally re-introduce this (a broken time grid silently re-anchors to the open).

## 3. Survivorship / per-symbol-demean (standing)

Any cross-sectional "edge" must survive a per-symbol demean (subtract each symbol's own mean forward return).
The overnight "edge" was survivorship and collapsed under demean. Run the demean gate on every cohort/L-S
result; an edge that vanishes under demean is an idiosyncratic/survivorship artifact, not alpha.

## 4. The POST-HOC hold-out rule — NEVER promote an in-sample number you peeked at (standing, 2026-06-16, H11→H12)

**What happened.** H11's pre-registered full-session momentum test was marginal (t=1.51). Its robustness
check then found a mid-session restriction (10:00–15:30 ET, W60/H120) that jumped to t=3.27 — tempting to
declare a discovery. But that restriction was chosen AFTER seeing the data, on only 2–3 entry slots:
"exclude data → signal improves" is a textbook overfit/multiple-testing smell.

**The rule (apply to EVERY post-hoc-flavored result — any signal that emerged from slicing, excluding, or
parameter-searching AFTER looking at the data).**
- Do NOT promote the in-sample number. A t-stat on data you've already peeked at is worth ~nothing — you
  can always find a slice that looks good.
- Instead, PRE-REGISTER a hold-out test: designate days/symbols NEVER seen when the idea was formed (e.g. an
  earlier time block, or a disjoint symbol set), commit a hard replication bar (e.g. t≥2 AND positive
  net-of-cost after the realistic turnover/no-trade band), and a LOW prior (post-hoc findings replicate OOS
  at a low base rate — pre-commit to that low prior so you don't talk yourself into it).
- KEEP only if the hold-out replicates the bar. If it doesn't, it was a discovery-set overfit — kill it.
  Worked example: `2026-06-16-h12-midsession-momentum/hypothesis.md`.
- This is the single discipline that stops a research platform from fooling itself. An in-sample t=3.27
  you've already seen is worth nothing; a hold-out that replicates is worth everything.

## 5. The CLUSTERING-UNIT trap — a day-clustered t must cluster by DAY, not by (symbol,date) cell (2026-06-16, HF02)

**What happened.** HF02 reported an OOS demeaned t=9.41 (an obvious red flag — HF01 was t=1.62 on the same
signal class). The IC was unchanged (~0.10); the t-stat was inflated ~3.5×. Cause: the metric helper computed
one rank-IC per **(symbol, date) CELL** and then fed ALL cells into the day-clustered t-stat as if each were
an independent observation — so n = n_symbols × n_days ≈ 204 instead of n_days ≈ 17. The t-stat denominator is
std/√n, so over-counting n by ~n_symbols inflates t by ~√(n_symbols).

**Why it's insidious.** The IC, the canary, and the cost-gate net are all UNCHANGED — only the significance is
wrong. A plausible IC + a (fake) high t reads as a strong KEEP. Cross-sectional cells on the SAME day are
correlated (a market-wide move hits every symbol), so they are NOT independent — treating them as independent
is the error.

**The rule.**
- The clustering unit for a day-clustered t-stat is the **DAY** (one independent observation per trading day).
  Compute the IC per cell if you must (for null-safety), then **AVERAGE the cells within each day to ONE IC per
  day**, and take the t over the ~n_days day-ICs. Report `n_days`, and SANITY-CHECK it against the calendar —
  if your "n" is ~n_symbols×n_days, you are over-counting.
- A t-stat far larger than a sibling test on the same signal class (here 9.41 vs 1.62) is a RED FLAG — audit
  the n before believing it.
- Separately: a low-turnover strategy's net-of-cost rests on FEW actual trades. The IC t-stat does NOT
  validate the net — compute a per-TRADE significance / bootstrap on the realized round-trip P&L and report
  the trade count. +1 bps over a few dozen trades can be noise.

## 6. OVERLAPPING-RETURN / IC-weighted "net" is NOT the tradeable P&L — bootstrap NON-OVERLAPPING trades (2026-06-16, HF02)

**What happened.** HF02 reported a "+1.17 bps net-of-cost" KEEP. It was an artifact. The `net_bps` was a
SIGNAL-WEIGHTED average over every 10s bucket, where each bucket's "return" was the OVERLAPPING h-minute
forward return — so a single 30-min move was counted ~180× (once per 10s bucket inside it), and the spread
was NOT honestly charged on each realized round-trip. The honest per-TRADE bootstrap (non-overlapping
realized entry→exit round-trips, measured spread crossed) showed the headline cell nets −0.20 bps/trade
(CI [−3.16, +2.79] straddles 0); ZERO of 80 cells had a per-trade CI above zero.

**The rule.**
- An IC, or a per-bucket/per-period "average net", with OVERLAPPING forward returns is NOT a tradeable P&L —
  it double-counts the same price move and can hide the per-trade friction entirely.
- The tradeable number is the realized P&L of NON-OVERLAPPING round-trips: enter, hold, exit, book
  (gross move) − (full spread crossed each side) − (impact). Collect those per-trade returns and BOOTSTRAP
  the mean (10k resamples, 95% CI). KEEP only if the CI excludes zero ABOVE — at the measured cost and a 2×
  stress.
- A low-turnover strategy's "net" is especially treacherous here: a tiny per-bucket edge × a huge bucket
  count looks large, but the actual trade count is small and each trade pays the real spread. Always report
  the realized TRADE COUNT and the per-trade CI, never just the aggregate net.

## 7. Maker / spread-capture backtests — fill = trade-THROUGH + post-fill MARK-OUT (2026-06-16, HF03 pre-reg)

The two ways a liquidity-provision backtest prints fake profit:
- **Touched ≠ filled (queue position).** A passive limit at the touch is NOT filled because the market
  touched your price — orders ahead of you fill first. Require the market to trade STRICTLY THROUGH your
  level (+ a queue-depth proxy: cumulative print size > prevailing same-side size at post time). Even then
  it's an upper bound on fills. "Touched → filled → earned half-spread" is the artifact.
- **Adverse selection → POST-FILL MARK-OUT is the honest edge.** You're filled preferentially when wrong.
  After each fill, mark at the mid at +1/5/30/60s; true edge = earned spread − mark-out loss. The headline is
  the mark-out-net-per-fill bootstrap CI (must exclude zero above). A naive "+half-spread per fill" with no
  mark-out is the artifact. And state plainly it's a MODELED backtest with conservative assumptions, not a
  live-fill measurement.

## 8. Mark-out / earned-spread must be anchored at the ACTUAL FILL timestamp + the mid AT fill (2026-06-16, HF03 replication)

**What happened.** An independent HF03 replication caught a subtle anchoring bias in the first pass: the
earned half-spread (and the mark-out) was measured against the RESTING-WINDOW edge referencing the STALE
POST-TIME mid (the mid when the passive order was POSTED), not the mid at the moment it actually FILLED.
Because you tend to fill after the mid has already moved toward you, anchoring at post-time inflated the
"earned spread" to ~1.05 bps; anchoring at the FILL instant gives ~0.87 bps — uniformly ~20% more
conservative. (The KILL was unchanged either way, but the honest number is the lower one.)

**The rule (maker/passive-fill backtests).**
- The earned spread = (mid AT the fill timestamp) − fill_price (for a bought bid) — referenced to the mid at
  the ACTUAL fill instant, NOT the mid when you posted and NOT a window edge. The price moved between post and
  fill; using the post-time mid books a move you didn't earn.
- The mark-out horizons (+1/5/30/60s) start from the FILL timestamp, not the post timestamp.
- General principle (applies beyond makers): anchor every realized-P&L reference to the ACTUAL execution
  timestamp and the price AT that instant — any stale/earlier reference silently books price moves you did
  not capture and inflates the edge.

## 9. Forward-return ML — label-horizon EMBARGO + bounce-immune entry; a non-zero shuffle canary = leakage (2026-06-16, W7)

Training a model to predict an H-day forward return has two leakage traps a label-shuffle canary alone does
NOT catch:
- **Overlapping-label leakage.** If train and predict windows are adjacent, the H-day labels OVERLAP across
  the boundary — the model sees (part of) the test labels in training. MANDATORY: a full-LABEL-HORIZON
  EMBARGO (gap ≥ H) between the last train date and the first predict date. W7: the embargo cut the apparent
  edge ~50–60% (pre-embargo +884 bps → +465; an early run was +1534 bps).
- **Entry-price bid-ask-bounce look-ahead.** If features are computed as-of close[t] and entry is also
  booked at close[t], the bounce in close[t] is in both → fake edge. Use a BOUNCE-IMMUNE entry: features
  as-of close[t], ENTER at close[t+1]. The label-shuffle canary does NOT catch this (it permutes the label,
  not the entry timing).
- **A non-zero shuffled-label canary is itself a red flag.** Shuffling the label should give OOS net ≈ 0; if
  it's significantly non-zero (W7 liquid500-H10 canary −148 bps, CI excludes 0), the bootstrap carries
  small-sample bias / residual leakage and the headline CI is UNTRUSTWORTHY — treat as KILL, not KEEP.
- And the megacap check: a real cross-sectional ML edge is STRONGEST in the cleanest (megacap) universe; if
  it only appears in the broad universe and vanishes/flips in megacaps, it's broad-universe overfit.
