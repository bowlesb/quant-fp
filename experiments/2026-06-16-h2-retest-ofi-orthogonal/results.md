# H2-RETEST — results (authoritative, vectorized rescore)

**Panel:** 1,568,418 sym-min rows · **250 liquid symbols** (incl. megacaps) · **20 completed days**
(2026-05-18 → 2026-06-15), RTH only, ≥15:50 ET excluded. Built from `/store/raw` quotes (true CKS OFI) +
trades (tick-rule signed vol) + bars (vwap_dev). Scored with `score_fast.py` (polars group_by Spearman
rank-IC per (date,minute) cross-section, day-clustered t over 20 days). Cross-check vs the explorer's
interactive standalone preview: consistent (all OFI |t| << 1).

## Standalone rank-IC (day-clustered t, n=20 days)

| signal | H15 IC | H15 t | H30 IC | H30 t | clears H15 canary? |
|---|---:|---:|---:|---:|:--:|
| ofi_15 | −0.0017 | −0.38 | +0.0013 | +0.21 | NO (in [−0.0024, +0.0020]) |
| ofi_30 | −0.0002 | −0.05 | +0.0010 | +0.17 | NO |
| ofi_15_norm | −0.0002 | −0.04 | +0.0032 | +0.49 | NO |
| ofi_30_norm | +0.0020 | +0.41 | +0.0037 | +0.55 | NO (= band edge) |
| sv_15 | −0.0021 | −0.42 | +0.0033 | +0.50 | NO |
| sv_30 | +0.0024 | +0.46 | +0.0014 | +0.21 | NO |
| sv_15_norm | −0.0012 | −0.28 | +0.0038 | +0.68 | NO |
| sv_30_norm | +0.0032 | +0.70 | +0.0018 | +0.33 | NO (≈ band edge) |
| **vwap_dev_15** | **−0.0233** | **−2.76** | −0.0028 | −0.25 | **YES** (only signal that clears) |
| vwap_dev_30 | −0.0102 | −1.00 | +0.0063 | +0.44 | borderline |

The 10-seed shuffle-canary 95% bands at H15 are ≈ ±0.002–0.003 for every signal. EVERY OFI/sv signal sits
INSIDE its canary band with |t| < 1. The ONLY signal clearing canary is `vwap_dev_15` (the known reversion,
t −2.76).

## Orthogonalized marginal IC over vwap_dev (the LOAD-BEARING test)

Residualize the forward return on vwap_dev cross-sectionally per (date,minute), then IC of the flow signal
on the residual:

| signal | resid@H15 IC | t | resid@H30 IC | t |
|---|---:|---:|---:|---:|
| ofi_15 | +0.0007 | +0.21 | +0.0002 | +0.03 |
| ofi_15_norm | +0.0036 | +0.87 | +0.0025 | +0.45 |
| ofi_30 | +0.0010 | +0.25 | +0.0005 | +0.10 |
| ofi_30_norm | +0.0043 | +0.89 | +0.0033 | +0.56 |
| sv_15 | +0.0030 | +0.84 | −0.0002 | −0.06 |
| sv_15_norm | +0.0042 | **+1.45** | −0.0001 | −0.02 |

Best marginal lift = `sv_15_norm` at H15, **t = +1.45** — below the pre-registered |t| ≥ 2.0 KEEP bar.
Every other cell |t| < 1. OFI adds NO significant IC orthogonal to vwap_dev in the liquid tier.

## Cost gate (decile L/S gross vs measured round-trip spread)

Measured median spread of the liquid set = **6.41 bps round-trip** (≈3.2 bps one-way).

| signal @ horizon | gross L/S | round-trip cost | clears? |
|---|---:|---:|:--:|
| ofi_15_norm @ H15 | +0.74 bps | 6.41 bps | NO |
| ofi_30_norm @ H30 | +0.86 bps | 6.41 bps | NO |
| ofi_15 @ H15 | +0.35 bps | 6.41 bps | NO |

Gross is ~8× below cost.

## Why the prior 3-day t≈3.96 did NOT replicate

The prior `ofi_15` t=3.96 was on ~80–100 names × 3 days. On 250 liquid names (incl. megacaps) × 20 days the
standalone IC collapses to ~0 (|t|<0.4). Consistent with: in the deepest, tightest-spread liquid names OFI
is efficiently arbitraged away; the prior result was small-sample / smaller-cap and did not survive the
powered, megacap-inclusive panel.
