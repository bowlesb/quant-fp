# Clean-engine live integration plan (#73)

What it takes to make `CleanEngine` REPLACE the OLD feature engine in the live capture path
(`quantlib/features/capture.py`), now that the port is ~done (~58/64 groups ported + gated). This is the
code-exact successor to the high-level "Taking it live" sketch in `CLEAN_ENGINE_REWRITE.md` — the porting and
per-group correctness are proven; **the integration itself is the unproven part**, and this is the concrete
cutover. Scope only; execution is off-live behind a default-off flag on the branch, ahead of the Ben-gated
relaunch (fingerprint bump + downstream model recal).

## Where the OLD engine lives today (the seam)

`process_bars(...)` (capture.py:295) is the single per-minute compute→store core for BOTH the mock and the real
Alpaca feed. Per minute it:

1. Pushes this minute's bars into `state.ring` (a `MinuteRing`, capture.py:350-354) and `materialize()`s a
   polars `frame` (the trailing buffer as `(symbol, minute, <cols>)`).
2. Attaches reduction anchors (`attach_reduction_anchors`, :369) and builds `BatchContext(frames)`.
3. Selects groups via `runnable(frames)` (:372) — the registry's `FeatureGroup` objects whose inputs are
   present.
4. Computes each group: plain groups via `group.compute_latest(ctx)` (:386); `ReductionGroup`s bucketed by
   `reduce_input` and run through a per-bucket `IncrementalEngine` (`_engine_for(...).step(frame)`, :405) for the
   `incremental_safe` ones, batch fresh-sum for the unsafe (:415).
5. Writes one `(symbol, minute)` row per present symbol per group (`store.write_group` / `state.writer.submit`,
   :437-447) and optionally publishes to the bus.

So the OLD engine is really TWO things in this path: (a) `compute_latest(ctx)` per plain group, and (b) the
per-bucket `IncrementalEngine` for the reduction groups. `CleanEngine` replaces BOTH with one `engine.emit(...)`.

## The target seam (what clean replaces)

