# Edge backlog — Modelling Agent (MA)

Ranked, falsifiable intraday-equity hypotheses grounded in our actual feature store (35 groups /
~660 features, `quantlib/features/groups/`) and the hardened prior verdicts in `docs/EXPERIMENTS.md`.
Every item carries: idea · prior · test · cost gate · KILL. Single-writer = the MA.

## Standing context (what is already SETTLED — do not re-tread)
- **Price-only cross-sectional ML has NO tradeable edge** under the 4-gate battery (net-of-cost L/S +
  shuffle canary + survivorship demean + de-fragmented overnight), proven clean/fund-free on 613 days.
- **The 30m signal is `vwap_dev` mean-reversion** (linear, model-independent — ridge AND GBM agree;
  largest |coef|, negative). It carries ~the whole 19-feature signal ALONE. `ret_5m` is a weaker
  redundant proxy; **momentum is dead at 30m** (model-independent); the nonlinearity buys ~nothing.
- vwap_dev breakeven ≈ **1.4–1.66 bps < ~2 bps realistic one-way cost** at turnover ~3.2/period →
  REAL but UNECONOMIC. The binding wall is **turnover/cost**, not signal discovery.
- **Three apparent edges died to the tradeable-entry trap today**: open-cadence gap, gap-fade
  (76% of gross lost in the un-tradeable 09:30→10:00 window; tradeable +5.2bps < ~9bps cost),
  open-anchored momentum. RULE: a return must be booked from a TRADEABLE entry price (≥09:35, never
  the 09:30 print), and cost must be the MEASURED open spread — not a flat charge on a 09:30 return.
- **Overnight "edge" was survivorship**, not timing (collapses under per-symbol demean).
- Path-to-edge per the Lead = **BETTER DATA, not better models on price**: universe-wide ORDER FLOW
  (OFI / signed volume, microstructure), quote-depth dynamics, corporate-action events, delisted backfill.

## Data reality in THIS stack (MA orientation, 2026-06-15)
- Live parquet `/store`: **35 groups, ~660 features, healthy** (feature_scan-equivalent run clean; a
  few window-warmup dead features in `efficiency`/`return_dynamics`/`momentum_consistency` — flagged to Lead).
- **Only TODAY'S single live RTH session is present** (~500–630 symbols/min in 13:30–20:00 UTC). The deep
  613-day battery panel + the experiment Postgres are NOT in this stack (old `~/quant` stack down). **Any
  multi-day backtest is BLOCKED on rehydrating the historical panel / sharded backfill** — the single
  biggest compute ask (see report). Microstructure groups (`trade_flow`/`quote_spread`/`liquidity`/
  `microstructure_burst`/`tick_runlength`) are present but on the limited microstructure-subscription
  symbol set (~50–350 rows/min), not the full universe yet.

---

## ★★★ STATUS (2026-06-16, after the depth-baseline cycle)
- **`/store/raw` has BARS (629 names × 126 days, 2025-12→2026-06) but NOT trades/quotes yet.** So
  **H2-RETEST and H3 are BLOCKED on the trades/quotes backfill** (read `/store/raw/trades` & `/store/raw/quotes`
  when they land — directory-existence is the gate). All heavy compute now runs via `ops/sandbox.sh` (capped,
  /store read-only) — NEVER `docker exec` into live `feature-computer`.
- **H1 CONFIRMED DEAD at depth** (illiq/liq |IC| 6–10×; liquid tier fails cost net). Removed from active pursuit.
- **Powered vwap_dev baseline established:** pooled IC −0.058 (H15) but illiquid-inflated; the H2-RETEST
  orthogonalizes against the **liquid-tier baseline −0.017/−0.014** (see LEADS.md).
- **Next unblocked cycles while trades backfill (bars-only or self-built):** H4 (corporate-action split events
  — LOW-turnover, cost wall weak), H5 (dividend-timing re-confirm), H6 (Garman-Klass vol from `/store/raw`
  OHLC), H7 (rank-space). Working order now: **H2-RETEST (gated) → H4 → H5 → H6 → H7 → H3 (gated) → H8**.

## ★★ REPRIORITIZATION (2026-06-15, after the H2 OFI cycle)
H2 ran on a **self-built Alpaca panel** (80 names × 3 days, tick-rule OFI built from SIP trades — proving
microstructure work is NOT blocked on the platform subscription; I build the data myself per the charter)
and produced the **first standalone signal to clear a shuffle canary on real multi-day data**: `ofi_15`
rank-IC +0.0185 (t +3.96), positive = continuation. AMBIGUOUS on the load-bearing marginal-lift question
only because the vwap_dev baseline was under-powered on 3 days (sign-cancellation in the raw sum). **New top
priority = H2-RETEST**: full universe incl. megacaps × ≥15 days, forward return **orthogonalized on
vwap_dev** before testing `ofi_15`/`ofi_15_norm` on the residual (drop the dead `signed_vol_z`), horizon-
matched 15–30 min holding so net-of-cost is meaningful. Resolves additive-carrier vs conditioner and is the
clearest path to a feature-spec PR. CPU for the IC battery; the heavier ask is fetching trades for a few
hundred names × 15 days (fetch incrementally; do it in the `fp-dev` image, NOT the live container — see ops
note). Working order: **H2-RETEST → H3 → H5 → H4 → H6 → H7 → H1 → H8**.

