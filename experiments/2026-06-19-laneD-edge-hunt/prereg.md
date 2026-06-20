# Lane D — EDGE HUNT on the EDGAR + sector signal surface (PRE-REGISTRATION)

**Author:** Modeller · **Date:** 2026-06-19 (PT) · **Status:** PRE-REGISTERED — written BEFORE looking at any outcome.

This is the payoff of Lane D. We have spent several cycles BUILDING the sector (#182) and EDGAR-frequency
signal surface; this experiment tests whether that surface buys **tradeable edge**, with the same
anti-fooling discipline as the batch1-4 invention screens + Modeller3's turbulence screen. An **honest
null is a SUCCESS** — we have 3 settled direction-nulls on the price surface; a clean null on EDGAR/sector
direction (with t-stats) is a real result that constrains the strategy-battery faithfulness targets.

NO quantlib edits. NO fingerprint flip. Research deliverable only. Reads stores READ-ONLY (`fp_store_real`
bars RO, Postgres `filings`/`sector_map` RO). Bounded `--rm` sandboxes.

---

## SUBSTRATE (verified before pre-registering — counts only, no outcomes)

- **Deep minute bars:** `fp_store_real` `raw/bars/symbol=*/date=*` — 1-min OHLCV+vwap+trade_count, **2016-01-04
  → 2026-06-18**, 7,703 symbols, extended hours (08:00–23:59 UTC). 252 trading days/yr.
- **Filings:** Postgres `filings` — 3,175,782 rows, **1994 → 2026-06-19**, 5,628 symbols, 462 form types.
  Look-ahead-safe `available_at` (SEC "submissions_accepted" acceptance instant for 3.17M of 3.18M; the
  remaining 8k are the live `atom_feed`). 8-K (461k), Form-4 (1.37M), 10-Q (121k), 6-K, 10-K. The 2016+
  intraday hour-of-day distribution is REAL (8-Ks cluster at 20–22 UTC = after the 16:00 ET close, plus a
  pre-market tail), not a midnight backfill artifact.
- **sector_map:** Postgres — 11,322 symbols, 11 GICS sectors (the #181 yfinance population; the #182
  features key off the SAME map).

### Look-ahead discipline for `available_at` (the EDGAR trap, pre-committed)
- A filing enters a feature ONLY when `available_at <= entry_t`. The `available_at` is the SEC acceptance
  instant — a known small lag exists between acceptance and full public dissemination, so I add a
  **conservative +5-minute embargo**: a filing is "known" at `available_at + 5min`. This is intentionally
  pessimistic (never lets a filing inform an entry it could not have).
- Entries are TRADEABLE: `>= 13:35 UTC (09:35 ET)`, never the 09:30 open print (the gap-fade look-ahead trap).
- All labels are FORWARD only (T → T+h), entered at the tradeable price at T.

---

## HYPOTHESES (pre-registered — 2 chosen, each with a direction + magnitude leg)

The diagnostic across the 3 settled price-surface direction-nulls + Modeller3's turbulence screen is
consistent: **our features inform intensity/volatility/move-magnitude, NOT signed cross-sectional
direction.** So each hypothesis is tested on BOTH a magnitude axis (where signal has survived before) and a
direction axis (where it keeps dying) — and I expect, a priori, magnitude > direction. Pre-committing that
expectation so a magnitude-only hit is not re-narrated as a direction win.

### H1 — EDGAR filing activity → forward MOVE-MAGNITUDE / VOLUME (primary), and DIRECTION (secondary)
**Claim:** A symbol with elevated recent SEC filing activity (a *burst*) or a very recent material filing
(8-K recency) has higher forward realized move-magnitude / volume than its baseline — an
information-arrival intensity effect. Direction is tested but expected null.

Features (all point-in-time, `available_at + 5min <= T`):
- `edgar_burst_7v90` = filing_count(trailing 7d) / (filing_count(trailing 90d)/90*7 + 1) — burst ratio vs a
  90-day baseline (the #182-roadmap `edgar_filing_burst` shape).
- `edgar_cnt_7d` = raw count of filings in the trailing 7 calendar days.
- `mins_since_8k` = minutes since the symbol's most recent 8-K `available_at` (capped/transformed; large =
  no recent 8-K). 8-K = the material-event form, the most magnitude-relevant.
- `mins_since_any` = minutes since the most recent filing of ANY form.

Targets (forward, entered at T's tradeable price):
- **Magnitude:** `Y_absret_{30,60}` = |forward signed return| over T..T+h; `Y_rv_30` = forward realized vol
  (std of 1m logret); `Y_vol_30` = forward log volume.
- **Direction:** `Y_ret_{30,60}` = forward SIGNED return.

**Predicted (pre-committed):** burst/8k-recency → POSITIVE relation to magnitude/volume targets, surviving
shuffle + the own-vol marginal control (net of vol persistence). Direction ~ null.

### H2 — SECTOR-RELATIVE reversal/momentum (the #182 sector features)
**Claim:** A name's within-sector excess return (`sector_excess`, own minus its GICS-sector EW mean)
carries a cross-sectional **reversal** (over-extension vs peers reverts) OR **momentum** (leaders keep
leading) signal at intraday horizons — the classic sector-relative mean-reversion question. Plus: does a
high-|sector_beta| name's forward move depend on its sector's contemporaneous move (sector-beta-conditional
amplification)?

Features (point-in-time, <= T; computed from the deep bars + sector_map, the SAME definitions as #182):
- `sector_excess_{15,30,60}` = own trailing-W return − its sector EW-mean trailing-W return.
- `sector_ret_{15,30,60}` = the sector EW-mean trailing-W return (the sector's own move).
- `abs_sector_beta_30` = |rolling 30m OLS beta of own 1m return on its sector's 1m aggregate| (the #182
  `sector_beta`), as a CONDITIONER not a direct predictor.

Targets (forward): `Y_ret_{15,30,60}` (signed — reversal/momentum is inherently a DIRECTION test here, the
honest framing) + `Y_absret_{30}` (magnitude, for completeness).

**Predicted (pre-committed):** This is a genuine DIRECTION test (sector-relative reversal is a directional
claim), so unlike H1 it is NOT pre-shaded toward magnitude. Prior from the 3 direction-nulls is skeptical;
the open question is whether SECTOR-RELATIVE (a different framing than the price-only cross-section that
nulled) revives it. The sector-beta conditioner is tested as an interaction (does sector_excess's relation
to forward return strengthen/flip for high-|sector_beta| names).

---

## SCREEN DESIGN (discipline — non-negotiable, pre-committed)

**Panel.** Substrate row-comparable to the invention/turbulence screens: a liquid universe (top-N by RTH
dollar volume), tradeable entry minutes `>= 09:35 ET`, point-in-time features over completed bars `<= T`,
forward labels over T..T+h. To give EDGAR events room (filings are sparse per name per day) the panel
spans a **multi-month-to-multi-year window** sampled across days (not the 24-day invention window) — exact
span set by cost, reported in results. Each row = (day, entry-minute, symbol).

**Metric.** For the cross-sectional H2 (reversal/momentum), a within-(day,minute) **rank-IC** of feature
vs forward target (Spearman), aggregated across timestamps with a Newey-West t-stat on the per-timestamp IC
series. For H1 (an event-intensity claim that is NOT purely cross-sectional — a quiet name has no burst), a
pooled relation with timestamp fixed-effects (de-mean the target within each (day,minute) so a market-wide
move can't masquerade as a filing effect) + NW t-stat.

**Baselines (every hypothesis):**
1. **SHUFFLE** — permute the target across rows WITHIN each (day,minute) block (preserves the
   cross-sectional structure, breaks the feature↔label link). Edge must vanish: report the real
   statistic's z vs the shuffle distribution (≥200 iters).
2. **PREDICT-ZERO / unconditional** — the trivial "no signal" benchmark; the feature must beat it.

**OOS.** Walk-forward **year-split** (and a within-window early/late split for short spans): fit the sign /
direction on the earlier block, score the later block, report **OOS sign-consistency**. A feature that only
works in-sample is flagged, not promoted.

**Marginal control (the turbulence lesson).** Partial out already-shipped / mechanical predictors so the
result is NET-NEW, not re-discovered vol persistence:
- For magnitude/volume targets: partial out **own trailing realized vol** (`own_rv_30`) and the universe
  **`mkt_rv_30`** turbulence scalar from BOTH sides; report the partial statistic + the collapse ratio
  (partial/raw). A pair that collapses to ~0 = no marginal edge (it was vol persistence).
- For H2 reversal: partial out own trailing return (the raw reversal a single-name feature already gives)
  so `sector_excess` must beat a plain own-return reversal to count as a SECTOR effect.

**Multiple comparisons.** **Benjamini-Yekutieli FDR** across ALL (feature × target) pairs tested in this
experiment (q = 0.10). A hit must survive BY-FDR, not just an individual z ≥ 3. The full pair count is
reported so the correction is honest.

**Cost sensitivity.** For any surviving pair, recompute the implied long/short decile spread NET of a
realistic round-trip cost (report at 5 and 10 bps) + a `$1`-floor on prices; any multi-day/overnight label
gets per-day symmetric winsorization + a label-std sanity check (the overnight-trap guard). A signal that
only survives at zero cost is reported as non-tradeable.

**Honest-null contract.** Each (hypothesis, target) gets a verdict line: raw stat, shuffle-z, OOS sign,
partial stat + collapse ratio, BY-FDR survival, cost-net spread. A null is reported with its t-stats as a
real finding — these verdicts become the strategy-battery faithfulness targets (aligning with BatteryBuild:
a battery archetype run on this surface should reproduce these signs/magnitudes).

---

## STOP CONDITIONS (pre-committed)
- If H1 magnitude survives shuffle + own-vol partial + BY-FDR + OOS sign → flag to Lead for a confirmatory
  replication on a DISJOINT year before any excitement (no promotion this cycle).
- If everything nulls → report the null cleanly with t-stats; that settles whether this surface is
  directionally tradeable and points the next hunt elsewhere (longer horizon / different label / the
  magnitude-target strategy family).
- I will NOT add features, tune thresholds post-hoc, or switch targets after seeing outcomes. Any
  exploratory follow-up beyond these pre-registered pairs is labeled EXPLORATORY and excluded from the FDR
  family / promotion claim.
