# Feature latency expectations — the stable explanation

> **What this is.** The stable EXPLANATION of how to think about per-group feature latency: the KINDS (Ben's
> A/B/Rust framing), the levers that move the per-bet number, the measurement methodology, and how to read
> the living data. This doc holds the concepts that DON'T change as we optimize.
>
> **Where the living numbers are.** The actual per-group **p50/p99 expectations** — the data we expect to
> change as we improve our latency — live as agent-updatable, UI-viewable JSON:
> [`docs/feature_latency_expectations.json`](feature_latency_expectations.json). This MD is deliberately
> NUMBER-FREE at the per-group level; the JSON is the single source of the live ranking.

## The machine-readable sources (one each, distinct jobs)

| file | job | who reads/writes it |
|---|---|---|
| [`docs/feature_latency_expectations.json`](feature_latency_expectations.json) | the **living EXPECTATIONS** — per-group p50/p99 + kind/mechanism/incremental-readiness, sorted slowest-first | a UI renders it; an agent re-measures + rewrites it after a latency change |
| [`docs/latency_budget.yaml`](latency_budget.yaml) | the per-group `compute_latest` ms **regression GATE** (`tests/test_fp_latency_budget.py`) | the pytest gate; re-seed only on a deliberate reviewed change |
| [`docs/latency_e2e_budget.yaml`](latency_e2e_budget.yaml) | the **end-to-end** bar→vector ceiling gate (`tests/test_fp_latency_e2e.py`, #315) | the pytest gate |
| [`docs/INCREMENTAL_READINESS.md`](INCREMENTAL_READINESS.md) | the deeper kind/state/lever detail + the PARKED corr-denom-straddle write-up | humans; the working detail behind this summary |

The JSON is the **human/UI/agent expectations view** (p50 + p99, slowest-first); the two YAMLs are the
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

## How to read the JSON

The JSON has a header block (schema/units/e2e context/measurement provenance) then a `groups` array sorted
**by `p99_ms` descending (slowest-first)** — the order a UI renders. Per group:

- `group`, `feat_count` — name and feature count.
- `kind`, `mechanism`, `incremental_ready` — the stable framing (above).
- `p50_ms`, `p99_ms` — the measured typical and tail per-bet `compute_latest` cost.

**Two caveats the JSON itself restates (`measurement.note`):**
- This is the **single-shard per-group profiling view**. It **OVER-counts the B incremental-sum groups** —
  they share ONE batched incremental emit in flow, so e.g. `price_volume`'s standalone ms is not its in-flow
  share — and it **excludes the reader gather/IPC**. Use it for RELATIVE ranking + regression detection; the
  honest bar→vector number is the e2e gate, not the sum of these rows.
- `not_measured_groups` lists groups that are real but not measurable in the single-shard profiler frames
  (the gather group `peer_relative` runs in the reader phase, so it has no per-bet `compute_latest` cost).

## Methodology — how the numbers are produced

Both percentiles come from `quantlib.features.latency_expectations`, which times each runnable group's
`compute_latest` (the LIVE per-minute path the bet actually pays) over a distribution of reps at the
reference shard scale (recorded in the JSON's `measurement.reference_shard`), and keeps the full distribution
so p50 (typical) and p99 (tail) are both real — unlike `profile.py`, which keeps the MIN over reps as a
stable regression seed for the gate. Absolute ms moves with host load; that is why the gate ceilings carry
generous headroom and why the JSON is RE-measured (not hand-edited) whenever the latency picture changes.

### Updating the JSON (agent-runnable)

After a latency change (a Rust kernel, the FP_INCREMENTAL flip, a new/removed group), re-measure and rewrite
the JSON deterministically:

```bash
docker run --rm -v "$PWD":/app -w /app --env-file .env fp-dev \
    python -m quantlib.features.latency_expectations --update
```

Omit `--update` to print the table without writing. The output is deterministic (groups sorted by p99 desc,
stable key order); set `SOURCE_DATE_EPOCH` to pin `generated_at` reproducibly. When a group is added/removed,
also add/remove its stable metadata in `GROUP_METADATA` (the module flags an unclassified group loudly rather
than dropping it).

## UI rendering (follow-up — flagged, not built here)

Ben wants this JSON **UI-viewable**. The dashboard should render `docs/feature_latency_expectations.json` as a
**slowest-first table** (the array is already in render order): columns group / feat / p50 / p99 / kind /
incremental-ready, with the header block (e2e context + the over-count caveat) shown above it. The JSON is
left clean and render-ready; the dashboard wiring (an endpoint + a panel) is a separate task.