## ★ REPRIORITIZATION (2026-06-15, after the H1 proof)
H1's proof-of-loop probe KILLED its core thesis: the vwap_dev reversion carrier is concentrated in
ILLIQUID names (illiq/liq |IC| ratio 2.06× at H5, 4.01× at H15 on a live session — see
`2026-06-15-vwap-reversion-cost-conditioning/`), so liquidity-gating gates AWAY the signal and cost-
conditioning is structurally doomed. **New top priority = the better-data microstructure bets (H2, H3)**
and the low-turnover event families (H4, H5), which attack the cost wall by DESIGN rather than by gating an
illiquid-concentrated signal. H1 drops to the bottom. The new working order: **H2 → H3 → H5 → H4 → H6 →
H7 → H1 → H8** (H8 is a data-acquisition ask, not CPU). All microstructure work is BLOCKED until the
microstructure subscription covers the full universe AND a settled multi-day panel exists (see report).

## RANKED HYPOTHESES (original numbering; current order in the reprioritization note above)

### H1 — vwap_dev reversion, COST-CONDITIONED to beat the turnover wall  ★ top / proof-of-loop
- **Idea:** Keep the only proven carrier (vwap_dev mean-reversion) but attack the thing that kills it —
  turnover/cost — instead of hunting a new signal. Trade vwap_dev ONLY where the expected reversion
  exceeds that name's expected round-trip cost: gate entries by liquidity (ADV$/spread proxy) and add
  hysteresis (don't rebalance until |Δsignal| crosses a band) to cut turnover 3.2→~1.5/period.
- **Prior:** vwap_dev breakeven 1.4–1.66 bps is an AVERAGE over all names at full turnover. Cost is highly
  cross-sectionally dispersed (liquid names ≪ 2 bps); the avg-breakeven verdict can hide an economic
  liquid-and-low-turnover SUBSET. Hysteresis is the standard turnover-reduction lever (Grinold-Kahn).
- **Test:** within-date decile L/S on `vwap_dev`; (a) restrict to top-liquidity tertile, (b) add a
  no-trade band so a name only flips legs on a material signal change; sweep band width. Metrics: net-of-
  MEASURED-cost Sharpe, breakeven, turnover. Baseline = full-universe full-turnover vwap_dev.
- **Cost gate:** per-name cost by liquidity (NOT flat 2bps); the liquid subset's measured spread.
- **KILL:** if the best liquidity×hysteresis cell's breakeven still < its own measured round-trip cost
  (i.e. no subset clears its OWN cost), vwap_dev is dead as a standalone tradeable and we stop refining it.

### H2 — OFI / signed-flow marginal lift over the TRUE carrier (vwap_dev), liquid microstructure set
- **Idea:** Does signed order-flow imbalance (`trade_flow`: signed-volume z, OFI windows) add cross-
  sectional IC ORTHOGONAL to vwap_dev at 30m/60m? Flow is the Lead's primary better-data bet.
- **Prior:** vwap-deviation reversion is partly a price-pressure/inventory effect; signed flow is the
  direct microstructure cause and may both refine the signal and predict WHICH deviations revert vs
  continue. Lit: Cont-Kukanov-Stoikov OFI→short-horizon returns.
- **Test:** marginal-IC battery on the microstructure symbol set, three arms — `vwap_dev+gap+ret_5m`
  (true baseline, NOT ret_5m alone — per the W12 correction), +OFI, OFI-only. Same 4 gates. Exclude
  minutes ≥15:50 ET (MOC distortion — Modeller spec in EXPERIMENTS.md).
- **Cost gate:** breakeven must RISE vs the vwap_dev baseline, not just IC.
- **KILL:** OFI marginal IC ≤ canary OR doesn't move breakeven on a settled multi-day microstructure panel.

### H3 — Quote-depth / spread dynamics as a reversion CONDITIONER (separate from trade flow)
- **Idea:** `quote_spread` group (spread, quote imbalance, bid/ask SIZE imbalance) — barely touched.
  Use depth/spread state to CONDITION vwap_dev reversion: deviations on a thin/widening book revert
  differently than on a deep/tight book.
- **Prior:** Quoted depth and spread carry inventory/adverse-selection information distinct from realized
  trade flow (a book can be imbalanced before any trade prints). Conditioning, not a standalone ranker.
- **Test:** interaction battery — vwap_dev × (spread regime, size-imbalance sign); does a conditioned
  book improve net-of-cost Sharpe over flat vwap_dev on the microstructure set?
- **Cost gate / KILL:** no conditioning cell beats flat vwap_dev net-of-cost → depth adds nothing here.

### H4 — Corporate-action EVENT features: reverse-split distress + forward-split attention
- **Idea:** `corporate_actions` (LIVE, deep: 7,133 dividends / 42 fwd-splits / 19 rev-splits / 11 stock-
  div). Splits are UN-mined. Reverse split = distress/delisting-pressure signal; forward split = retail-
  attention/continuation signal. Build days-to/since-split + is-recent-split flags (δ-delayed, parity-safe).
- **Prior:** Reverse-split underperformance and forward-split announcement drift are documented anomalies
  (e.g. Desai-Jain; retail attention post-split). Structurally orthogonal to intraday price.
- **Test:** standalone + marginal battery on the panel (event-day cohorts vs matched controls); net-of-cost
  on a LOW-turnover event horizon (multi-day), where the cost wall is far weaker.
- **Cost gate:** low turnover by construction; breakeven bar is easy — the risk is sample size/IC, not cost.
- **KILL:** event cohorts show no forward-return separation vs controls beyond canary (likely thin: 61 split
  events total — may be UNDERPOWERED; if so, document as "needs more history," not "dead").

### H5 — Family-C dividend-timing, survivorship-clean re-confirm
- **Idea:** Dividend run-up / ex-date drift (`days_to/since_ex`, runup window, is-payer). The ONLY prior
  standalone family that SURVIVED survivorship demean (surv-neutral breakeven 2.1 bps).
- **Prior:** Dividend-capture run-up + post-ex drift is a calendar-of-the-firm effect orthogonal to price.
  Prior caveat: its overnight canary was contaminated (0.014) when COMBINED — must re-isolate.
- **Test:** re-run `family_c_dividend_timing.py` standalone on a clean multi-day panel; the headline is
  whether the standalone surv-neutral breakeven (2.1bps) holds AND the canary is clean in isolation.
- **Cost gate:** low-turnover dividend horizon; breakeven 2.1bps must clear measured cost on payers.
- **KILL:** canary stays elevated in isolation (intrinsic selection) OR breakeven < measured cost.

### H6 — OHLC-efficient volatility (Garman-Klass / Rogers-Satchell) as a cost/regime conditioner
- **Idea:** `ohlc_vol` group exists but is unexplored as a CONDITIONER. GK/RS vol (5–8× more efficient than
  close-to-close) → identify low-vol names/minutes where vwap_dev reversion is cleaner and cheaper.
- **Prior:** Reversion is stronger and spreads tighter in calmer microstructure regimes; an efficient vol
  estimate sharpens the regime cut without adding turnover.
- **Test:** vwap_dev net-of-cost Sharpe within GK-vol terciles; does the low-vol tercile clear cost?
- **KILL:** no vol tercile lifts vwap_dev breakeven above its measured cost.

### H7 — Cross-sectional RANK features as the model input (rank-space reversion)
- **Idea:** `cross_sectional_rank` group (universe percentile of return/vol/etc per minute) — pinned-universe
  parity-safe. Feed RANKS not raw values; reversion in rank-space is robust to fat tails and aligns with
  decile trading.
- **Prior:** Rank labels already mildly beat raw IC; rank FEATURES may be a more trading-aligned carrier and
  reduce turnover via rank stability.
- **Test:** vwap_dev-rank vs vwap_dev-raw, net-of-cost + turnover. Does rank-space cut turnover?
- **KILL:** rank-space gives no turnover/breakeven improvement over raw vwap_dev.

### H8 — Delisted-name backfill: overnight survivorship-FREE re-test (data-acquisition, Lead-owned)
- **Idea:** Put delisted/merged names BACK into each historical cross-section (PIT) and re-run the overnight
  battery WITHOUT the conservative per-symbol-demean proxy.
- **Prior:** Per-symbol demean (which killed overnight) over-removes — it also kills any real persistent
  alpha. The honest test is source-level de-biasing.
- **Test:** acquire adjusted daily OHLCV + delist dates for ~50–120 liquid names delisted 2024→now; rebuild
  `universe_membership` PIT; re-run overnight battery, no demean.
- **Cost/KILL:** if net-of-cost overnight Sharpe stays positive survivorship-FREE → genuine lead (full M3).
  If it collapses like the demean proxy → no-edge verdict confirmed at the SOURCE. Either outcome is a result.
  **Blocked on a data source (Polygon/FMP/Sharadar) — a Lead/owner data-acquisition ask, not CPU.**
