# Strategy-Harness Dogfood — usability report

**What this is:** a deliberate end-to-end USE of the `quantlib/harness/` strategy harness
(`run_strategy(config) -> StrategyReport`, PR #237) on one simple tradeable idea, at Ben's
request. The point is **usability feedback, not an edge** — any edge result is a bonus. Run as
the Modeller, bounded `--rm fp-dev`, `/store` read-only, off `origin/main` 6671f6b. No
fingerprint / quantlib-live / registry change.

The raw chronological friction notes are in `FRICTION_LOG.md`; this README is the synthesized,
prioritized deliverable.

---

## (a) The idea, the config, the result vs both baselines

### Primary run — the documented DAILY demo path (multi-feature ridge), reproduced end-to-end

The cleanest "does the engine work as advertised" path. Trusted-liquid daily panel,
**2025-12-01..2026-06-17**, top-500-ADV (496 symbols, 13 trailing features, 57,265 rows), forward-1-day
cross-sectional excess label, dollar-neutral L/S, **$1M** book, net of per-name half-spread + 1bp
slippage + 50bp borrow.

```bash
python -m quantlib.harness --model ridge --label-horizon-days 1 --frac 0.10 \
  --capital 1000000 --universe-top 500 --out /app/experiments/2026-06-21-harness-dogfood/out/ridge
```

Warm runtimes: ridge **1.1s** / gbm **8.3s** / composite **1.4s** (after the cold cache build — see
friction #4). Ridge 10%-basket: **+24.2% net on $1M, Sharpe 1.34**.

**The threshold curve (the headline) vs the shuffle baseline — it dominates at every cut:**

| cut | precision | $/trade | total $ P&L | Sharpe | shuffle $ P&L | shuffle Sharpe |
|----|----|----|----|----|----|----|
| 1%  | 0.5065 | **$714** | $546,995 | 1.66 | -$102,799 | -0.84 |
| 2%  | 0.5104 | $235 | $405,082 | 1.76 | $11,405 | 0.14 |
| 10% | 0.5065 | $22 | $210,104 | 1.34 | -$61,023 | -1.57 |
| 50% | 0.5050 | $1 | $42,697 | 0.59 | -$33,535 | -1.74 |

$/trade and total-$ rise monotonically as the cut tightens; the real curve beats shuffle (AUC 0.5026,
rank-IC 0.001 — clean ~chance) at every cut; predict-zero = $0. This is exactly the documented
"real-but-tail-concentrated" shape — the engine behaves correctly and the diagnostics are legible.

### Bonus run — the single-feature idea (the actual dogfood "pick one idea")

**Idea:** short-horizon cross-sectional behavior of ONE trusted feature `price_returns.ret_5m` — does
the last-5-min return predict the next-30-min cross-sectional *excess* return, and which sign? I let
the harness report the sign + the money rather than assume momentum vs reversal.

```bash
python -m quantlib.harness --cadence intraday \
  --intraday-groups '{"price_returns": ["ret_5m"]}' \
  --label-horizon-min 30 --date-start 2026-04-15 --date-end 2026-06-17 \
  --model composite --frac 0.10 --capital 1000000 --universe-top 500 --out .../out/intraday_ret5m_composite
```

(`composite` with one feature IS "rank by ret_5m" — the purest single-feature test.)

**Result: I could not get a number out in a reasonable time — I stopped it after ~40 minutes.** This
single feature, single group, 46-date intraday run was still going (steady ~2GiB RSS, ~270% CPU) when
I SIGTERM'd my own sandbox container to stop hogging the box — vs ~1s for the daily multi-feature run.
The runtime is dominated by `build_intraday_panel` (per-date store join + concat) and the pure-Python
diagnostics over the large minute-level panel. **This non-result IS the finding** (friction #8, #9):
the harness is fast and pleasant on the cached daily path and
effectively unusable for an ad-hoc intraday single-feature idea — which is precisely the "test one
trusted feature" workflow the dogfood was meant to exercise. This matches the ledger's standing
`[Modeller/Latency]` gap (build_intraday_panel at scope, the "<30s all-tickers" bar unproven).

---

## (b) Prioritized harness-usability improvements

Ordered by leverage. Effort is rough (S < 1h, M a few h, L a day+).

1. **Make the documented "just run it" actually run on a fresh checkout — bundle a tiny cache OR a
   one-command cache-build, and document the store mount.** (Friction #1,#3,#4) **Effort: M.**
   Removes the entire cold-start cliff: the example cache path doesn't exist in the main tree, nothing
   says the store is a docker volume that must be `-v fp_store_real:/store:ro`, and the uncached first
   run is ~3min silent. Ship `make harness-demo` (mounts the volume, builds the cache once with a
   progress print, runs ridge) and a `docs` line: "first run builds a ~22MB cache from /store in
   ~3min; subsequent runs are ~1s." Optionally commit a small sample cache so the pure-Python example
   works with NO store at all.

2. **Actionable error on the empty raw-bar glob.** (Friction #2) **Effort: S — SHIPPED below.**
   Replaced the opaque `ValueError: cannot concat empty list` with a `FileNotFoundError` naming STORE,
   the date count, and the mount hint. (PR-ready, test added.)

3. **A `--daily-features a,b,c` knob so a single daily feature is testable without editing source.**
   (Friction #6) **Effort: M.** Today only the intraday path takes a feature subset; the daily panel
   hard-codes all 13 `DAILY_FEATURE_COLS`. The dogfood task literally asks to "test ONE trusted
   feature" and the daily path can't express it. Add `feature_subset: tuple[str,...] | None` to
   `HarnessConfig` (filter `panel.feature_names` / the matrix columns after the panel build) + the CLI
   flag. This also makes the cheap, fast daily path the natural home for single-feature screens
   (sidestepping the slow intraday build entirely).

4. **Cache/persist the intraday panel + make the build emit progress.** (Friction #8,#9) **Effort: L.**
   The intraday path has no `--intraday-cache` equivalent of the daily cache, re-globs/re-joins the
   store every run, and prints nothing while doing it. Add a per-(groups,range,universe) parquet cache
   + a `reducing date N/total` line. This is the single biggest blocker to using the harness for
   intraday ideas and overlaps the existing Latency-gap to fix `build_intraday_panel` at scope.

5. **Don't lose `--out` artifacts in the `--rm` container; default `--out` under `/app` or warn.**
   (Friction #5) **Effort: S.** `--out /tmp/...` (the path the docs example uses) writes inside the
   ephemeral container and vanishes. Either default `--out` to a repo-relative dir, or detect a
   non-`/app` out-dir and print "this path is inside the container; mount it or use /app/... to keep
   the report." A 3-line guard saves a confusing "where did my report go?".

6. **Vectorize (or numpy-ify) the threshold-curve / AUC diagnostics.** (Friction #9) **Effort: M.**
   `diagnostics.py` is pure-Python dict-bucketing + per-row loops over the whole panel, run once for
   the real curve and again for shuffle. On the daily panel it's invisible; on the intraday panel it's
   a large fraction of the (already huge) wall-clock. Bucketing by timestamp + the rank/AUC math are
   straightforwardly columnar in polars/numpy.

7. **Add a README to `quantlib/harness/` and reconcile the cache-path examples.** (Friction #7)
   **Effort: S.** The module has no README (the battery does); the cache path is relative in
   `docs/STRATEGY_HARNESS.md` but absolute `/app/...` in the battery README — copy-pasting between them
   breaks depending on cwd. One short README + one consistent path.

---

## (c) The single highest-leverage improvement

**#1 — make the documented example run on a fresh checkout (bundled/one-command cache + store-mount
docs + the empty-glob error).** Every other improvement assumes the user got *a* run to complete; this
is the gate they hit in the first 60 seconds, and right now the published "just run it" example
fails three different ways (missing cache file, unmentioned docker-volume store mount, 3-min silent
cold build) before producing the opaque `cannot concat empty list`. Fixing the on-ramp is what turns
the harness from "works if you already know the incantation" into "a new modeller can actually use it."
I shipped the cheap half of this (the actionable error, #2); the `make harness-demo` + docs half is the
follow-up I'd do first.

---

## Shipped from this dogfood

- **Actionable empty-store error** (`quantlib/battery/panel.py` `build_daily_table`) + a focused unit
  test (`tests/battery/test_panel_empty_store.py`). Replaces `cannot concat empty list` with a
  `FileNotFoundError` that names STORE, the requested date count, the mount hint, and the missing
  cache path. `tests/battery/ + tests/harness/` = 17 passed with the change in.

## Artifacts

- `out/{ridge,gbm,composite}/` — the daily demo reports (report.md / equity_curve.csv /
  threshold_curve.csv / report.json), persisted via an `--out` mounted under `/app`.
- `FRICTION_LOG.md` — the raw chronological friction notes.
