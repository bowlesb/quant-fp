# Journal — explorer-features (new features & data families)

Append-only. Every idea, proposal, dead end, and learning — dated. Dead ends are
first-class: "tried, nothing there" stops re-treading. My lens: what measurable thing
about a stock/minute is NOT in our 21-feature panel? What transform of existing data
carries information we are discarding?

The binding constraint I inherit (read EXPERIMENTS.md before proposing):
- Price-only has NO tradeable edge under the 4-gate battery (30m IC 0.027 real but
  net-negative @ breakeven ~1.4bps < ~2bps cost; overnight = SURVIVORSHIP, timing ~0).
- The grind verdict: **"the 30m signal IS ret_5m; momentum is dead weight."** So the
  bar for a NEW feature is high — it must carry cross-sectional information ORTHOGONAL
  to ret_5m, and (the real test) it must move BREAKEVEN, not just IC.
- Path to edge per the Lead = BETTER DATA: order-flow (OFI v1.2.0, in flight) +
  delisted backfill. My job is to widen that search: what other measurable, available
  signal is the panel discarding?

Standing rules I obey: pre-register (hypothesis + metric + falsifier BEFORE running);
DB read-only; write ONLY this journal + my proposal dir; commit as
--author="explorer-features <explorer-features@quant-team>"; mechanism > data-mining;
elegant > complex. Every proposal carries the 4 gates (shuffle canary, survivorship
neutralization, net-of-cost, turnover honesty) or the Lead rejects it.

---

## 2026-06-12 — Wake 1: data inventory + first proposal batch

### What I read
- EXPLORATION_PIPELINE / MISSION / ROADMAP (M2 NOW) / EXPERIMENTS.md (full clean-v1.1.1
  verdict + Families A/B/C + the open-gap/horizon-ensemble shapes + cost-by-liquidity).
- INSPECT.md; queried the live DB (read-only) for the actual data inventory.

### Data inventory (ground truth, queried 2026-06-12 — what EXISTS vs needs collection)
- **bars_1m**: full OHLCV + vwap + trade_count, 1213 syms, 2023-12 → 2026-06, ~297M
  backfill bars. OHLC means Parkinson / Garman-Klass realized vol and intraday-range
  structure are FREE — and NONE of them are in the panel (only vol_30m/60m = simple
  close-to-close std). **This is the cheapest unexplored family.**
- **corporate_actions** (LIVE, deep): 7,133 cash_dividends / 42 forward_splits /
  19 reverse_splits / 11 stock_dividends, 2023→2026. Family C (dividend timing) already
  mines the dividends. SPLITS are NOT mined yet (reverse-split distress signal, forward-
  split retail-attention signal). Cheap, available.
- **quote_agg_1m / trade_agg_1m**: microstructure, ~50 names now, scaling to 512 Monday.
  OFI (trade_agg) is the Lead's in-flight bet. quote_agg (spread, quote_imbalance,
  bid/ask SIZE imbalance) is a SEPARATE family barely touched — depth/spread dynamics.
- **news**: TABLE IS EMPTY (0 rows). Any news feature = collection-first, NOT cheap.
  De-prioritized until someone owns the collector (flag to Manager).
- **asset_metadata**: tradable/shortable/exchange/name — but **NO sector / industry /
  shares-outstanding / float column anywhere.** Sector-relative and float-normalized
  features are completely unexplored AND need a data source. Brief says a sector map is
  "landing" — so sector-relative is the highest-value MEDIUM-cost family: spec it now,
  ready to fire when the map lands.

