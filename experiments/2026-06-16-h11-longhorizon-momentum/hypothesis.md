# H11 — Longer-horizon intraday MOMENTUM (the tradeable sign-flip H9 uncovered) — pre-registration

**Registered:** 2026-06-16 (before any run). Motivated DIRECTLY by an unexpected H9 finding, not a re-run.
This is NOT "another reversion variant" (the pivot rule forbids that) — it is the OPPOSITE signal that H9
incidentally revealed, and it is data-available NOW (reuses the H9 bars panel; no EDGAR/corp-action ask).

## The observation that motivates it

H9 tested the vwap_dev REVERSION decile L/S (long most-below-VWAP, short most-above) at H60/H120 and found it
**strongly NEGATIVE gross** (−12.7 to −33.7 bps, t −1.75 to −2.58). A negative reversion L/S means the
SIGN-FLIPPED book — long most-ABOVE-VWAP, short most-below — is strongly POSITIVE gross at the same horizons.
I.e. at 60–120 min intraday, vwap_dev is a MOMENTUM/continuation signal, not reversion: names extended above
their VWAP keep rising, names below keep falling. H9 did NOT evaluate that momentum direction's net-of-cost
or its turnover-managed form — it only reported the reversion leg as a kill. H11 tests whether the momentum
direction is TRADEABLE.

## Hypothesis

The momentum decile L/S — **long top-vwap_dev decile, short bottom** — held 60–120 min has GROSS ≈ +12 to
+34 bps (the sign-flip of H9), which COMFORTABLY exceeds the ~6 bps round-trip cost IF turnover can be
contained. The open question H9 left: turnover was ~0.90 (decile reshuffles fully each rebalance), which at
+gross is FINE (you WANT to ride continuation) but each rebalance still pays cost. Net = gross −
turnover×cost. At +12 bps gross / 0.90 turnover / 6 bps cost → net ≈ +12 − 5.4 = +6.6 bps (H30/H60 cell);
the H120 cells are +28 to +34 gross so net is clearly positive even at high turnover.

## Test design (reuse H9's panel + infra; flip the sign + add the gates H9 skipped)

Same 300 liquid × 50 days bars panel. For each (W ∈ {30,60} × H ∈ {60,120}):
1. Decile L/S **long top-vwap_dev, short bottom** (momentum direction). GROSS bps (= −1 × H9's number).
2. Net = gross − turnover×RT_cost at 4/6/10 bps (turnover already measured ~0.90).
3. **The gates H9 did NOT apply to the momentum direction:** (a) the tradeable-entry rule — enter at ≥09:35,
   never the 09:30 print (the platform's hardest-won lesson); re-book the L/S from a tradeable entry and
   re-measure. (b) 10-seed within-CS shuffle canary on the MOMENTUM leg. (c) day-clustered t. (d) a
   no-trade band / hysteresis to test whether turnover can be cut without killing the +gross (the standard
   momentum turnover lever).
4. Robustness: does the +gross survive excluding the first/last 30 min of the session (open/close
   microstructure)? Does it survive a per-symbol-demean (the survivorship/idiosyncratic check)?

## Prior

Intraday momentum/continuation at the 30–120 min horizon is documented (Moskowitz time-series momentum;
intraday underreaction to order-flow). The platform's PRIOR verdict "momentum is dead at 30m" was for the
CROSS-SECTIONAL ret_5m/momentum features at 30 min — NOT for vwap_dev-extension continuation at 60–120 min,
which H9 just showed is the dominant effect at that horizon. So this is genuinely untested in our stack.

## Expected / confidence

- Confidence a (W×H) momentum cell clears net-of-cost beyond canary AND survives the tradeable-entry +
  per-symbol-demean gates: **~35%** — higher than any reversion variant because the GROSS is already known
  to be large (+12 to +34 bps) and positive; the risk is entirely whether it survives realistic entry +
  the survivorship demean (the two things that have killed apparent edges before).
- KEEP: a cell net > 0 beyond canary, survives tradeable-entry AND per-symbol-demean, robust to cost stress.
- AMBIGUOUS: positive gross but collapses to ≈0 under demean or tradeable-entry.
- KILL: every cell net ≤ canary after the gates (the +gross was a look-ahead / open-print / survivorship
  artifact — exactly the traps that killed the gap-fade and overnight).

## Why this respects the pivot rule

The pivot says "no further price-REVERSION variant." This is the MOMENTUM direction — the opposite signal,
and the one the data is actively pointing at. It is the single highest-EV data-AVAILABLE thread while the
event families (H10/H5/H4) wait on the EDGAR/corp-action backfill. If H11 also dies to the tradeable-entry
or demean gate, THEN the price branch is exhaustively closed and the hunt is fully on the (blocked-on-data)
event families.

## Ordering

Dispatch NOW (data-available, highest-EV non-blocked thread). The event families remain pre-registered and
gated on the data ask already routed to the Lead.
