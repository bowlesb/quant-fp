# H13 Method — Small-capital re-cost

**Run date:** 2026-06-16  
**Universe:** 7,331 symbols with ADV data from /store/raw/bars  
**Quotes panel:** 2,504 symbols from /store/raw/quotes  

---

## 1. ADV_dollar

Computed as median daily dollar volume (sum of close×volume over RTH bars) over the **last 20 trading dates**
in the panel (2026-05-15 – 2026-06-16 approx). RTH = UTC hours 13–21, with the open bar requiring
UTC hour==13 and minute≥30 (13:30 UTC = 09:30 ET, correct per pitfalls doc).

All 7,331 symbols with >0 dollar volume in that window are included.

## 2. Half-spread model

### Measured (quoted names)

For the 2,504 symbols with quotes data, median half-spread is computed as:

```
half_spread_bps = 0.5 * (ask - bid) / midpoint * 10000
```

over all RTH quote updates with bid>0 and ask>bid. Only RTH quotes used (same 13:30–21:00 UTC filter).

Measured coverage by tertile:
- Liquid (top-ADV 2,443 symbols): **2,390 measured** (98%), 53 estimated
- Mid (2,444 symbols): **82 measured** (3%), 2,362 estimated
- Illiquid (2,444 symbols): **32 measured** (<2%), 2,412 estimated

**Critical honesty note:** The illiquid tertile, where H10's alpha actually concentrates, has almost NO
measured spread data. Only 32 of 2,444 names have quotes. The spread model below is an extrapolation
into the far tail of the ADV distribution. It is flagged as an estimate and very likely an **UNDER-estimate**:
the model is fit on names averaging $66M ADV, extrapolating to names with median $36K ADV — a gap of
~1800×. Measured vs. estimated half-spreads in the illiquid tail should be treated as a lower bound,
not a point estimate.

### Fit for non-quoted names

OLS fit on the 2,504 quoted symbols:

```
half_spread_bps = a + b * log10(ADV_dollar) + c * (1/price)
```

Results:
- a = 64.93, b = -7.02 (per log10-dollar of ADV), c = 0.53 (per inverse price unit)
- R² = 0.254 on 2,504 names (y_mean=10.0 bps, y_std=10.4 bps)
- R² = 0.25 is modest — substantial residual variance even within the quoted universe

**What R²=0.25 means for illiquid names:** The fit explains only 25% of spread variance even in-sample
(quoted names). For the truly illiquid tail (>4 orders of magnitude smaller ADV), extrapolation error
could easily be 2–5× in either direction. The model predicts ~33 bps for the median illiquid name;
true spreads in names trading $36K/day could plausibly be 100+ bps. The 2× sensitivity test in results.md
likely understates the possible error.

## 3. Impact model

```
impact_bps = k * sqrt(order_notional / ADV_dollar) * 100
```

where the sqrt term is dimensionless (fraction of ADV), and ×100 converts to bps.

- **Baseline k = 10** (Almgren-Chriss convention; represents average-liquidity conditions)
- **k = 5 sensitivity** (highly optimistic: frictionless fill, minimal adverse selection)
- **k = 20 sensitivity** (stress: high adverse selection, illiquid order book)

Orders tested: **$5,000** and **$10,000** per name.

For the median illiquid name (ADV = $35,822):
- $5K order = 14.0% of daily volume. Even at k=5, impact = 5 × sqrt(0.14) × 100 ≈ 187 bps.
- $10K order = 27.9% of daily volume. At k=5, impact = 5 × sqrt(0.28) × 100 ≈ 264 bps.

These are large-participation trades even at $5K. The 1% ADV cap used in the capacity sweep allows
only $358/name for the median illiquid stock.

## 4. Round-trip cost

```
round_trip_cost_bps = 2 * (half_spread_bps + impact_bps)
```

The 2× accounts for both entry and exit. The 2× stress on half-spread adds 2×half_spread to the baseline
cost (effectively tripling the spread component while keeping impact fixed), as a conservative bound
on the spread-estimation error.

## 5. Capacity ceiling method

- Per-name max order = min($50K, 1% × ADV_dollar)
- Valid names at each order size = those where 1% ADV ≥ order_notional
- Total capital = order_notional × n_valid_names
- Net alpha = gross_alpha_bps − median_rt_cost_bps
- Sweep: order sizes $1K, $2.5K, $5K, $10K, $25K, $50K
- Report: maximum total capital at which net alpha remains positive (breakeven point)

The capacity sweep uses the OOS demeaned alpha at the 1d horizon as the "gross alpha" baseline.
Since the illiquid-tertile OOS 1d alpha is itself negative after even the smallest cost deduction,
the capacity ceiling evaluates to $0 at all k values.

## 6. H10 illiquid tertile re-score

Reuses the H10b event cohort (8-K filings, 2025-12-15 onward, DB `filings` table), the same train/OOS
split (first 63 / last 63 trading days), and the same D+1 open-entry convention. The per-symbol cost
applied is the **cohort-median** round-trip cost (since the actual per-event symbol varies and many events
fall on names without direct spread measurement). Per-symbol demean is computed within each OOS/train
split. Canary permutation (10 seeds, within-date shuffle) is reported for all cells.
