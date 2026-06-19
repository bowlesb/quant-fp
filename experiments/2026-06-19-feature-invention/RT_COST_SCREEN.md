# RT-cost screen for invented features

> A cheap, definition-only rubric the FeatureInventor applies to each candidate **as it is invented**, so a
> great-IC feature that is too slow for the <100ms production path is caught BEFORE it is proposed for
> promotion. Authored by the Latency workstream (`docs/LATENCY_PLAN.md` §7, `latency-ledger.md`).
>
> **One line:** invention should PREFER RT-GREEN forms; every KEEP feature gets an RT tag (GREEN / YELLOW /
> RED) in `backlog.md` before it goes to the promotion gate.

## Why this exists

Ben's standing principle is **bottom-up, RT-trivial feature design** ("Engineering is the edge"): a feature
earns its place only if it is *timed and fast* (`tests/test_fp_latency.py`, `PER_FEATURE_CEILING_US`). The
production per-minute path must reach **bar-arrival → universe-vector-ready in <100ms**; today it runs ~1.75s
p50 live (inside the 60s cadence, ~8-10× over goal), and the entire over-budget mass is concentrated in a
**handful of groups** — not spread evenly. So the *compute SHAPE* of a feature, knowable from its definition
alone, determines whether it lands in the ~2.5ms floor tier or the ~150ms dominant tier. Screening for that at
invention time costs nothing and avoids inventing into the expensive corner.

The IC screen (`invent_screen.py` → `screen_results.csv`) answers *"does it predict?"*. This answers the
orthogonal *"can we afford to compute it live, every minute, on the whole universe?"*. A feature needs BOTH.

## The grounding: production's measured latency tiers

From `docs/SIM_LATENCY_AUDIT.md` (682 feats / 51 groups, single-shard 93-symbol / 300m-buffer baseline). The
shape — NOT the absolute ms, which moves with host load — is what the rubric maps onto:

| tier | example groups | p50 / shard | per-feat cost | compute SHAPE |
|---|---|---|---|---|
| **floor (cheap)** | the 33 `ReductionGroup`s (volume, price_volume, distribution, liquidity, …) | **~2.5 ms** each | ~0.7–1 k µs | windowed **sum/mean/std/min/max** → O(1) incremental running-sum form; one row/symbol at T |
| **mid** | `residual_analysis` (~17–50 ms), the cross-sectional gather groups `market_context` / `daily_beta` / `return_dispersion` / `liquidity_rank` (~9–20 ms) | **~10–50 ms** | ~3–4 k µs | per-symbol **rolling().agg()** on a bounded slice (75m), OR a **single cross-sectional gather** (rank/zscore across the universe at T) |
| **dominant (expensive)** | **`momentum_run`** (~95–230 ms; ~23,500 µs/feat — the single worst) | **≫ everything** | ~12–24 k µs | **OLS / rolling-regression**, run-length, `rolling().agg()` with a `list.eval`, large-window recompute |

`momentum_run` + `residual_analysis` alone are ~43% of the per-minute compute (the open, Lead-gated "Lever 2").
The lesson for invention: **the algebra you choose puts a candidate in one of these three tiers, and you can
read the tier off the definition.**

## The rubric — tag each candidate GREEN / YELLOW / RED from its DEFINITION

Ask, in order. The FIRST match that fires sets a floor on the tag (you cannot be greener than the worst trait
your definition needs).

### RT-GREEN — real-time-trivial (target every invented feature here)
A windowed reduction whose state updates **O(1) per bar** and emits **one value per symbol at T**, with **no
cross-symbol dependency**. Concretely, the feature is a fixed function of running, bounded-window **sums** of
per-bar (or per-trade-bucket) quantities:

- sum / mean / count over a bounded trailing window
- variance / std / coefficient-of-variation / z-score (a function of Σx and Σx²)
- a ratio of two such sums (OFI = Σ signed_vol / Σ vol; block-share = mean(p99)/mean(mean); body-efficiency
  = Σbody / Σrange; semi-deviation ratio)
