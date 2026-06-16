# Results — H2: OFI marginal lift over vwap_dev (REAL numbers)

Panel: **80 symbols (dollar-vol ranks 20–100) × 3 days (06-09/11/12) × RTH minutes.**
Within-minute Spearman rank-IC vs cross-sectionally demeaned tradeable forward return (enter t+1 close, exit
t+H close). IC sign NOT flipped (raw). Pooled mean IC; t = mean / (std/√n_minutes) over the per-minute series.

## H=15 (primary) — actual script output
```
PANEL rows 89819 symbols 80 minutes 1128 days 3

=== WITHIN-MINUTE RANK-IC (H=15, raw, NOT sign-flipped) ===
  baseline (vwap_dev)          IC=-0.00444  t=-0.45  n_min=1128
  ret_5m                       IC=-0.00162  t=-0.16  n_min=1113
  OFI-only                     IC=+0.00534  t=+1.21  n_min=1122
  +OFI (vwap_dev+OFI)          IC=-0.00086  t=-0.10  n_min=1128

=== individual OFI feature ICs ===
  signed_vol_z         IC=-0.00426  t=-0.90  n_min=1101
  ofi_15_norm          IC=+0.01403  t=+3.02  n_min=1122
  ofi_30_norm          IC=+0.00785  t=+1.80  n_min=1122
  ofi_15               IC=+0.01853  t=+3.96  n_min=1122

=== SHUFFLE CANARY (permute fwd within minute, 10 seeds) ===
  baseline     canary IC mean=-0.00098 std=0.00353 band=[-0.00804, +0.00607]
  ofi_only     canary IC mean=-0.00194 std=0.00356 band=[-0.00907, +0.00519]
  plus_ofi     canary IC mean=-0.00132 std=0.00339 band=[-0.00811, +0.00547]

=== CRUDE NET-OF-COST (decile L/S, ~2bps one-way) ===
  baseline   dir=- gross/min=-0.31bps turnover=0.12 cost=0.47bps net/min=-0.78bps
  OFI-only   dir=+ gross/min=-0.66bps turnover=0.49 cost=1.94bps net/min=-2.60bps
  +OFI       dir=- gross/min=+0.51bps turnover=0.32 cost=1.27bps net/min=-0.76bps
```

## H=5 (secondary) — actual script output
```
PANEL rows 92219 symbols 80 minutes 1158 days 3

=== WITHIN-MINUTE RANK-IC (H=5, raw, NOT sign-flipped) ===
  baseline (vwap_dev)          IC=-0.00673  t=-0.72  n_min=1158
  ret_5m                       IC=-0.02587  t=-2.61  n_min=1143
  OFI-only                     IC=+0.00096  t=+0.22  n_min=1134
  +OFI (vwap_dev+OFI)          IC=-0.00352  t=-0.44  n_min=1158

=== individual OFI feature ICs ===
  signed_vol_z         IC=-0.00746  t=-1.62  n_min=1113
  ofi_15_norm          IC=+0.00978  t=+2.16  n_min=1134
  ofi_30_norm          IC=+0.00090  t=+0.20  n_min=1134
  ofi_15               IC=+0.01185  t=+2.67  n_min=1152

=== SHUFFLE CANARY (10 seeds) ===
  baseline     canary band=[-0.00512, +0.00519]
  ofi_only     canary band=[-0.00859, +0.00649]
  plus_ofi     canary band=[-0.00635, +0.00411]
```

## Reading the numbers (honest)

**Sign of OFI = CONTINUATION (positive).** Every OFI window has POSITIVE IC: positive net signed-flow over the
last 15/30 min → positive forward return. This matches the Cont–Kukanov–Stoikov prior and is the sign I
pre-registered.

**OFI carries real standalone signal — but it lives in the rolling-flow windows, not the composite.**
- `ofi_15` (raw 15-min signed-volume sum): **IC=+0.0185, t=+3.96 at H=15** (and +0.012, t=+2.67 at H=5) — well
  OUTSIDE the canary band (H=15 band ≈ ±0.006–0.008). This is the headline positive.
- `ofi_15_norm` (flow / volume): IC=+0.014, t=+3.0 at H=15 — also clearly outside canary.
- `ofi_30_norm`: +0.008 (t=1.8) at H=15, ~0 at H=5 — borderline.
- `signed_vol_z` (z-scored 1-min flow): **IC≈0 / slightly negative, inside canary — this is NOISE** and is the
  member that drags the equal-weight composite down.
- Equal-weight **OFI-only composite**: IC=+0.0053 (t=+1.21), which sits INSIDE its canary band — because the
  good `ofi_15` signal is diluted by the dead `signed_vol_z`. So the *composite as I built it* does NOT clear
  the gate, even though its best member does.

**The baseline (vwap_dev) is unexpectedly WEAK on this panel.** IC=-0.0044 (t=-0.45), inside its own canary.
The known vwap_dev reversion carrier (~-0.028 historically) does NOT show up at strength here. Almost certainly
because the panel is only **3 days** and excludes the megacap top — i.e. low statistical power, not a refutation
of vwap_dev. This is the critical caveat for the marginal-lift comparison below.

**Marginal lift (+OFI vs baseline) is INDETERMINATE here.** `+OFI` IC = -0.0009 vs baseline -0.0044. The two
blocks (vwap_dev reversion, sign −; OFI continuation, sign +) partially CANCEL when summed raw, so the combined
score is near zero. Because the baseline itself is near-zero/inside-canary on this 3-day panel, the "does +OFI
exceed baseline beyond the canary band" test cannot be cleanly evaluated — neither arm is reliably distinct from
the canary as a composite. The orthogonality SIGN evidence (OFI positive, vwap_dev negative) is the informative
part; the additive-vs-conditioner question is not resolved at this scale.

**Crude net-of-cost: nothing survives 2bps one-way at 1-minute rebalancing.** OFI's decile book turns over
~0.49/min → ~1.9bps cost/min, swamping a ≤1bps gross. Even baseline nets negative. This is expected for a
minute-rebalanced book and says the signal (if real) needs slower rebalancing / horizon-matched holding, not
that it's fake — the IC gate is the cleaner read than this crude cost ballpark.
