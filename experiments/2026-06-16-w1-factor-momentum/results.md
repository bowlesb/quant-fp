# W1 — Results

All numbers are **per-rebalance PORTFOLIO returns** (multi-week holding periods), NOT per-day or IC.
`net1` = net @ measured spread (median 7.34 bps round-trip); `net2` = 2× stress.
Each rebalance is one independent (non-overlapping) observation. Full table: `results.csv` (64 cells).

## Headline: cost is NOT the binding constraint — the per-symbol DEMEAN control is.

- **Gross positive in 62/64 cells.** Cost is small (turnover 0.22–0.77 × ~7 bps ≈ a few bps/rebalance),
  so `net1 ≈ gross` and even `net2` stays positive almost everywhere. The friction wall barely bites —
  the friction-wall-favorable shape (diversified, low-turnover, liquid) DID keep cost negligible.
- **Per-symbol demean is NEGATIVE in 64/64 cells** (range −0.0095 to −0.145; `demean_t < −2` in 28 cells,
  down to t = −14.1, −9.3, −7.9, −6.9). Subtracting each name's OWN mean forward return — the control for a
  repeatable RANKING effect — **erases and reverses the entire L/S return.**
- **Interpretation:** the positive gross is an unconditional LEVEL effect, not momentum. In a 125-day
  window with non-overlapping H10/H21 rebalances (5–10 periods), a handful of megacaps that drifted up the
  whole window land in the long leg every rebalance. The "signal" is "rank by who already went up," which
  carries no out-of-sample edge once the per-name mean is removed.
- **Shuffle-canary** is ~0 everywhere (mean −0.0005, range ±0.027). So the ranking is non-RANDOM
  (gross ≫ canary) — but "non-random" here just means "sorts on the persistent level," which the demean
  exposes. Beating the canary is necessary, not sufficient; the demean is the decisive control and it fails.

## OOS net-of-cost bootstrap (DECISIVE number)

OOS half (dates t ≥ 62) has very few non-overlapping rebalances: **H5 → 11–12, H10 → 5–6, H21 → 2**
(H21 OOS has n=2 → bootstrap CI is NaN, NOT a pass; a 5-day move is not testable on 2 points either).

Cells whose OOS net@1x bootstrap 95% CI excludes zero ABOVE (n=6, ALL H10 except one H5 quintile):

| universe   | leg      | F  | S | H  | oos_n | turnover | gross  | oos_net1 | OOS 95% CI        | canary  | demean_mean | demean_t |
|------------|----------|----|---|----|-------|----------|--------|----------|-------------------|---------|-------------|----------|
| megacap100 | decile   | 63 | 2 | 10 | 5     | 0.36     | +0.1053| +0.1050  | [+0.0376, +0.1674]| +0.0017 | **−0.0438** | −1.19    |
| megacap100 | quintile | 63 | 2 | 10 | 5     | 0.275    | +0.1110| +0.1108  | [+0.0567, +0.1650]| −0.0070 | **−0.0134** | −0.55    |
| megacap100 | decile   | 63 | 0 | 10 | 6     | 0.358    | +0.0890| +0.0887  | [+0.0220, +0.1511]| −0.0026 | **−0.0524** | −1.17    |
| megacap100 | quintile | 42 | 2 | 10 | 6     | 0.366    | +0.0382| +0.0436  | [+0.0062, +0.0811]| −0.0036 | **−0.0574** | −1.88    |
| megacap100 | quintile | 42 | 2 | 5  | 12    | 0.25     | +0.0274| +0.0341  | [+0.0106, +0.0580]| +0.0020 | **−0.0213** | −1.49    |
| liquid500  | quintile | 63 | 2 | 10 | 5     | 0.31     | +0.0553| +0.0553  | [+0.0134, +0.0958]| +0.0031 | **−0.0095** | −0.53    |

**Every cell that "clears" the OOS bootstrap has a NEGATIVE per-symbol-demean mean.** The OOS-CI pass is
driven by the same few-megacap level drift, on 5–6 OOS rebalances — it is not a robust momentum edge.

## Liquid-500 vs megacap-100

- **megacap100** shows LARGER gross (up to +0.11/rebalance at F63 H10) and more full-sample t>2 cells — but
  also the MOST negative demean (down to t=−14 at F42 H21). The concentration is worse, not better, in the
  100 largest names: fewer names → a couple of persistent winners dominate the long leg even harder.
- **liquid500** gross is more modest and only one cell (quintile F63 S2 H10, n=5 OOS) clears the OOS CI;
  its demean is also negative. The diversification helps the level-bias slightly but does not rescue a
  real ranking edge — there isn't one.

## Best cell, honestly

The single most-tradeable-LOOKING cell is **megacap100 decile F63 S2 H10**: gross +0.105/rebalance,
turnover 0.36, OOS net@1x +0.105 with 95% CI [+0.038, +0.167]. **But** it rests on 5 OOS rebalances and its
per-symbol demean is −0.044 — i.e. remove the persistent per-name level and the edge inverts. It is the
short-window level artifact, not Jegadeesh-Titman momentum.
