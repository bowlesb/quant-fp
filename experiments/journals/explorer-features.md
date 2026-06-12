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
