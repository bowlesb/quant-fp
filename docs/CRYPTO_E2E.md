# Crypto End-to-End Rehearsal — the off-hours full-pipeline exercise

The 24/7 crypto stream (`crypto-capture`, PR #154) lets us exercise the WHOLE production machinery —
capture → feature compute → parity → trust → within-day cert → WDPC continuous-deploy — on a LIVE feed at
any hour, instead of waiting for the Monday equity open. This doc is the architecture (what is shared vs
separated, partitioned by asset class) and the PHASED plan to get from "crypto features stream to a bus" to
"a crypto WDPC hot-swap on the live crypto fc", with every stage cleanly isolated from equity.

This is a REHEARSAL harness, not a crypto trading product. Its value is that it drives the same code the
equity pipeline runs, so a defect in parity/trust/within-day/WDPC surfaces off-hours on crypto first.

---

## 1. The separation principle — SHARED infrastructure, SEPARATED data (by asset class)

The design rule: ONE engine and ONE set of machinery (parity / trust / within-day / WDPC), but the DATA each
asset class flows through is partitioned so crypto and equity can NEVER cross-contaminate. A bug in crypto
trust must not move an equity trust grade, and vice versa.

| Concern | SHARED (one implementation) | SEPARATED (per asset class) |
|---|---|---|
| Feature compute | `capture.process_bars`, `materialize._write_all`, the registry, every universal group | which groups RUN (crypto self-selects the universal bar/trade groups; equity-only groups skip by construction) |
| Parity compare | `compare.cell_verdict` / `match_predicate`, the group tolerances | the SWEEP that drives it (`validation_sweep` for equity, `crypto_validation_sweep` for crypto) |
| Trust grading | `trust_lifecycle.clean_feature_day` / `lifecycle_state`, the grading thresholds | the trust LEDGER row (equity `feature_trust`; crypto `crypto_feature_trust`, keyed by asset_class) |
| Within-day cert | `within_day_parity.compare_window`, settle-lag logic | the cert LEDGER (equity `within_day_parity_cert`; crypto `crypto_within_day_parity_cert`) |
| WDPC deploy | the assignment lock + FIFO queue + scope-guard + applier | the live fc the swap targets (equity `feature-computer` vs the crypto fc) |
| Store | `store.write_group` / `get_features` primitives, the partition layout | the STORE ROOT (equity `/store`; crypto `fp_store_crypto` volume) |
| Bus | `BusPublisher`, the codec | the NAMESPACE (`fv:<sym>` equity; `fv:crypto:<sym>`) |
| Universe | the symbol-discovery path | the symbol SET (equity ~11k; crypto a handful of liquid pairs) |
| Container | the `fp-dev` image (Rust kernels) | the CONTAINER (`feature-computer` vs `crypto-capture`) |

> **The store separation is by ROOT, NOT by a source tag.** crypto-capture runs `mode="real"`, so via
> `store.source_for_mode` it writes `source=stream` — the SAME source tag equity writes. Crypto is isolated
> from equity ONLY because it writes to a different store ROOT (the `fp_store_crypto` volume) and a different
> bus namespace (`fv:crypto:*`); there is NO `source=crypto` tag. (The `crypto_capture` module docstring's
> "crypto source marker" line is aspirational — the code does not set one.) So the crypto sweep reads
> `source=stream` / `source=backfill` from the CRYPTO root, exactly like the equity sweep reads from `/store`.
> Do NOT rely on a source tag to tell crypto from equity — use the store root.

### Why a SEPARATE crypto trust ledger (not an `asset_class` column on `feature_trust`)

The equity trust model is keyed `(feature, version)` GLOBALLY — `feature_trust`, `feature_parity_defect`,
`within_day_parity_cert`, `stream_symbol_day_cleanliness` all have no asset dimension. A crypto grant written
into `feature_trust` would COLLIDE with the equity grant for the same feature name (e.g. `volume_zscore_1m`):
trusting it on crypto would auto-trust it on equity even though they are computed over different universes,
calendars, and tape density, with genuinely different parity profiles.

Two ways to separate:

* **(A) add `asset_class` to the PK of every equity trust table.** Correct long-term, but it touches the
  equity PK and every reader (`selective_backfill`, `feature_data`, `feature_grid`, `trusted_features` view,
  the dashboard) — a fingerprint-adjacent, equity-behavior-touching migration. NOT a one-cycle, zero-equity-
  risk change.
* **(B) a SEPARATE crypto-namespaced ledger** (`crypto_feature_trust`, `crypto_within_day_parity_cert`),
  additive (`CREATE TABLE IF NOT EXISTS`), carrying an explicit `asset_class` column. Zero equity behavior
  change by construction (equity never reads these tables). The crypto sweep writes here; the equity sweep is
  untouched.

**This rehearsal takes (B).** It is the cleanest expression of the separation principle (the ledgers are
physically distinct, so cross-contamination is impossible, not merely guarded), and it is purely additive.
If/when crypto graduates from a rehearsal to a first-class asset class, (A) is the consolidation — fold the
two ledgers into one `asset_class`-keyed table — but that is a deliberate later migration, not the off-hours
rehearsal's job.

---

## 2. The exact crypto feature set (which universal groups RUN on crypto)

Crypto capture (`crypto_capture.run_crypto_capture`) feeds `process_bars` ONLY the `minute_agg` (+ `trades`)
frames and NO `reference` / `daily` / `universe` snapshots. `runnable(frames)` therefore self-selects exactly
the groups whose inputs are present, and `EXCLUDED_GROUPS = ("market_context", "market_beta")` removes the two
index-relative groups that key off a SPY row (there is no crypto equity index). The crypto feature set is thus
defined BY CONSTRUCTION, not by an allow-list:

* **RUNS on crypto** — the universal bar/trade/microstructure groups: price/return, volatility, range,
  volume (see caveat), candlestick, order-flow (`trade_flow`, `signed_trade_ratio`, `inter_arrival`,
  `large_print_burst`, `microstructure_burst`, `tick_runlength`, `trade_size_dist`), and the
  cross-sectional reduce groups over the crypto symbol set (`cross_sectional_rank`; `breadth` self-skips
  without `reference`/`daily`).
* **SKIPS on crypto by construction** (no `reference`/`daily`/`universe` snapshot, or explicitly excluded):
  `sector_*`, `breadth` (needs `daily`), `peer_relative`, the multi-day groups (`prior_day`,
  `overnight_*`, `gap_fill_state`), and `market_context` / `market_beta` (explicitly excluded).
* **Quote groups stay honest-null** — crypto has no NBBO quote stream here, so `quote_spread` and the
  quote-derived columns emit null (not zero), which is correct.

**OPEN COORDINATION ITEM — the `volume` group anchor.** `volume` uses a daily-snapshot reduction anchor
(volume centering); crypto passes no `daily` snapshot, so the anchor is absent. Whether `volume` still RUNS
(or self-skips / emits without centering) on crypto post the anchor work (#332) is owned by agent
**AnchorTestFix** — NOT resolved here. The crypto sweep grades whatever groups actually materialize; if
`volume` is ambiguous it is graded honestly (or absent) and flagged, never special-cased. The authoritative
crypto group set on any given day is therefore "the set `runnable` self-selects from the crypto frames minus
`EXCLUDED_GROUPS`" — query it at sweep time, do not hard-code it.

---

## 3. The crypto parity test — what "backfill" means with NO raw tape

The equity sweep compares `source=stream` (live) against `source=backfill` (re-materialized from `/store/raw`
via `materialize_from_raw`). **Crypto has no raw tape and no equity-style backfill acquisition**
(`raw_backfill` is equity-only: `StockHistoricalDataClient`, dollar-volume ADV ranking, ETF screen). So the
crypto "backfill side" is produced differently, and this is the honest core of the rehearsal:

> The crypto backfill side is RECOMPUTED from the SAME stored `minute_agg` (OHLCV + aggregated-tick) inputs
> the live path consumed, through the identical batch `materialize._write_all` path, written to
> `source=backfill`. Comparing `source=stream` vs this `source=backfill` is a genuine
> **live-emit-vs-batch-recompute parity test** — it catches exactly the live-path divergences the equity
> sweep catches (incremental-vs-batch fold drift, `compute_latest`-vs-`compute` corner cases, warmup/null
> handling) — minus an INDEPENDENT data acquisition (there is no second source of the crypto tape to cross-
> check against; the input frame is the one the live feed delivered).

This requires PERSISTING the crypto `minute_agg` inputs (the live path computes features from them but did not
store them). The rehearsal adds an additive crypto INPUT store (`source=input` partitions under the crypto
root) so the backfill recompute is possible. The limitation is stated plainly: crypto trust certifies
**emit==recompute parity on the captured tape**, NOT independent-source agreement. That is still a real and
useful certification (it is the bulk of what equity parity tests), and it is what makes the off-hours
rehearsal meaningful. A future "genuine off-hours trust surface" (Alpaca `CryptoHistorical` → an independent
crypto raw tape) is the stronger version and is tracked as a later phase, owned by DataIntegrity.

### The fingerprint-currency requirement (crypto-capture must run CURRENT code)

The crypto bus fingerprint is NOT a universal-subset fingerprint computed separately — `crypto_capture` uses
`BusPublisher(prefix="fv:crypto")` with the DEFAULT schema (`default_schema()` = the FULL registry
`BusSchema.from_registry`), so crypto stamps the SAME registry-authoritative fingerprint equity does, frozen
at the crypto-capture PROCESS START (Python imports the registry once). The bind-mount (`/home/ben/quant-fp` →
`/app`, like fc) is `rw` to the live tree, but a running process does NOT pick up tree changes — only a
relaunch reloads the registry.

**Finding (2026-06-21, investigated read-only at the Lead's flag):** crypto-capture started 06-19 23:30Z,
which is `858162d` (#175) — BEFORE the 728 deploy (#213 + the #254/#270 swing_dc re-land/de-stage + the
registry-affecting group commits landed AFTER). Verified by computing `default_schema().fingerprint` at both
commits in fp-dev: at `858162d` it is `0xae849d400c909972 / 694` (exactly what CryptoStrategy decoded off the
crypto bus); at current HEAD it is `0x873f2fceb8f00c92 / 728`. So **crypto-capture is STALE** — running the
old 694 registry because it was never relaunched since 728 landed. NOT 694-by-design.

**Consequence for this rehearsal:** the crypto parity sweep must certify a CURRENT feature set, not a stale
one. The OFFLINE slice (this PR) is unaffected — it recomputes the backfill side from the SAME stored inputs
through whatever registry the SWEEP runs (current tree in the sandbox), so emit==recompute holds regardless of
the live container's age. But to make the LIVE crypto stream a faithful current-fingerprint surface (and
before Phase 2's live crypto WDPC), crypto-capture needs a **controlled relaunch on the current tree** so it
computes 728. This is a Lead-gated click (golden rule: relaunch only via the sanctioned path, never
`docker restart`); tracked in the READINESS ledger as "crypto-capture CODE CURRENCY". The fingerprint must be
well-defined AND current, else the rehearsal certifies a stale set.

---

## 4. The phased plan

### Phase 1 — OFFLINE rehearsal (no live-container touch) — THIS WORKSTREAM

Exercise parity → trust → within-day on crypto data, in a `--rm fp-dev` sandbox against the crypto store,
writing crypto-namespaced ledgers. NO live container is started/stopped/relaunched.

1. **Crypto-namespaced ledgers** — `db/init/15_crypto_trust.sql`: `crypto_feature_trust` +
   `crypto_within_day_parity_cert` + `crypto_trust_check`, each carrying `asset_class`, additive
   (`CREATE TABLE IF NOT EXISTS`), zero equity impact. **(shipped this slice)**
2. **Crypto input persistence** — additive `source=input` write in the crypto capture path (env-gated so it
   is opt-in; the live container picks it up only on the Lead's sanctioned relaunch) so a backfill recompute
   has the inputs it needs. **(shipped this slice — write path + helper; live relaunch is the Lead's call)**
3. **The crypto parity sweep** — `quantlib/features/crypto_validation_sweep.py`: discover crypto stream
   symbols → recompute `source=backfill` from the stored inputs → compare cell-for-cell with the existing
   `compare`/`cleanliness`/`trust_lifecycle` primitives → write crypto trust grades. Reuses the equity
   grading logic; replaces only the equity-specific seams (no raw-settle gates — crypto is 24/7; no
   market-ticker pin; UTC-day instead of NYSE calendar). **(shipped this slice — see §5 for what is in the
   first slice vs deferred)**
4. **Crypto within-day cert** — run `within_day_parity.compare_window` on a settled crypto window and stamp
   `crypto_within_day_parity_cert`, proving the within-day tables update on crypto data. **(NEXT — designed
   here, thin wrapper over the same compare; deferred to the next cycle to keep this slice small)**

### Phase 2 — LIVE WDPC hot-swap on the crypto fc (escalation, gated)

Once Phase 1 is solid AND the equity WDPC live-wiring (#329, currently HELD on the RunningState
`up_to_date()` contract) has landed, escalate to a real continuous-deploy on the ISOLATED crypto fc:

1. A crypto-scoped WDPC applier targets `crypto-capture` (not `feature-computer`), using the SAME assignment
   lock + FIFO queue + scope-guard, but with the crypto store/bus and the crypto trust ledger as its trust
   source.
2. A feature-group fix for a UNIVERSAL group is enqueued → scope-guard (fp-neutral / single-group /
   crypto-trusted) → hot-swap the crypto fc group → bus tripwire on `fv:crypto:*` → rollback on tripwire fail.
3. Because crypto is isolated (own container/store/bus), a bad crypto hot-swap can NEVER dent equity capture —
   so the crypto fc is the SAFE first place to exercise live WDPC. A clean crypto WDPC cycle is the evidence
   the equity WDPC live-wiring is ready.

**The escalation is gated** on: (a) Phase 1 crypto parity/trust/within-day green; (b) #329 RunningState
contract landed (the applier's `up_to_date()` base); (c) the GOLDEN RULE — the crypto fc is relaunched ONLY
via `ops/nightly_relaunch.sh` (never `docker restart/start`), and never `docker kill --filter ancestor=fp-dev`
(crypto-capture + fc + strategies + sandboxes share `fp-dev`).

---

## 5. This slice — what shipped and the next step

**Shipped (Phase 1, steps 1–3 core):**

* `db/init/15_crypto_trust.sql` — the crypto-namespaced trust + cert ledgers (`asset_class`, additive).
* `quantlib/features/crypto_input_store.py` — the additive crypto `source=input` minute_agg persistence +
  loader, and the env-gated hook the crypto capture path calls.
* `quantlib/features/crypto_validation_sweep.py` — the crypto parity sweep: recompute backfill from stored
  inputs, compare stream-vs-backfill via the existing primitives, grade cleanliness, write
  `crypto_feature_trust` grades keyed `asset_class='crypto'`. UTC-day, no raw-settle gate, no market pin.
* Unit tests proving the mechanism end-to-end on synthetic crypto data (input persist → recompute →
  compare → grade → crypto trust written), separated from equity (own tables, own store root).

**The first slice deliberately scopes to:** the per-symbol bar/trade groups on a small crypto symbol set,
recompute-vs-stream parity, and the crypto trust write. The cross-sectional reduce grade and the within-day
cert stamp are documented above as the next steps (thin wrappers over the same compare) and follow in the next
cycle.

**Next concrete step toward live WDPC:** run the crypto within-day cert (`compare_window` → stamp
`crypto_within_day_parity_cert`) on a settled crypto window to prove the within-day tables update on live
crypto data; then, once #329's RunningState contract lands, stand up the crypto-scoped WDPC applier (Phase 2)
against the isolated crypto fc.
