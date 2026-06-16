# W4 — Results

Universe: `liquid500` (500 names, 61,992 name-days) and `megacap100` (100 names, 12,396 name-days),
126 days. Returns in **bps**. Full machine output: `results.json`.

## 1. Descriptive — mean overnight vs intraday

| universe | pooled overnight | pooled intraday | xsec on mean/med | on %pos | xsec id mean/med | id %pos |
|---|---|---|---|---|---|---|
| liquid500 | **+9.30** | +4.86 | 9.30 / 3.07 | 0.59 | 4.85 / 4.86 | 0.64 |
| megacap100 | **+16.88** | +10.76 | 16.88 / 6.11 | 0.66 | 10.75 / 9.99 | 0.69 |

Raw picture matches the literature/cycle-0: overnight mean is positive and larger than intraday, and a
majority of names have positive mean overnight. **This is the seductive raw level.**

## 2. Per-symbol demean — THE LOAD-BEARING TEST

Day-clustered (each day = one obs, n=124 days). Raw level significance, then per-symbol-demeaned residual.

| universe | comp | raw daily mean | raw day-clustered **t** | demeaned mean | demeaned **t** |
|---|---|---|---|---|---|
| liquid500 | overnight | +9.31 | **1.40** | +0.0009 | 0.000 |
| liquid500 | intraday | +4.85 | 0.59 | −0.0032 | −0.000 |
| megacap100 | overnight | +16.90 | **1.78** | +0.020 | 0.002 |
| megacap100 | intraday | +10.77 | 0.92 | +0.007 | 0.001 |

**The raw overnight level is not even day-clustered-significant** (t=1.40 liquid500, t=1.78 megacap — both
< 2), and it **collapses to ~0 (t≈0) under per-symbol demean.** Intraday is weaker still. The entire
"overnight is positive" effect is a per-name LEVEL — survivorship/composition, exactly cycle-0's failure.
Nothing signed survives removing the name's own mean.

## 3. Cross-sectional L/S (decile, equal-weight, non-overlapping) — net @ measured cost + OOS

`liquid500` (avg round-trip cost ≈ 14 bps; 123 rebalances; OOS = 2nd-half dates):

| form | gross | net@meas [boot 95% CI] | CI≠0 | OOS net | OOS CI≠0 | canary | cost |
|---|---|---|---|---|---|---|---|
| overnight_momentum | +7.36 | −6.54 [−38.9, +25.5] | no | −14.37 | no | +5.4 | 13.9 |
| overnight_reversal | −7.36 | −21.26 [−52.8, +11.4] | no | −13.54 | no | −3.4 | 13.9 |
| intraday_momentum | −3.60 | −18.42 [−48.0, +10.6] | no | −32.99 | no | +3.0 | 14.8 |
| intraday_reversal | +3.60 | −11.21 [−40.7, +18.5] | no | **+3.33** | **no** [−42,+49] | +4.6 | 14.8 |

`megacap100` (cost ≈ 16–18 bps): overnight_momentum gross +22.2 but net +5.6 with CI [−31.8,+44.2]
(straddles 0); OOS net +12.97, CI [−48,+75] (straddles 0). The only CI that EXCLUDES zero anywhere is
megacap `overnight_reversal` net = −38.8 [−76.9, −0.2] — i.e. significantly **negative** (losing).

**No L/S form, in either universe, has a positive net-of-cost bootstrap CI that excludes zero — full-sample
OR OOS.** Best gross spreads (overnight momentum +7 to +22 bps) are entirely eaten by the ~14–18 bps
round-trip cost. Canaries are all small (±2–5 bps) → no signal-leakage; the gross spreads that exist are
real but tiny and sub-cost.

## 4. Tradeable entry / cost
Even at the GENEROUS measured cost (range proxy, optimistic vs true bid-ask + MOC/MOO auction slippage which
we do NOT add), every net mean is ≤ ~+5.6 bps with CIs spanning ±40 bps. At 2× cost stress every form is
solidly negative (e.g. liquid500 overnight_momentum net_2x ≈ −20 bps; megacap overnight_momentum −11 bps).

## 5. Gates summary
- Per-symbol demean (PRIMARY): **FAIL** — overnight & intraday levels collapse to t≈0.
- Day-clustered raw significance: FAIL (overnight t=1.4–1.8 < 2; intraday t<1).
- Bootstrap CI > 0 on net: **none** (the only CI excluding 0 is negative).
- OOS net > 0 with CI>0: **none**.
- Cost gate (measured, ×2): FAIL everywhere.
- Canary: PASS (≈0) — confirms no leakage; just no edge to leak.