### The 21-feature panel — what it ALREADY has, so I don't re-propose it
ret_5m/15m/30m/60m, vol_30m/60m (close-to-close std), vol_z_30, vwap_dev, range_pct,
gap_from_open, rel_ret_30m, minute_of_day, day_of_week, mom_1d/3d/5d/10d(+_rel).
=> Has: return term-structure, simple vol, one range feature, one gap feature, daily
momentum, calendar. MISSING: high-low-based vol estimators, intraday range PERCENTILES
(vs the name's own history), volume-profile/turnover, sector-relative anything,
split-event features, quote-depth dynamics, gap-FADE conditioning. That gap list is my
proposal pipeline.

### Already-tried (do NOT re-tread) — from EXPERIMENTS.md + proposal dirs
- Family A: ex-div overnight-label correction (mechanical drop neutralized).
- Family B: dispersion / beta-to-universe / idiosyncratic residual — DIES under
  survivorship demean (survivorship_neutral_sharpe -1.5). Dead as standalone.
- Family C: dividend-timing (days_to/since_ex, runup window, is_payer) — the ONLY family
  whose standalone survives demean (survivorship_neutral_breakeven 2.1bps) BUT its
  overnight canary is contaminated (0.014) when combined. Live frontier; Lead owns.
- Shapes (explorer-shapes lane): open-gap, opening-range, horizon-ensemble.
- ML lane (explorer-ml): ridge linear floor.
- OFI (Lead, in flight, trigger-gated ~6/26): trade_agg signed-volume z-scores.

### My batch-1 theses (each = a proposal file, mechanism-first)
1. **001 — High-low realized-vol estimators (Parkinson/Garman-Klass) + intraday range
   percentile.** FREE (bars OHLC). Mechanism: close-to-close vol throws away the H/L path;
   GK is ~5-8x more efficient and the range percentile (today's range vs the name's own
   30-day range) is a cross-sectionally-comparable "is this name unusually active" signal
   orthogonal to ret_5m. Test marginal IC + breakeven over price-only.
2. **002 — Turnover / volume-surprise family.** FREE (bars volume). Mechanism: volume
   relative to the name's OWN recent norm (volume z-score, dollar-turnover percentile) is
   the classic attention/information-arrival proxy; volume LEADS volatility and often
   return continuation. The panel has NO volume feature at all — a glaring hole.
3. **003 — Split-event features from corporate_actions.** Cheap (CA feed live). Mechanism:
   reverse splits cluster in distressed/delisting-bound names (short-leg signal);
   forward splits draw retail attention (post-announcement drift). Days-to/since-split +
   recent-reverse-split flag, mirroring Family C's structure but for the split feed nobody
   mines. Tests a structurally orthogonal corporate-calendar effect.
4. **004 — Sector-relative / sector-neutralized returns (SPEC, gated on the sector map).**
   Medium cost (needs the landing sector map). Mechanism: the panel ranks names against
   the WHOLE universe; but a tech name's 30m move is mostly sector beta. Sector-demeaned
   returns isolate the within-sector idiosyncratic move — the purest cross-sectional alpha
   target — and a "sector momentum" conditioner. Pre-spec so it fires the moment the map
   lands. (Distinct from Family B, which used universe-beta, not sector membership.)
5. **005 — Quote-depth / spread-dynamics family from quote_agg (gated on M2 scale).**
   Mechanism: bid/ask SIZE imbalance and spread COMPRESSION/widening are the resting-
   liquidity side of order flow, orthogonal to trade-side OFI. Spread regime also conditions
   WHICH names are cheap to trade — directly attacks the cost wall that kills every signal.
   Spec now; fires when quote_agg scales to 512 Monday. Inherits the ≥15:50 ET close
   exclusion (Modeller's parity spec).

Writing all five as pre-registered proposals now. Sending the Lead a batch summary after.

## 2026-06-12 — Wake 1 cont.: Lead dispositions + building the 3 approved scripts

### Lead dispositions (all 5 written into the proposal files)
- BUILD ORDER: 002 family_e_volume FIRST (widest hole, zero collection, orthogonal), then
  001 family_d_highlow_vol, then 003 family_f_splits (lower priority, tiny event counts —
  judge STRICTLY on survivorship-neutralized number; reverse-split is likely a survivorship
  proxy = short-leg risk filter, not timing alpha).
- IMPOSED REFINEMENT on all three: report vs the C11 price-only baseline already in
  results.jsonl (IC ~0.027, breakeven ~1.4bps) apples-to-apples; mirror family_b/family_c
  structure EXACTLY so gates are byte-identical via battery.run_config.
- BLOCKED-but-approved-as-spec: 004 sector (Lead escalated → task #8: wire FMP profile→GICS this
  weekend) and 005 quotes (M2 scale + accrual). Don't idle on them.
- LOAD-BEARING CONTEXT (reshapes interpretation): ret_5m+position signal lives in the ILLIQUID
  broad cross-section (IC ~0 on the liquid-50 tier); ret_5m is a REVERSAL concentrated in
  illiquid names. So when a feature lifts IC, check WHERE — a feature that only helps illiquid
  names inherits the cost problem. The PRIZE is a feature carrying signal in the LIQUID head
  (cost payable). => baked a liquidity-stratified read into all 3: each variant runs on the FULL
  panel AND the top-ADV quartile (LIQUID_HEAD_FRAC=0.25, median daily $-volume) SEPARATELY.

### Engineering design (all 3 scripts)
- family_d/e need raw bars (OHLC / volume); a family_c-style (symbol,ts) join isn't enough.
  Design: STREAM one symbol at a time — load its RTH bars over [pad_start, end], build a
  date→minute→bar index, compute that symbol's panel-row features, discard. Bounded memory across
  ~715 names. pad_start = 1yr before the panel min so the 20-day same-time-of-day baseline has
  history.
- Same-time-of-day baseline (NOT whole-day) for volume/range surprise removes the intraday
  U-shape → these are NOT calendar features in disguise (the trap that faked the early price IC).
- family_f clones family_c (cached (symbol,date) split-distance lookups).
- DEV ERGONOMICS: added SMOKE_SYMBOLS env (keep first N symbols) to all three — per-symbol
  full-year bar load makes a full smoke slow; SMOKE_SYMBOLS validates correctness cheaply without
  changing production behavior.

### Smoke-test friction (learned)
- EXIT=137 = OOM-killer under transient pressure when 2-3 heavy smokes ran CONCURRENTLY in the
  experimenter (no cgroup mem limit — Memory=0; host has 68GB free → it's contention, not a hard
  ceiling). LESSON: run ONE bar-streaming family at a time; the queue must serialize these (each
  streams bars + trains LightGBM). Flag to compute-coordinator (#7): these are heavier than the
  panel-only batteries.
- Per-symbol full-year bar load is the smoke bottleneck (sequential DB round-trips), not memory.
  Fine for the real one-time run; SMOKE_SYMBOLS=30 is the fast dev path.
- OPTIMIZED the hot path in family_d/e (2026-06-12): compute_symbol_features was re-summing bar
  ranges over 20 prior days FOR EVERY panel row. Now precompute per-(date, cadence-minute)
  trailing-volume / session-dollar / Parkinson-GK ONCE per symbol → each row is an O(20) lookup.
  Same numbers, far fewer ops. (The remaining time sink is the gate harness itself — LightGBM
  walk-forward × canary × 2 backtests × 6 variants × 2 horizons — inherent to the battery.)
- SMOKE VALIDATION (60d × 30 sym, family_e, fwd_30m): pipeline works end-to-end, NaN-rate 1.7%,
  all 4 gates populate, liquidity-stratified read works. NOT a verdict (thin). One mechanically
  notable smoke-only artifact: liquid_head baseline IC +0.047 vs full-panel negative — consistent
  with the Lead's "signal differs by liquidity tier" note; the stratification is doing its job.

## 2026-06-12 — Wake 1 cont.: NEW protocols (shared research DB, literature, reports) + lit search

### Three new binding protocols from the Lead (re-read EXPLORATION_PIPELINE.md)
1. **Shared research DB**: read-only lifted for a new `research` schema. Any mind may CREATE
   (`<agent>_<name>` own / `common_<name>` shared); REGISTER in research.catalog SAME turn;
   builder scripts → experiments/builders/ (PIT-guarded, regenerable); `public` stays read-only;
   QUERY THE CATALOG FIRST to dedup. Existing common_ to reuse: common_spreads_at_cadence (mine,
   half-spread@cadence — cost-gate against it), common_daily_session_price (open/10:00/range/close).
   DECISION for my families: family_d/e/f compute features in-python from bars per-run (standalone,
   no persistence). I'll only promote a derived bar-aggregate to a common_ table if a SECOND lens
   needs it (e.g. if explorer-shapes wants my volume-surprise) — premature to materialize now.
2. **Literature each wake**: targeted searches, cite in journal, lit INFORMS pre-registration but
   never replaces gates (published alpha = hypothesis here). Done this wake (below).
3. **Reports**: every completed experiment → experiments/reports/<date>_<agent>_<topic>.md with
   tables not adjectives. Unreported = doesn't count toward the Monday bar. Will write one per
   family as each completes.

### LITERATURE SEARCH (my lens: volume/microstructure/anomalies), 2026-06-12
- **Llorente, Michaely, Saar, Wang (2002), "Dynamic Volume-Return Relation of Individual Stocks,"
  RFS 15(4):1005-1047** (https://academic.oup.com/rfs/article-abstract/15/4/1005/1567663).
  TAKEAWAY: the volume-return relation is CONDITIONAL — volume from risk-sharing trades → return
  REVERSAL; volume from informed/speculative trades → CONTINUATION; the cross-sectional split
  tracks the extent of informed trading. => DIRECTLY validates my signed_vol_ret_interaction
  mechanism AND warns: the sign is NOT universal — it flips with informed-trading intensity. The
  model may need volume-surprise to interact with a liquidity/informedness proxy, not just ret sign.
- **Chordia & Subrahmanyam (2004), "Order Imbalance and Individual Stock Returns,"** + Chordia-Roll-
  Subrahmanyam "Liquidity and Market Efficiency"
  (https://www.cis.upenn.edu/~mkearns/finread/Chordia_buy-sell_orders.pdf).
  TAKEAWAY: lagged order imbalance POSITIVELY predicts short-horizon (5-min) returns (continuation),
  transaction-based imbalance stronger than dollar-based; BUT predictability DIMINISHES as spreads
  narrow / liquidity rises (it's a market-maker inventory price-pressure effect). => the volume/
  imbalance signal CONCENTRATES in less-liquid names — same signal-vs-cost tension as task #5 and
  the Lead's Cont-Kukanov-Stoikov note (impact ∝ 1/depth). My liquidity-stratified read is the
  right instrument: if family_e only lifts IC in the illiquid tail and dies in the liquid head,
  that's the LITERATURE-PREDICTED outcome, not a surprise — and it inherits the cost problem.
- **Realized-range lit (Martens-van Dijk realized range; G7 range-estimator studies; GK most
  robust)** (https://repub.eur.nl/pub/7582/ei2006-10.pdf). TAKEAWAY: Parkinson/GK are validated as
  vol ESTIMATORS (range ~5-8x more efficient than squared returns; GK most robust), but the lit
  uses them to MEASURE/forecast vol, NOT to RANK cross-sectional returns. => my range-percentile-
  AS-SIGNAL (family_d) is genuinely novel ground, exactly as the Lead predicted. Confirmed + cited.
- NET EFFECT ON PRE-REGISTRATION: family_e/d hypotheses UNCHANGED in direction, but I add a
  SHARPENED prediction (logged blind, before family_e full results): the volume-surprise lift, IF
  any, will be LARGER in the illiquid tail than the liquid head (Chordia-Subrahmanyam liquidity
  gradient). The PRIZE-test (signal in the liquid head where cost is payable) is therefore a HIGH
  bar the literature says is hard to clear — clearing it would be the genuinely valuable result.
  No empty searches this wake; all three queries returned directly on-point primary sources.
