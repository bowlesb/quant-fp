# Parity-Validation Lifecycle

The scheduled job + trust state machine that proves every **live** feature (`source=stream`, written by
`compute_latest` during capture) is cell-for-cell equal to its **backfill** twin (`source=backfill`,
written by `compute` during materialize) — and **quarantines** the ones that aren't, with evidence, so
downstream code (ML, strategy containers) can gate on a feature's trust grade.

This layers ADDITIVELY on the existing validation ledger (`docs/VALIDATION_LEDGER.md`,
`db/init/07_feature_validation.sql`). The nightly sweep still writes the same per-cell comparison into
`feature_validation_day` / `feature_validation_exception`; this lifecycle adds a **contamination-aware
grade**, a **state machine**, and a **defect backlog** on top.

## The core correctness problem: contamination

A windowed/breadth feature (e.g. `volume_zscore_30m`, a 30-minute rolling stat) is computed *correctly*
by both `compute_latest` (live) and `compute` (backfill). But on a day where live capture **restarted
mid-session**, the live stream is missing a block of minutes. Backfill reads the complete tape, so for
the minutes after the gap the two windows see different inputs and the feature legitimately diverges —
**not because the feature logic is wrong, but because the live DATA was lost.**

Grading that day as a parity FAILURE would condemn a correct feature. So we must first decide, per
`(symbol, day)`, whether the live capture was **CLEAN** enough to be a fair parity test, and grade each
feature over **CLEAN comparisons only**.

### Session scope — extended hours are NOT a contamination signal

We capture **pre-market** (~08:00 UTC / 04:00 ET), the **regular session** (13:30–20:00 UTC / 09:30–16:00
ET), and **post-market** (to ~24:00 UTC). A full liquid-name day is **~850+ minutes, not 390**, and
extended-hours minutes are legitimately **sparse** — an illiquid name may print few or zero pre/post-market
bars, and a minute with no trade simply has no bar (normal, not a gap). Requiring full-day contiguous
coverage would wrongly flag almost every symbol-day.

So the cleanliness check is scoped to the **regular session only** (`rth_mask`, ~390 minutes), which IS
dense for any actively traded name. Extended-hours coverage is bonus, never a contamination signal.

### The cleanliness heuristic (`quantlib/features/cleanliness.py`)

Within the regular session, a capture restart is what we must catch: the live stream loses an **internal
block** of minutes that backfill (the complete tape) had, so post-gap windows legitimately diverge. We
measure that **relative to backfill**:

1. **No internal missing run** (the primary signal) — the longest contiguous run of regular-session
   minutes that **backfill produced but the stream did not** must be ≤ **5 minutes**. A restart leaves a
   hole > this; a single missed print does not. A thin name with few-but-fully-matched backfill minutes
   has **no** miss run relative to backfill, so it passes trivially — the fair reference is "what truth
   had", never a flat 390.
2. **Coverage floor** (a permissive secondary signal) — distinct regular-session stream minutes ≥ **90%**
   of the minutes backfill produced. Catches a stream that is sparse *everywhere* vs a dense backfill
   (capture started late and never caught up) without one long internal run.

Failing either → **CONTAMINATED**: the day's comparisons for that symbol are recorded but **excluded
from the grade**. Per-symbol verdicts are persisted to `stream_symbol_day_cleanliness` (the audit trail).

### Clean-breadth floor (the sweep)

A day must have ≥ **`MIN_CLEAN_SYMBOLS` (= 20)** clean symbols to contribute a clean-day grade at all.
Grading off one or two marginal survivors of a contaminated day is noise — a single thin name's
near-zero-denominator relative errors masquerade as failures and would file hundreds of spurious defects.
Below the floor the day yields **no** clean comparison and every feature stays **PENDING** for it (no
defects filed). A normal day has thousands of clean liquid names, so this only ever suppresses
pathologically contaminated days.

## The trust state machine (`quantlib/features/trust_lifecycle.py`)

Per feature (additive columns on `feature_trust`: `lifecycle_state`, `clean_days`, `clean_days_passed`,
`clean_value_rate`). A clean `(feature, symbol, day)` comparison **passes** iff its match rate over
compared cells ≥ **0.999**.

| State | Meaning | Transition in |
|-------|---------|---------------|
| `PENDING`   | Not enough clean days yet (`< MIN_CLEAN_DAYS = 2`). Not proven; do not trust. | default |
| `VALIDATED` | ≥ 2 clean days AND parity held on **every** clean day. Live compute reproduces backfill. | enough clean days, no clean-day failure |
| `DIVERGENT` | Parity **failed on at least one CLEAN day** — a real `compute_latest != compute` bug. **Quarantined** (kept + collected, marked untrusted) and a defect is filed. | any clean-day failure |
| `RETIRED`   | Terminal, set **manually** (last resort). Never auto-recomputed back to a live state. | operator only |

Key guarantees:

- **DIVERGENT comes only from clean-day failures.** A feature that fails *only* on contaminated days
  never enters the clean history as a failure, so it is never condemned — exactly the contamination
  isolation we want.