`CleanEngine.emit(minute_bars)` (clean_engine.py:269) is the drop-in: it folds one minute (`step`) and returns
`(present_symbols, {group: {feature: (n_symbols,) array}})` already NaN'd outside the present mask — i.e. it
already IS the present-row-emission boundary (#57). The cutover replaces steps 3-4 above with a single
`engine.emit(...)` call, then reshapes its numpy output back into the per-group `(symbol, minute)` polars frames
the write loop (step 5) already consumes. The write/bus/timing tail (:421-455) is UNCHANGED.

## What stays vs changes

STAYS (untouched):
- `MinuteRing` push/materialize, warm-start (`warm_start_ring`), the bar-normalization parity boundary
  (`_bars_to_frame`), `attach_reduction_anchors` (for the daily volume-anchor groups still reading it),
  snapshot-holding (`state.snapshots`).
- The write loop + `StoreWriter` + bus hook + Prometheus group timings (:421-455) — clean's per-group frames
  feed it identically.
- The trust/cert system — it grades the new engine's output exactly as today.

CHANGES (the integration work):
- The compute middle (capture.py :372-419) — group selection + `compute_latest` + the `IncrementalEngine`
  buckets — is replaced by one `CleanEngine.emit(...)` + a numpy→polars reshape.
- `CaptureState` gains a held `CleanEngine` (one per `reduce_input`/shard bucket, like `state.engines` today),
  seeded once from the warm-start ring.

## The five real integration gaps (the unproven part)

These are NOT correctness-of-math (that's gated per group) — they're the wiring the per-group tests never exercised:

1. **No canonical "all clean groups" list.** The suite instantiates groups ad-hoc per test; there is no registry
   analogue of `runnable(frames)` for the clean `EngineGroup`s. DELIVERABLE: an authoritative
   `ALL_CLEAN_GROUPS` (or a `clean_registry`) that lists every ported `EngineGroup` instance, with the
   legacy→clean name map (the `_LEGACY_GROUP_OF` the completeness gate already half-builds in tests). This is the
   single source the live path constructs the engine from.

2. **Fixed symbol universe vs dynamic live universe.** `CleanEngine(symbols, ...)` builds a FIXED symbol index at
   construction (`RingBuffer.index[s]`, clean_engine.py:73); `_marshal` does `self.ring.index[s]` → **KeyError on
   an unknown symbol**. The live universe changes daily (new listings, universe rebalances) and a shard's symbol
   set is fixed per session but differs across sessions. DELIVERABLE: rebuild the engine at each SESSION boundary
   with that session's universe (the natural lifecycle — daily snapshots already reload per session), seeded from
   the warm-start ring. Decide: rebuild-per-session (simplest, matches the snapshot lifecycle) vs a growable ring
   (more complex, unneeded if the universe is fixed within a session). RECOMMEND rebuild-per-session.

3. **Input shape: polars frame → numpy minute dict.** The OLD path hands groups a `BatchContext(frames)`; clean
   `step` takes `{"symbol": arr, "<col>": arr, "minute_epoch": arr}`. DELIVERABLE: a thin marshal from THIS
   minute's bars (already in hand as `new_frame`/`bars`) to the minute dict — NOT from the materialized buffer
   (the engine carries its own buffer). The bar columns a group reads come from its `input_cols`; the union over
   all groups is the engine's `cols`.

4. **Session population (#69, the sibling blocker) — a TRANSFORM, not net-new wiring.** The daily-snapshot +
   event-tape groups read `window.session`. Nothing populates these live yet — `set_session` is called only by
   tests. BUT the SOURCE already exists and is already loaded session-scoped: `real_capture.py:149-163` builds
   `snapshots` ONCE at session startup — `snapshots["daily"] = backfill_daily(day, symbols)` (split-adjusted daily
   bars `(symbol,date,o,h,l,c,v,vwap)`, trailing 370 calendar days ENDING AT `day` → today's daily bar IS
   included = the `(a)` [-1]=today source confirmed at the data layer), plus `load_news_features(day)` /
   `load_filings(day)` / `load_reference` / `load_universe`, all flowing into `process_bars(... snapshots)` today.
   So #69 is NOT a new data path — it is a per-session PIVOT of the already-held polars frames into the clean
   engine's numpy session layout (the `(n_sym, n_days)` daily matrices + the news/edgar CSR `at/off/payload`
   arrays), then `engine.set_session(...)` at the rebuild-per-session boundary (gap 2). The matrix/CSR layout +
   the `(a)` convention + the parity math are DONE and gated; #69 is ~one pivot function from the held frames.
   Until #69 lands, the session groups emit NaN defaults live (honest, not wrong) — so clean can go live for the
   non-session groups FIRST and the session groups light up when #69 lands.

5. **Enriched derived bar columns (the real input-contract dependency).** VERIFIED by sweeping every clean
   group's `input_cols`: NO clean group reads the legacy reduction-anchor / centered-std column — the windowed
   ports (e.g. `PriceVolumeClean` = `high/low/close/volume`) compute their centering INTERNALLY from raw OHLCV
   (the port-the-batch-math principle). What clean groups DO need beyond raw OHLCV is the per-minute ENRICHED
   columns the reader already produces for both live and backfill (`materialize.py:108` / `capture.py:42`
   enrich `minute_agg` with `n_trades, signed_volume, spread, imbalance, mean_spread_bps, …`):
   `PriceVolumeClean` reads `signed_volume`; `QuoteSpreadClean` reads `mean_spread_bps/quote_imbalance/
   mean_bid_size/mean_ask_size`; the #63 tick-tape groups will read their derived `_hhi/_gap_fano/bin-count`
   columns (same input-contract class — the worker populates them, like `daily_open`). DELIVERABLE: the minute
   marshal (gap 3) must carry the FULL enriched column set (union of every group's `input_cols`), not just raw
   OHLCV; the enrich step is unchanged and already a shared live+backfill boundary, so this is parity-neutral.
   The original "reduction-anchor parity" worry does NOT apply — there is no anchor column to thread.

## Cutover mechanics (flag-gated, reversible)

- Add `FP_CLEAN_ENGINE=1` (default off). When off, `process_bars` is byte-identical to today (zero deploy risk).
- When on, `process_bars` builds/holds a `CleanEngine` on `CaptureState` (rebuilt per session, gap 2), marshals
  the minute dict (gap 3), calls `emit(...)`, reshapes to per-group `(symbol, minute)` frames, and feeds the
  UNCHANGED write/bus tail. The OLD `compute_latest`/`IncrementalEngine` branch is skipped.
- Soak: arm on ONE canary shard under the existing deploy seam; diff its store output against a non-armed
  shard's for the same symbols/minutes (the live analogue of the per-group golden — CP owns this gate, #52).
  Then widen shard-by-shard.
- Roll back = flip the flag off (no data migration; the store rows are the same schema).

## Order of work (off-live, branch-only, each gated)

1. `ALL_CLEAN_GROUPS` registry + legacy→clean name map (gap 1) — unblocks everything; no behavior change.
2. The minute-dict marshal + numpy→polars reshape helpers (gap 3) — pure plumbing, unit-testable in isolation.
3. The flag-gated `CleanEngine` branch in `process_bars` with per-session rebuild (gaps 2, 5) — the cutover code,
   default-off.
4. Session population (#69, gap 4) — daily matrices + event tapes; lights up the session groups.
5. CP's live-path gate (#52): canary-shard store-diff vs the OLD engine on a real session, off-line.
6. Ben-gated arm: canary shard live, soak, widen. (Cert retirement, if ever, is a SEPARATE later decision.)

## Honest status

Steps 1-3 are pure off-live plumbing I can execute now behind the default-off flag with zero live risk; step 4
is #69; step 5 is CP's gate; step 6 is Ben's call. The math is proven per group — what this plan de-risks is the
WIRING (the registry, the dynamic universe, the input marshal, the session builder), which no per-group test
touched. Nothing here arms anything; `fc` keeps running the OLD engine until Ben greenlights step 6.
