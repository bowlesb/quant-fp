# Representation-Learning Lane — Insights (honest analysis)

> Two substrates: (A) REAL certify300 daily bars — structure claims here are honest. (B) synthetic feature
> panel (planted structure) — results prove the HARNESS recovers structure, NOT that it exists in markets.

## A. Stock embeddings on REAL daily bars (D4) — REAL STRUCTURE, validated OOS

**Setup:** 2,722 symbols with >= 250 trading days, 377 days total (18mo). Per-symbol standardized
daily-return matrix; truncated SVD -> 16-d behavioral embedding; KMeans(11) for peer groups. Embedding fit
on the first 263 days (TRAIN); co-movement-recovery evaluated on the last 114 days (HELD-OUT).

**Finding 1 — a dominant market factor + a thin tail of style factors.**
Top SVD component explains **21.2%** of cross-sectional return variance (the market). Components 2-8 fall off
fast: 4.5%, 2.5%, 2.0%, 1.7%, 1.2%, 1.1%, 1.0%. So ~1 strong factor + ~7 weak style/sector factors carry the
co-movement; the rest is idiosyncratic. Consistent with the scoreboard ("simple liquid factors mostly
arbitraged-away"): the LINEAR co-movement spectrum is thin. Any edge in non-linear structure must live
beyond these top components.

**Finding 2 — discovered peer groups are ECONOMICALLY COHERENT (unsupervised, no sector labels used).**
Nearest behavioral neighbours (cosine in embedding space):
- JPM -> GS, SCHW, MS, C, RJF, WFC  (banks / brokers)
- XOM -> CVX, OXY, IMO, MGY, KRP, COP  (oil & gas)
- NVDA -> TSM, AVGO, CRDO, VRT, ETN, ORCL  (semis + AI-datacenter power/infra — a *thematic* cluster,
  not a GICS sector: VRT/ETN are electrical, grouped by behavior not classification)
- PG -> KMB, CHD, CL, CLX, KO, XLP  (consumer staples + the staples ETF itself)
- JNJ -> KMB, MDLZ, PG, PEP, CL, KHC  (staples-like defensives)
The NVDA cluster is the interesting one: behavior groups NVDA with power/infra names (VRT, ETN) that GICS
puts in different sectors — a **data-driven theme GICS misses**. This is the kind of structure the lane is
for.

**Finding 3 — the structure PERSISTS out-of-sample (the load-bearing rigor result).**
Within-cluster minus across-cluster return correlation:
- TRAIN window: **0.092**
- HELD-OUT 114-day window: **0.092**  (essentially unchanged)
- RANDOM-label baseline on held-out: **0.0003**
The peer structure is ~**300x** the random baseline and does not decay out-of-sample. `structure_is_real_oos
= true`. Behavioral peer groups are a REAL, persistent property of the data — not a period-specific artifact.
(Silhouette 0.149 is modest in absolute terms because cluster boundaries are soft — the *cohesion-vs-random*
ratio is the honest metric, and it is decisive.)

**Honest caveats:** SVD co-movement embedding ~ PCA — this is LINEAR structure (it's the floor, not the
ceiling, of what embeddings can find). The value here is (a) the embedding is real-data-validated and
parity-trivial, (b) it yields the most ship-ready feature candidate (peer-relative return, C1), and (c) it
sets the linear baseline the deep models (VAE/contrastive) must BEAT to justify their complexity.

## B. Feature-panel VAE (D1, synthetic) — an HONEST negative + a clean baseline verdict

**Setup:** beta-VAE (encoder 519->256->128->z=16, symmetric decoder, GELU/BatchNorm, KL warmup), trained
80.4s on the RTX 3090 (CUDA confirmed), 616k train rows / 160k held-out-symbol / 160k held-out-time
(purged + 60-min embargo). Pre-registered bar: beat PCA at equal latent dim, generalize to held-out
symbols+time, no posterior collapse.

**Result — the beta-VAE COLLAPSED, and PCA wins. All 6 pre-registered checks FAIL (as designed to catch).**
| metric | beta-VAE z=16 | PCA k=16 |
|---|---|---|
| recon R2 train | -0.000 | — |
| recon R2 held-out symbol | -0.000 | **0.031** |
| recon R2 held-out time | -0.000 | **0.031** |
| active latent dims (KL>0.01) | **0 / 16** | n/a |
| final recon loss | 1.0003 (= total variance) | — |

The KL term drove every latent dim to zero (posterior collapse); the decoder fell back to predicting column
means -> recon R2 ~ 0. This is the **textbook collapse failure the pre-registration exists to catch** — and
the harness caught it honestly rather than reporting a flattering train number.

**Why this is the RIGHT outcome, not a bug:**
1. The synthetic panel is BY CONSTRUCTION ~510 independent-noise features + 5 signal features. There is almost
   no low-rank reconstructable structure to compress — **even PCA recovers only 3.1% of variance.** A faithful
   model SHOULD find little. A VAE reporting high recon R2 here would be the alarming result.
2. Per the pre-registered rule "if PCA matches the deep model, ship PCA": on this substrate the **linear
   baseline is the ceiling**, so a latent-derived feature (C3 recon-error / C4 latent dims) is NOT justified
   on synthetic data. We do NOT ship a D1 feature off this run.
**Follow-up — plain AUTOENCODER (beta=0, no KL pressure) PASSES the deep-vs-linear test:**
| metric | AE beta=0 z=16 | beta-VAE z=16 | PCA k=16 |
|---|---|---|---|
| recon loss (1=total var) | **0.942** | 1.000 | ~0.969 |
| held-out-symbol beats PCA | **YES** | no | — |
| held-out-time beats PCA | **YES** | no | — |
| active latent dims | **16 / 16** | 0 / 16 | n/a |
| checks 1-3 pass | **YES** | no | — |

Removing the KL term fixed the collapse: the plain AE generalizes to **held-out symbols AND held-out time**
and **beats PCA on both** (lower recon loss => recovers more than PCA's 3.1%). So the rigor gate works in
BOTH directions — it rejected the collapsed beta-VAE and passed the genuine nonlinear AE. (Only the
sector-structure check is False: the synth panel carries no sector labels, so that check is inapplicable
here, not a failure of the model.) Artifacts: `out/vae_z16_result.json` (AE beta=0),
`out/vae_z16_beta0.5_result.json` (collapsed VAE), `out/vae_encoder_z16.npz` (encoder weights + standardizer).

**Takeaway for the lane:** the harness + rigor gate are PROVEN (reject collapse, reject sub-PCA, pass a
generalizing nonlinear AE). On synthetic data the nonlinear AE's edge over PCA is real but small (the panel
is mostly noise by construction), so we do NOT ship a D1 feature off synthetic data — but the AE, not the
VAE, is the architecture to carry to real vectors. **The headline REAL-data win this burst is D4 (Section A).**
When the vector backfill lands, re-run the AE on real feature vectors; only then can recon-error (C3) / latent
dims (C4) clear the feature bar.

## What this means for the findings->features loop

1. **C1 (peer-relative return)** is the most ship-ready candidate: real-data-validated, parity-trivial
   (nightly static lookup + cheap intraday reduce), and it encodes a relationship (behavioral relative
   strength) that simple per-symbol factors don't. -> promote to a FeatureGroup PR for the coordinator's
   parity/canary/trust pipeline.
2. The thin linear factor spectrum (1 strong + ~7 weak) tells us the **linear embedding is mostly market +
   sector**. For the lane's core hypothesis (edge in non-linear structure) to pay, the VAE / contrastive /
   sequence models must find structure BEYOND these components AND beat the PCA baseline on the
   pre-registered bar. The VAE result (Section B) is the first test of that.
</content>
