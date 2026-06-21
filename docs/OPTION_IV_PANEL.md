# Historical Option-IV Panel — scope, pilot result, and full-backfill plan

**Owner:** DataIntegrity · **Status:** scoped + bounded pilot PROVEN · **Decision needed:** Lead/Ben go on
the full backfill (universe + window + granularity).

The Modeller's vol work (`experiments/2026-06-21-vol-implied-vs-proxy`, #331) surfaced ONE surviving
thread: the *unconditional* structural variance-risk-premium (IV ~1.1–1.3× realized). Studying / harvesting
it as a point-in-time strategy needs a HISTORICAL option-IV panel — not the single live snapshot the
Modeller used. This doc is the scoping of what Alpaca can actually deliver, the store design, the bounded
pilot result, and the full-backfill estimate.

> Honesty flag carried forward from the Modeller's RESULTS.md: that screen did NOT itself strengthen the
> case for this backfill (the VRP it would harvest is unconditional; the forecast doesn't improve it). This
> doc scopes the infra Ben greenlit; the $-decision on harvesting the VRP is a separate Modeller/Ben call.

## 1. What Alpaca offers for HISTORICAL options (the binding constraint)

Probed directly against the live API (alpaca-py 0.43.4) + the official docs:

| Endpoint | Carries IV/greeks? | Historical (start/end)? | History depth | Verdict |
|---|---|---|---|---|
| `get_option_chain` / `get_option_snapshot` | **YES** (implied_volatility + greeks) | **NO** — snapshot only (`updated_since` is a freshness filter, not as-of) | current only | the ONLY IV source, but un-replayable |
| `get_option_bars` | no | **YES** (start/end, minute + daily) | since **2024-02** (verified back to 2024-02-01) | the historical price surface |
| `get_option_trades` | no | **YES** (verified deep, Dec-2024 returns 12k+ rows; the "7 days" docstring is stale) | since ~2024-02 | tick-level marks |
| option **quotes** (historical) | — | **NO endpoint** — `/v1beta1/options/quotes` returns 404; only `get_option_latest_quote` (snapshot) exists | — | no historical bid/ask |

**Conclusion: Alpaca does NOT serve a historical IV/greeks time series.** The chain/snapshot (the only IV
source) is current-only. So a historical IV panel must be **RECONSTRUCTED**, not downloaded:

> per (underlying, date, contract): historical option **bar close** (= EOD mark) + underlying **spot** for
> the same session + a rate assumption → **invert Black–Scholes** → IV + first-order greeks.

Other facts pinned during scoping:
- **Contract discovery** (which OCC contracts existed on a past date): the Trading API
  `get_option_contracts` with **`status=INACTIVE`** enumerates EXPIRED contracts (status=ACTIVE alone
  returns 0 for a past expiry). Both statuses paginated + deduped covers the historical universe.
- **Feeds:** `indicative` (free, 15-min delayed) and `opra` (subscription). Historical daily bars returned
  identical rows on both for the probed contracts — daily marks do not need OPRA. (Intraday/recent windows
  may differ; the pilot uses daily, feed-agnostic.)
- **Granularity:** both **minute** and **daily** option bars are available historically (verified 163
  minute bars on an expired contract for one session). The pilot uses **daily** (one EOD mark/contract/day)
  — the right grain for a daily VRP study and ~390× lighter than minute.

## 2. Store design (separate namespace, manifest-driven, point-in-time correct)

A SEPARATE namespace from the raw tape because the grain differs (one row per
(underlying, date, contract) carrying reconstructed values, not a raw tick):

```
<store>/option_iv/underlying=<U>/date=<YYYY-MM-DD>/data.parquet
<store>/option_iv/_manifest_option_iv.d/part-*.parquet   (append-only, unioned on read)
```

- Mirrors the raw tape's `(symbol, date)` partition grain → the SAME manifest "skip what's on disk, never
  double-acquire" resume logic applies (`done_keys` ignores rows==0 entries, the raw-tape poison-entry
  guard). The raw `bars/quotes/trades` namespace is a sibling and is **never touched**.
- **Point-in-time / golden-set-safe:** every row carries `available_at` = the session-close instant the
  EOD mark is knowable (21:00 UTC / 16:00 ET). A study reading as-of T filters `available_at <= T`; an EOD
  mark for date D is never knowable before D's close. (Refinement for production: a daily mark is a
  *close-time* observation — a strictly intraday point-in-time study would want minute granularity, see §5.)
- **Row schema** (`quantlib/data/option_iv_store.py::OPTION_IV_SCHEMA`): underlying, date, occ, expiration,
  right, strike, dte, moneyness, spot, option_close, option_volume, rate, implied_vol, delta, gamma, vega,
  theta, iv_status (`ok`/`no_solution`), available_at. The inversion INPUTS are stored alongside the
  outputs so IV is auditable + re-derivable if the rate/model assumption changes.

Code (worktree → PR, NOT the live tree; fingerprint-neutral — pure data/research, no live feature def):
- `quantlib/data/option_iv_store.py` — layout + manifest + resume.
- `quantlib/data/option_iv_backfill.py` — discover → fetch bars → spot → BS-invert → write. CLI bounded by
  `--underlyings --start --end --moneyness-band --max-dte`.
