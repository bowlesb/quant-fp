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

### The cleanliness heuristic (`quantlib/features/cleanliness.py`)

A stream `(symbol, day)` is **CLEAN** iff, over the regular session (09:30–16:00 ET = 390 minutes):

1. **Coverage** — distinct RTH stream minutes ≥ **95%** of the minutes the **backfill** side actually
   produced for that symbol-day. The denominator is "minutes truth had", not a flat 390, so a thin or
   halted name that legitimately prints few bars is still clean.
2. **No internal gap** — the largest gap between consecutive distinct RTH stream minutes is ≤ **5
   minutes**. A capture restart leaves a multi-minute hole that breaks any window reaching across it; a
   single missed print does not.

Failing either → **CONTAMINATED**: the day's comparisons for that symbol are recorded but **excluded
from the grade** (marked, not counted as a failure). Per-symbol verdicts are persisted to
`stream_symbol_day_cleanliness` (the audit trail behind why a symbol-day was excluded).

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
   never loads at once. Symbols with no `/store/raw` bars are reported + skipped.
3. **Validate** each chunk (`validate(symbols=chunk)`) — scoped, memory-safe, writes the per-cell
   verdicts / exceptions / rollup / legacy trust / canonical DB record.
4. **Grade** contamination-aware, derive the lifecycle state, upsert the defect backlog.
5. **Summarize**: symbols discovered/materialized/skipped, counts per lifecycle state, new defects,
   contamination stats.

Idempotent/resumable: every write is an upsert keyed on `(feature[,symbol],day)`.

## Schedule

Run nightly **after** market close AND after that day's raw backfill (`ops/raw_backfill.sh`) has landed
in `/store/raw`. Thin wrapper: `ops/validation_sweep.sh` (mirrors `ops/raw_backfill.sh` — fp-dev image,
prod `fp_store_real` volume, `.env` creds; never docker-execs the live feature-computer).

Crontab line (install manually — raw backfill at ~20:30 ET, sweep at 22:30 ET to let it land):

```cron
# Nightly parity-validation sweep — last market day, all collected symbols, chunked. 22:30 ET.
30 22 * * 1-5  cd /home/ben/quant-fp && ops/validation_sweep.sh >> /var/log/quant/validation_sweep.log 2>&1
```

Evidence / sandbox run: `MAX_SYMBOLS=50 ops/validation_sweep.sh` (caps the discovered set).

## Tables (`db/init/10_parity_lifecycle.sql`, additive)

- `feature_trust` (extended) — `lifecycle_state`, `clean_days`, `clean_days_passed`, `clean_value_rate`,
  `lifecycle_updated_at`. Legacy `status`/`value_grade`/`coverage_grade` untouched.
- `feature_parity_defect` — the investigate queue (one row per DIVERGENT feature, upsert).
- `stream_symbol_day_cleanliness` — per `(symbol, day)` clean/contaminated verdict + reason.
