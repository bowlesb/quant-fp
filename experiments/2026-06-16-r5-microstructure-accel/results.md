# R5 microstructure acceleration → forward returns — RESULTS

Study: `study.py` (~3,300 symbols kept of 3,836 sampled, raw trades → per-minute n_trades, 378d).
trade_accel = (n_trades last 5m)/(n_trades prior 5m) − 1; forward returns booked from the NEXT
minute's close (tradeable entry). Rank-IC + decile spread + shuffle canary, two tiers separately.

## Results
| tier | horizon | rank-IC | canary | top-bottom-decile spread |
|---|---|---|---|---|
| LIQUID (600k obs) | fwd_5m | +0.0024 | +0.0005 | +0.012% |
| | fwd_30m | +0.0023 | −0.0014 | +0.019% |
| | fwd_1d | +0.0024 | −0.0007 | +0.014% |
| SPECULATIVE (398k obs) | fwd_5m | +0.0002 | +0.0021 | **−0.047%** |
| | fwd_30m | +0.0015 | −0.0016 | −0.030% |
| | fwd_1d | +0.0022 | −0.0028 | **−0.092%** |

## Interpretation — STRATEGY KILL (both tiers)
- **LIQUID:** rank-IC ~+0.0024 at all horizons. With n=600k it is barely distinguishable from the
  canary band, and ECONOMICALLY NEGLIGIBLE: decile spreads +0.012% to +0.019% = **~1–2 bps gross**,
  far below any realistic cost. A statistically-detectable, economically-dead signal (the same shape
  as the killed vwap_dev baseline — real but uneconomic).
- **SPECULATIVE:** INCOHERENT — the rank-IC is ~0 to +0.0022 but the decile spreads are NEGATIVE
  (−0.047% to −0.092%), and the fwd_5m IC (+0.0002) is BELOW its canary (+0.0021). The IC and the
  decile spread disagree in sign → this is noise, not a signal. (Notably the speculative tier does NOT
  show a stronger effect — the runner/dumper-style tier asymmetry does NOT appear for acceleration.)
- Trade-frequency ACCELERATION does not predict forward returns at 5m / 30m / multi-day in either
  tier. The directional content is absent.

## FEATURE decision — NO feature (honest)
Per the mandate a feature has the lower bar (real + parity-true + non-redundant + NOT pure noise), and
a killed strategy can still yield a feature. But here I do NOT ship trade_accel:
- The return-predictive content is absent (IC economically zero / sign-incoherent). The only argument
  for the feature would be "it's a real microstructure STATE a model might use as a conditioner" — but
  trade_accel is largely a transform of the trade-rate dynamics that ``trade_freq_z`` (the LEVEL
  z-score, F4, already live) already exposes; a model gets the rate-of-change implicitly from the
  z-score across its windows. Adding a near-redundant variable with zero standalone signal is feature
  bloat, not value — exactly what dropping C2 and the forced "unified" feature taught.
- The bar is "non-redundant AND not-noise-FOR-A-MODEL." trade_accel fails non-redundancy-in-practice
  (overlaps trade_freq_z) without compensating signal. Honest NO.

This is a clean negative result: it CLOSES the microstructure-acceleration branch (the rate-of-change
of trade frequency is not a tradeable edge and not a value-adding feature beyond the existing level
z-score), and it confirms the tier-asymmetry seen for PRICE extremes (runner/dumper) does NOT extend
to trade-frequency acceleration. One less thing to re-tread.

## What WOULD be worth a follow-up (NOT now, logged)
The study used UNSIGNED trade-count acceleration. A SIGNED variant (acceleration of BUY vs SELL trade
imbalance, using the tick rule / the signed_volume already in minute_agg) is a DIFFERENT quantity —
but H2 OFI/signed-flow was already powered-KILLED (|t|<1, cost gate ~8x), so the prior is low. Not
pursuing without a new reason.
