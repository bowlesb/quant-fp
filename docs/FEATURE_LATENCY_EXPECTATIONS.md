# Feature latency expectations — the stable explanation

> **What this is.** The stable EXPLANATION of how to think about per-group feature latency: the KINDS (Ben's
> A/B/Rust framing), the levers that move the per-bet number, the measurement methodology, and how to read
> the living data. This doc holds the concepts that DON'T change as we optimize.
>
> **Where the living numbers are.** The actual per-group **p50/p95/p99 expectations** — the data we expect to
> change as we improve our latency — live as agent-updatable, UI-viewable JSON:
> [`docs/feature_latency_expectations.json`](feature_latency_expectations.json). This MD is deliberately
> NUMBER-FREE at the per-group level; the JSON is the single source of the live ranking.
>
> **It is a LOOP, not a snapshot.** `re-measure → write JSON → the dashboard reads it → iterate as we
> optimize`. A scheduled cron keeps it fresh; a one-shot trigger refreshes it the moment an optimization
> lands. See [The recompute loop](#the-recompute-loop) below.

## The machine-readable sources (one each, distinct jobs)

| file | job | who reads/writes it |
|---|---|---|
| [`docs/feature_latency_expectations.json`](feature_latency_expectations.json) | the **living EXPECTATIONS** — per-group p50/p95/p99 + kind/mechanism/incremental-readiness, sorted slowest-first, + the in-flow e2e block + the live crypto cross-check | a UI renders it; the recompute loop re-measures + rewrites it |
| [`docs/latency_budget.yaml`](latency_budget.yaml) | the per-group `compute_latest` ms **regression GATE** (`tests/test_fp_latency_budget.py`) | the pytest gate; re-seed only on a deliberate reviewed change |
| [`docs/latency_e2e_budget.yaml`](latency_e2e_budget.yaml) | the **end-to-end** bar→vector ceiling gate (`tests/test_fp_latency_e2e.py`, #315) | the pytest gate |
| [`docs/INCREMENTAL_READINESS.md`](INCREMENTAL_READINESS.md) | the deeper kind/state/lever detail + the PARKED corr-denom-straddle write-up | humans; the working detail behind this summary |

The JSON is the **human/UI/agent expectations view** (p50/p95/p99, slowest-first); the two YAMLs are the
**pytest gates** (a single number a regression must stay under). Keep them separate: the JSON tracks where we
ARE and is expected to move; the YAML ceilings are floors-we-must-not-cross that move only on a deliberate win.

## The end-to-end picture (read this first)

The number that matters for trading is **per-bet bar→vector**: from a minute's last bar arriving to THAT
bet's feature vector being ready. The current measured e2e numbers live in the JSON's `e2e_context` block
(and the e2e gate `docs/latency_e2e_budget.yaml`); the target is **p99 < 100ms**.

**Two honest facts about the gap:**
1. The **isolated** floor is per-group COMPUTE. The per-bet vein of cheap structural wins (caching the static
   groups, sharing passes, latest-only folds) is largely harvested — only a **Rust-resident emit kernel**
   (folding all the per-group polars frame-builds into one resident pass) moves the isolated floor toward
   <100ms. That's a coordinated fp-dev image build, not a quick win.
2. The isolated → under-load gap is **CPU contention, NOT IPC**. Measured: the reader→shard transit is
   backpressure-blocking on saturated workers, not serialization — an Arrow/zero-copy transport claws back
   ~0ms. The gap is the shard processes + the live containers oversubscribing the 32 cores. Levers there are
   ops/core-headroom + reducing what each shard computes (the FP_INCREMENTAL flip below), not a transport
   change.

**The two REAL levers (both Lead/Ben-sequenced):**
- **FP_INCREMENTAL enablement** of the ready reduction groups — flips them from re-running the rolling
  recompute each minute to reading the pre-folded running sums (O(1)). The actual live latency payoff; gated
  on a careful PARITY=1 soak → PARITY=0. The JSON's `incremental_ready: ready` groups are the candidates.
- **Rust-resident emit kernel** — the only thing that moves the isolated compute floor.

## The KINDS (Ben's framing — "should it be on running state?")

| kind | meaning | per-minute cost | "on running state?" |
|---|---|---|---|
| **A — intraday-invariant** | output is a pure function of a per-session-constant snapshot (daily / reference); compute ONCE per day, cache, broadcast | ~0 | YES — already cached |
| **B — incremental sum** | windowed reduction; the running per-(symbol,window) Σ is folded O(1)/minute (`WindowedSumState`), backfill is the parity oracle | O(symbols×windows) | YES — running sums (gated on FP_INCREMENTAL flip) |
| **B — latest-only fold** | session-cumulative or window-anchored; reduced to a single per-(symbol,session) aggregate at T (no per-minute scan) | small | partially — could promote to a declared CumulativeState kind |
| **Rust-resident** | sequential-hot per-symbol fold already in a Rust kernel (EMA/lag/extrema/swing) | O(1)/minute in-kernel | YES — done |
| **Gather** | universe cross-sectional reduce; runs ONCE in the reader phase, NOT a per-bet cost | n/a (reader-phase) | n/a |
| **hand-written** | bespoke `compute_latest`; a candidate to migrate to a kind or Rust kernel | varies | candidate |

Every group in the JSON carries its `kind`, `mechanism`, and `incremental_ready` (`ready` / `parked` / `n-a`)
so the framing above maps one-to-one onto the live data.

## How to read the JSON (the schema contract)

The JSON has a header block then a `groups` array sorted **by `p99_ms` descending (slowest-first)** — the
order a UI renders. The schema is stable so the dashboard viz binds to it:

**Header:**
- `schema_version` (currently `2`), `generated_at` (ISO-8601 Z), `units` (`milliseconds`), `sorted_by`.
- `measurement` — provenance: `per_group_method`, `e2e_method`, `reproducible`, `reference_shard` (the
  per-group isolated scale), `sim_scale` (the e2e sim scale).
- `e2e_context` — the in-flow bar→vector truth: `measured_at_sim_scale` (this regen's `{p50_ms, p95_ms,
  p99_ms, minutes}` from the real streaming sim), the documented production anchors
  (`single_bet_isolated_p50_ms` ~289, `typical_bet_under_load_p50_ms` ~935), and `target_p99_ms` (100).
- `live_crypto_crosscheck` — `{status, samples, p50_ms, p95_ms, p99_ms, note}` (or `status: unavailable` /
  `skipped`): the realism anchor (see below).
- `group_count`, `feature_count`, `not_measured_groups`.

**Each `groups[]` row:**
- `group`, `feat_count` — name and feature count (feat_count is registry-authoritative).
- `kind`, `mechanism`, `incremental_ready` (`ready` / `parked` / `n-a`) — the stable framing (above).
- `p50_ms`, `p95_ms`, `p99_ms` — the measured typical / upper / tail per-group `compute_latest` cost.

**Two caveats the JSON itself restates (`measurement` + `e2e_context.note`):**
- The per-group rows are the **isolated single-shard view**. They **OVER-count the B incremental-sum groups**
  (each is timed standalone, not as its in-flow shared-emit share, since they share ONE batched emit) and
  **exclude the reader gather/IPC**. Use them for RELATIVE ranking + regression detection; the honest
  bar→vector number is `e2e_context.measured_at_sim_scale`, **NOT the sum of these rows**.
- `not_measured_groups` lists groups real but not measurable in the isolated profiler frames (the gather
  group `peer_relative` runs in the reader phase, so it has no per-bet `compute_latest` cost).

## Methodology — how the numbers are produced

`quantlib.features.latency_expectations` produces **two** measurements, both reproducible, neither touching
live capture:

1. **Per-group p50/p95/p99 (the rankable `groups` array)** — each runnable group's `compute_latest` (the LIVE
   per-minute path) timed in **isolation** at the reference shard, over a distribution of reps, keeping the
   full distribution so all three percentiles are real. *Why isolated, not the sim:* the streaming sim splits
   the shared reduction emit **evenly** across its reduction groups, so it reports one identical number for
   all of them and cannot rank them against each other — the isolated path gives each group its own cost.
2. **The in-flow e2e block (`e2e_context.measured_at_sim_scale`)** — p50/p95/p99 of the real bar→vector
   wall-clock from driving the **REAL streaming path**: the #315 sim harness `run_profile_sim_raw`
   (protocol-faithful msgpack mock → a real `StockDataStream` → the same shard workers → the incremental fast
   path) at a bounded prod-like scale. This is what a bet actually pays (gather + IPC + shared emit together).

**Realism cross-check (the crypto anchor).** Ben asked to "re-use the crypto streaming to make it realistic."
The live `crypto-capture` container is a genuine 24/7 capture running the **same shared compute core** on a
real Alpaca feed; its per-minute `compute_ms` log line is real live latency. The loop harvests a recent window
of it into `live_crypto_crosscheck` as a **realism floor to sanity-check the sim against** — NOT a per-group
source, because crypto is a tiny 2–5 symbol universe with SPY-relative groups excluded (fixed-overhead
dominated, not per-group-attributable for equity). So: the **sim** is the reproducible per-group/e2e baseline;
the **crypto live numbers** are the "is this in the right ballpark of real capture?" anchor.

Absolute ms moves with host load; that is why the JSON is RE-measured (not hand-edited) whenever the latency
picture changes, and why the pytest ceilings carry generous headroom.

## The recompute loop

`ops/remeasure_latency.sh` is the cpu-capped wrapper both callers run. It (1) harvests the crypto `compute_ms`
window **on the host** (the fp-dev container has no docker socket) into a temp file passed in via
`CRYPTO_COMPUTE_MS_FILE`, then (2) runs the measurement inside the baked `fp-dev` image **`--cpus`-capped** so
it never starves live capture, writing the JSON deterministically (sorted, stable keys).

```bash
# One-shot re-measure (e.g. right after an optimization lands):
ops/remeasure_latency.sh                 # per-group + e2e + crypto cross-check
CPUS=4 ops/remeasure_latency.sh          # tighter cpu cap
NO_E2E=1 ops/remeasure_latency.sh        # per-group + crypto only, skip the heavy sim (~1 min)

# Or the raw module (inside fp-dev), for a dry-run print without writing:
docker run --rm --cpus=8 -v "$PWD":/app -w /app --env-file .env fp-dev \
    python -m quantlib.features.latency_expectations            # print only
#   ... latency_expectations --update                           # write the JSON
#   ... latency_expectations --update --no-crypto --no-e2e      # fastest, per-group only
```

The output is deterministic (groups sorted by p99 desc, stable key order); set `SOURCE_DATE_EPOCH` to pin
`generated_at` reproducibly. When a group is added/removed, also add/remove its stable metadata in
`GROUP_METADATA` (the module flags an unclassified group loudly rather than dropping it; feature counts come
from the registry automatically).

**Scheduled recompute (cron).** Proposed cadence: **nightly, off-hours, weekdays** (the JSON tracks the
equity feature set; the box is quietest overnight and the crypto cross-check is still live 24/7). A sensible
slot is **02:30 PT** (after the nightly relaunch settles, well clear of the open):

```cron
30 2 * * 1-5  cd /home/ben/quant-fp && CPUS=8 ops/remeasure_latency.sh >> /home/ben/.quant-ops/remeasure_latency.log 2>&1
```

The Lead wires the live crontab (per ops/OPERATIONS.md). The job is idempotent (re-running only rewrites the
JSON), bounded (fixed sim scale), and cpu-capped (never starves capture), so it is safe to schedule.

## UI rendering (follow-up — coordinate with DashLatencyView)

Ben wants this JSON **UI-viewable**. The dashboard should render `docs/feature_latency_expectations.json` as a
**slowest-first bar chart / table** (the array is already in render order): `p50_ms` is the bar length;
`p95_ms` + `p99_ms` + `kind` / `mechanism` / `incremental_ready` / `feat_count` are hover detail; the header
block (`e2e_context` + the crypto cross-check + the over-count caveat) renders above it. The schema above is
the binding contract — keep it stable. The JSON is left clean and render-ready; the dashboard wiring (an
endpoint + the viz) is the separate DashLatencyView task.