- `tests/test_option_iv_inversion.py` — BS round-trip (4 vols × call/put to 1e-3), no-arb floor, greek
  signs, put-call parity. **12/12 pass.**

## 3. Bounded pilot — RESULT (proven end-to-end)

Launched as a guarded `quant-backfill` container (cpus2/mem8g, nice19/ionice idle, so `live_monitor`
pauses it under host pressure — never fc), writing to the live `fp_store_real:/store` `option_iv` namespace.

- **Scope:** SPY, QQQ, AAPL, NVDA, TSLA × **2024-12-02 → 2024-12-31** (21 trading days), moneyness ±15%,
  dte ≤ 60.
- **Result:** 105 partitions, **102,580 rows**, **94.3% IV-ok**, **5.25 MB total**, **~95 s wall**. Exit 0.
  (The ~6% no-solution are deep-OTM/illiquid contracts whose EOD mark sits below the model floor — expected;
  recorded as `iv_status=no_solution`, not silently dropped.)
- **Quality (the actual proof):** the IV smile is textbook (ATM ~0.10–0.17, rising wings); greeks are
  textbook (ATM call delta ~0.5, monotone in strike; vega peaks ATM); `available_at` = 21:00 UTC on the obs
  date. The **SPY ATM-IV (dte 20–40) daily time series** rises coherently 0.10 (calm early Dec) → 0.14 (end
  Dec) — a real point-in-time IV path that captures the term/level structure the VRP study needs.

| underlying | rows (Dec) | MB |
|---|---|---|
| SPY | 40,537 | 1.93 |
| QQQ | 37,744 | 1.81 |
| NVDA | 10,311 | 0.60 |
| TSLA | 8,744 | 0.54 |
| AAPL | 5,244 | 0.38 |

## 4. Reconstructed IV vs Alpaca's own live IV — known gaps (stated, not hidden)

The reconstruction is a daily-close BS inversion, NOT Alpaca's live snapshot IV. Differences to expect, and
how production should close them:
- **Daily close mark, not mid.** No historical option quotes exist, so we invert the bar CLOSE (last trade
  of the session), not a bid/ask mid. On illiquid contracts the close can be stale / off-mid → IV noise.
  Mitigation: weight by `option_volume`, prefer near-ATM (the pilot already bands moneyness), and treat the
  ATM-interpolated IV (not a single deep-OTM contract) as the panel signal.
- **Flat rate + no dividend/borrow.** The pilot uses a flat 4.5% and no dividend yield. For index ETFs the
  dividend matters at longer DTE. Production refinement: a term risk-free curve + per-underlying dividend
  yield (cheap, deterministic).
- **American vs European.** Single-name options (AAPL/NVDA/TSLA) are American; BS is the European
  approximation. For ATM short-DTE the early-exercise premium is small; flagged for the Modeller. Index ETF
  options (SPY/QQQ) are American too but very BS-close at ATM.

These are all REFINEMENTS, not blockers — the panel is already a usable VRP study substrate at the ATM
level (where the Modeller's thread lives).

## 5. Full-backfill plan + estimate

Storage/runtime scale linearly off the pilot (5.25 MB / 105 (underlying·day) partitions = **~50 KB per
(underlying, day)** at ±15% moneyness / dte≤60 daily; ETFs ~92 KB, single-names ~25 KB).

| Scenario | universe | window | est. (underlying·days) | est. storage | est. runtime |
|---|---|---|---|---|---|
| **A — VRP study (recommended first)** | ~30 liquid (SPY/QQQ/IWM + top single-names) | 2024-02 → present (~16 mo, ~340 td) | ~10,200 | **~0.7 GB** | ~2–3 h |
| B — broad | ~200 names | 16 mo | ~68,000 | ~4–5 GB | ~1 day (sharded) |
| C — minute granularity (intraday PIT) | ~30 names | 16 mo | — | ~50–100× daily → ~35–70 GB | multi-day |

**Recommendation:** run **Scenario A** — it's the exact substrate the surviving VRP thread needs, fits in
<1 GB (vs 1.9 TB free), completes in a few hours, and is daily-grain (matches the VRP study's horizon). Do
NOT jump to minute (C) or broad (B) until the Modeller's VRP study on the daily ~30-name panel shows the
premium is real and tradeable net of REAL option round-trip cost — same discipline as every other lane.

**Operational shape (mirrors the raw tape):** guarded `quant-backfill` container, `--processes 1` (the
pilot is network-bound, single-threaded suffices and stays well under the mem guard), manifest resume so a
restart never re-acquires, NVMe checked before launch. Reconcile = the manifest IS the reconcile surface
(partition-on-disk ⇔ manifest part). One-time launch, monitored to completion, `docker rm` after.

**Open decisions for Lead/Ben:**
1. Go on Scenario A (the ~30-name / 16-month / daily backfill)? Pick the underlying list.
2. Rate curve + dividend refinement now, or accept flat-rate for the first study (the pilot shows flat-rate
   ATM IV is already coherent)?
3. Whether to also capture a forward LIVE option-IV snapshot daily (the chain endpoint DOES give true
   IV/greeks live) so the panel extends forward with Alpaca's own IV, not just reconstruction — a small
   recurring job, complementary to the historical reconstruction.