- min / max / last over the window
- a recent-sub-window-vs-window ratio built from two bounded sums (accel/shift features: `f_vol_accel`,
  `f_flow_accel`, `f_intensity_accel`, `f_close_loc_shift`, `f_print_size_accel`)

These are exactly the `ReductionGroup` shape — they live at the ~2.5ms floor and (when the algebra is a clean
sum) can be served incrementally. **Most of the invent batch is already GREEN-eligible** (see the tagging
below). Prefer expressing a new idea in this form whenever possible.

### RT-YELLOW — bounded but not O(1); affordable, not free (use deliberately)
Fires if the definition needs ANY of:

- a **lag-k autocorrelation / serial-correlation** within the window (`corr(x, x.shift(1))`) — needs the
  ordered series, not just sums, so it is a per-symbol rolling op, not a pure reduction
  (`f_ret_autocorr1`, `f_vol_autocorr1`, `f_flow_autocorr1`)
- a **within-window quantile / median / rank / sort** (an order statistic, e.g. a true per-window p99 rather
  than the bar-level p99-then-mean approximation `f_block_share` already uses)
- a **single cross-sectional gather at T** (rank / zscore / dispersion across the universe) — this is the
  `market_context` / `liquidity_rank` shape: bounded (one pass over the T cross-section), but it serializes
  the shard and cannot be sharded away
- a per-symbol `rolling().agg()` on a bounded slice (the `residual_analysis` 75m shape)

YELLOW is fine in moderation; it is the ~10–50ms tier. The cost is real but bounded. A handful of YELLOW
features is affordable; a *family* of them is a budget decision for the Lead.

### RT-RED — expensive; do not invent here without an explicit reason (flag to Lead)
Fires if the definition needs ANY of:

- **OLS / rolling regression** over the window (slope/intercept/r²/residual-skew per symbol per minute) — the
  `momentum_run` / `residual_analysis` algebra, ~12–24 k µs/feat, the single most expensive shape we run