- **DIVERGENT wins over PENDING.** A single clean-day failure files a defect even before
  `MIN_CLEAN_DAYS` is reached (we don't wait to flag a known-broken feature).
- The state is a **pure recompute** over the persisted clean history each run (idempotent, self-healing;
  re-running a day refreshes in place).

## Quarantine + the parity-defect backlog (`feature_parity_defect`)

Divergence policy = **QUARANTINE + INVESTIGATE**, never silently delete. When a feature becomes
`DIVERGENT` it keeps being computed/collected and is marked untrusted, and an **OPEN** row is upserted
into `feature_parity_defect`:

- feature, version, group, status (`open` / `investigating` / `fixed` / `wontfix`)
- first/last clean day it failed, # clean days failed, worst relative error
- up to 10 **exemplar** diverging cells `{symbol, ts, stream_value, backfill_value, rel_err}` pulled
  from `feature_validation_exception`

This is the queue the modelling-agent / lead works: investigate → fix → re-validate (a re-validation
that now passes moves the feature back toward `VALIDATED`; the defect is reopened if it regresses).
`RETIRE` only as a last resort.

## Downstream trust gating

Consumers gate on the contamination-aware grade via `quantlib/features/trust_lifecycle.py`:

- `trusted_feature_names()` → the set of `VALIDATED` feature names. A consumer (bus publish, ML training
  export, strategy) intersects its requested features with this set and **excludes** the rest (`PENDING`
  = not proven yet; `DIVERGENT`/`RETIRED` = quarantined).
- `feature_trust_grades()` → `{feature: lifecycle_state}` for a consumer that wants to **flag** rather
  than exclude.

Example (training export):

```python
from quantlib.features.trust_lifecycle import trusted_feature_names

trusted = trusted_feature_names()
export_features = [f for f in requested if f in trusted]  # never train on an unproven feature
```

(The legacy `validation_store.certified_features()` grade still exists for the overlap-certification
gate; the lifecycle state is the contamination-aware grade and is the one to gate on going forward.)

## The nightly sweep (`quantlib/features/validation_sweep.py`)

For a settled day (default = the **last market day** via the Alpaca calendar):

1. **Discover** the symbols collected live that day (distinct `source=stream` symbols).
2. **Materialize** the backfill side for those symbols from `/store/raw` (`materialize_from_raw` —
   download-once tape, no Alpaca re-fetch), in **chunks** (default 200 symbols) so a ~11k-symbol day
   never loads at once. Symbols with no `/store/raw` bars are reported + skipped. **The market-reference
   tickers (SPY/QQQ, `MARKET_TICKERS`) are pinned into every chunk** — see below.
3. **Validate** each chunk (`validate(symbols=chunk + market_tickers)`) — scoped, memory-safe, writes the
   per-cell verdicts / exceptions / rollup / legacy trust / canonical DB record.
4. **Grade** contamination-aware, derive the lifecycle state, upsert the defect backlog.
5. **Summarize**: symbols discovered/materialized/skipped, counts per lifecycle state, new defects,
   contamination stats.

Idempotent/resumable: every write is an upsert keyed on `(feature[,symbol],day)`.

### Market-reference tickers must be in every backfill chunk

The cross-sectional features — `market_beta_*`, `market_corr_*`, `idio_vol_*`, `market_return_*`,
`nasdaq_return_*`, `relative_return_*`, `outperforming_*` — regress each ticker's return against **SPY**
(and QQQ). SPY/QQQ are **ETF-like**, so `quantlib.universe.is_etf_like` screens them out of the raw-backfill
universe. If a materialize chunk lacks SPY, the backfill produces a **null** market return for the whole
chunk, so every market-relative cell is `extra_live` (live had a value, backfill had none) → never compared
→ **the feature can never validate** (it sits at `PENDING`/null forever, indistinguishable from "not enough
days yet"). Two changes close this:

- `quantlib.data.raw_backfill.universe_symbols` **force-includes** `MARKET_TICKERS` (SPY/QQQ) despite the
  ETF screen, so their bars are always acquired into `/store/raw`.
- `validation_sweep` **pins** `MARKET_TICKERS` into every materialize + validate chunk, so the backfill
  regression always resolves its market reference. The tickers are reference symbols — they are NOT counted
  in the discovered/materialized accounting, but they ARE validated themselves (SPY live vs SPY backfill).

## Schedule

Run nightly **after** market close AND after that day's raw backfill has landed in `/store/raw`. For a
self-sustaining daily loop use **`ops/daily_lifecycle.sh`**, which chains *acquire that day's raw tape* →
*sweep* in one wrapper (the 6-month `ops/raw_backfill.sh full` is a ONE-TIME job; the daily acquire is the
missing day-2 link). Thin wrappers mirror `ops/raw_backfill.sh` — fp-dev image, prod `fp_store_real`
volume, `.env` creds; never docker-exec the live feature-computer.

Crontab line (install manually — `daily_lifecycle` acquires then sweeps, so it REPLACES the standalone
sweep cron):

```cron
# Daily parity lifecycle: acquire the just-closed day's raw tape, then sweep. 18:30 PT (after close).
30 18 * * 1-5  cd /home/ben/quant-fp && ops/daily_lifecycle.sh >> /home/ben/.quant-validation/daily_lifecycle.log 2>&1
```

Or keep them separate: `ops/raw_backfill.sh daily` (acquire) earlier, then the existing
`ops/validation_sweep.sh` (sweep) at 19:30 PT. Evidence / sandbox run: `MAX_SYMBOLS=50 ops/validation_sweep.sh`
(caps the discovered set; assumes the raw tape is already present).

## Tables (`db/init/10_parity_lifecycle.sql`, additive)

- `feature_trust` (extended) — `lifecycle_state`, `clean_days`, `clean_days_passed`, `clean_value_rate`,
  `lifecycle_updated_at`. Legacy `status`/`value_grade`/`coverage_grade` untouched.
- `feature_parity_defect` — the investigate queue (one row per DIVERGENT feature, upsert).
- `stream_symbol_day_cleanliness` — per `(symbol, day)` clean/contaminated verdict + reason.