- **run-length / path-dependent state machines** scanning the ordered window (`momentum_run`'s run math)
- a **`rolling().agg()` with a `list.eval`** or any per-window Python/expr callback
- **full-history (unbounded / 300m) recompute** every minute (the recompute tax the buffer-slice already
  fought; do not reintroduce it)
- a **multi-pass cross-sectional** op (sort-then-join, pairwise cross-symbol, an O(N²) or repeated gather)

RED is allowed ONLY when a feature's IC is exceptional AND no GREEN/YELLOW re-expression captures it. It then
requires an explicit Lead-gated production-cost decision (it rides the Lever-2 territory). Tag it RED, note the
expensive trait, and flag it — do not silently promote.

## Decision checklist (paste into a candidate's backlog row)

```
RT-tag:  GREEN | YELLOW | RED
- emits one value per symbol at T?            (no → not a per-minute feature)
- window bounded (≤ a few hours)?             (no → RED: unbounded recompute)
- needs OLS / regression / run-length?        (yes → RED)
- needs autocorr / quantile / sort in-window? (yes → YELLOW, else continue)
- needs a cross-sectional gather at T?        (yes → YELLOW: gather, can't shard away)
- pure function of bounded running sums?      (yes → GREEN)
RT-note: <the single trait that set the tag>
```

## Applied to the current invent batch (`screen_results.csv`)

Tagged from the definitions in `invent_screen.py` (the screen's own implementation is a vectorized backtest
over many days at once — these tags describe the **per-minute live cost** of the same algebra, the thing that
matters for production):

| candidate | RT-tag | trait |
|---|---|---|
| `f_size_tail_ratio`, `f_size_cv`, `f_avg_print_size`, `f_block_share` | **GREEN** | ratios/means of bounded trade-bucket sums |
| `f_body_efficiency`, `f_wick_asym`, `f_close_loc_mean`, `f_vwap_dev_mean`, `f_vwap_dev_now` | **GREEN** | sums/means of per-bar shape primitives |
| `f_vol_cv`, `f_vol_accel`, `f_close_loc_shift`, `f_print_size_accel`, `f_intensity_accel`, `f_flow_accel` | **GREEN** | sub-window-vs-window ratios of bounded sums |
| `f_ofi_window`, `f_signed_notional_imb` | **GREEN** | Σ signed / Σ total over the window |
| `f_interarr_burst` | **GREEN** | std/mean of per-bucket inter-arrival stats (bounded sums) |
| `f_ret_autocorr1`, `f_vol_autocorr1`, `f_flow_autocorr1` | **YELLOW** | lag-1 autocorr → ordered series, not pure sums |
| `f_ret_runfrac` | **YELLOW** | consecutive same-sign run fraction → path-dependent (borderline; a sign-change *count* is GREEN, the run-length form is YELLOW) |

**Read-out: the top IC survivors are all RT-GREEN.** `f_size_tail_ratio` (IC 0.80 vs y_fwd_vol),
`f_avg_print_size`, `f_size_cv`, `f_block_share`, `f_body_efficiency` — the entire strong-IC head of the
table is in the cheap tier. The only non-GREEN candidates are the three autocorr features and `f_ret_runfrac`,
all YELLOW (none RED), and none of them is a top IC survivor. **So the invention batch has NO RT conflict** —
the inventor can promote the strongest features without a latency objection, and there is no RED candidate that
would force a Lead-gated cost decision. This is the screen working as intended on its first application:
confirming the best-IC ideas are also the cheapest to run live.

## How the inventor uses this (backlog convention)

1. When authoring `backlog.md`, add an `RT-tag:` line to each KEEP row (the checklist above).
2. Prefer the GREEN re-expression of an idea: e.g. count sign-changes instead of run-length; use a bounded
   running-sum z-score instead of a per-window quantile; avoid a per-symbol OLS where a sum-ratio captures the
   same effect.
3. A KEEP feature carrying a **YELLOW** tag is fine but is a budget line item — note it. A **RED** tag is a
   STOP: flag it to the Lead with the IC justification before proposing promotion; it cannot ride a routine
   fingerprint deploy without a production-cost decision.
4. The latency workstream will re-confirm the tag with a one-shard timing once a candidate has a real quantlib
   prototype (the rubric is the cheap definition-time filter; the profiler is the confirmation), via
   `python -m quantlib.features.profile <symbols> <buffer> <warmup> <reps> --latest`.

## Optional: time a real prototype on one shard

The rubric tags from the definition. Once a candidate exists as a real quantlib `FeatureGroup` prototype, the
exact cost is one command (the same harness the Latency regression check uses):

```bash
docker run --rm -v /home/ben/quant-fp:/app -w /app fp-dev \
  python -m quantlib.features.profile 93 300 250 5 --latest
```

It prints per-group `compute_latest` p50/p99 ms and µs/feature. Compare the candidate's group against the
tiers above: at the ~2.5ms floor → GREEN confirmed; in the ~10–50ms band → YELLOW; near `momentum_run`'s
~150ms → RED. Re-confirm any offending rep on a quiet box first (load inflates uniformly — the diff rule in
`docs/SIM_LATENCY_AUDIT.md`: a real cost is one that survives a quiet re-measure across all reps).

---

## The symmetric parity gate

This RT screen answers *"can we afford to compute it live?"*. Its sibling **`PARITY_PROMOTION_GATE.md`**
answers the orthogonal *"will the live value EQUAL the backfill value?"* — a definition-time GREEN/YELLOW/RED
parity tag + mandatory degenerate-window guards (the `#122`/`#131`/`#139` relative-epsilon floor class) so a
promoted feature is parity-true BY CONSTRUCTION. A KEEP feature carries BOTH tags (`RT-tag:` + `Parity-tag:`)
before any promotion PR. The two tiers coincide: the cheap `ReductionGroup` form is also the parity-true
form, so the strong-IC head of the batch is GREEN on both screens.

---

_Cross-linked from `experiments/BACKLOG.md` (Modeller portfolio) and `PARITY_PROMOTION_GATE.md` (the
symmetric parity gate). Latency workstream; see `latency-ledger.md`._
