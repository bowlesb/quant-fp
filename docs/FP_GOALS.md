# FP Goals — the quantified scoreboard

Concrete, countable, demonstrable goals under `docs/FEATURE_PLATFORM.md`. Every goal is a specific
number tied straight to the direction: *a parity-true feature platform that computes a large,
complex feature set for the whole market every minute, with a repeatable dev lifecycle.* The
Manager updates `[ ]`/`[x]` every wake. Numbers may be **tightened, never loosened** (§6); "green"
needs the verify-command run this cycle with its artifact committed.

## ★ Headline targets (these numbers ARE the direction)
- [ ] **1,000 features × 10,000 tickers — full vector ready in < 2 s** (p99) at every minute boundary.
- [ ] **≥ 95% live-vs-backfill parity** per feature, per tier, per session (the T+1 Settled-Day Test).
- [ ] **Raw ticks captured for 10,000 tickers, 04:00–20:00 ET, 0 dropped messages** a full session.
- [ ] **The dev lifecycle demonstrated for ≥ 5 feature groups** end-to-end (collect → parity → model → serve).
- [ ] **1 model trained on backfill, served live, with ZERO train/serve skew.**

## Pinned definitions
- Tiers (trailing 20-day ADV$): **Tier-1 = top 500 · Tier-2 = 501–2,000 · Tier-3 = 2,001–10,000**.
- Settled day = D+1 after corrections. Parity match = |live − backfill| ≤ declared tolerance.
- Latency budget (of the 2 s, p99): aggregate finalize ≤ 0.5 s · feature compute ≤ 1.0 s ·
  persist ≤ 0.5 s · HARD max ≤ 5.0 s with **0** minutes over.

---

## A. Extensible infrastructure — designed AND executed
- [ ] A new feature group is added in **1 new file**, **0 edits** to engine/store/registry,
      ≤ **30 lines** of boilerplate — proven by `make new-feature`.
- [ ] **3 commands operational**: `make introspect`, `make parity`, `make feature-catalog`.
- [ ] The store is **per-feature-addressable**: update **1** feature without rebuilding the other
      **999** — proven by recomputing one feature in isolation.
- [ ] Old flat-vector store **thrown away**; the raw-ticks → minute-aggregates → derived-vectors
      layers are live (greenfield mandate, §1.1).
- [ ] **Group-conformance CI gate** FAILS on each of **5** break types (undeclared column,
      look-ahead, non-determinism, duplicate name, in-place mutation) and PASSES clean.

## B. Parity — the cornerstone
- [ ] **2 feature groups incorporated AND backfilled** at ≥ 95% parity per tier — first proof of the loop.
- [ ] First **10**-feature cohort: **all 10 ≥ 95% parity** in **each of 3 tiers**,
      **≥ 50,000 compared cells per tier per feature**.
- [ ] **10 consecutive settled days** of green daily T+1 parity, **≥ 100,000 cells/feature/day**.
- [ ] **1 injected drift** auto-flags the feature FAILING and excludes it from scoring within **1 day**.

## C. NaN & correctness scrutiny
- [ ] **1 mostly-NaN feature (> 50% NaN) fully verified** — **every** NaN category traced to a
      sensible cause (warmup / tradeless minute / undefined math), **0** NaNs from bugs, documented.
- [ ] Across all certified features: **0 unexplained NaN categories**; each feature's NaN% ≤ its
      declared cap (returns ≤ 20%, trade/quote/micro ≤ 5%).
- [ ] **0 degenerate features** (each certified feature ≥ 2 unique values, within declared range).
- [ ] No-look-ahead test: **0 violations** over **≥ 10,000** random (ticker, minute) samples.

## D. Scale & latency — the headline machine
- [ ] Lossless capture (≥ 98% per tier) sustained a full session at **500 → 2,000 → 10,000** tickers.
- [ ] **0 dropped messages**, ingest lag p99 < 2 s, at **10,000** tickers through the open-bell burst.
- [ ] Minute vector for **1,000 features × 10,000 tickers** ready **p99 < 2.0 s**, **0** minutes over 5 s.
- [ ] Vector completeness **≥ 99.5%** of (ticker, minute) cells, per tier, per session.

## E. Feature breadth — the growth curve
- [ ] **2 groups → 7 groups (5 new added) → 10 groups certified.**
- [ ] Feature count **10 → 50 → 150 → 500 → 1,000**, every feature certified at each step.
- [ ] **≥ 8 distinct families** with ≥ 1 certified feature each; **no pair |corr| > 0.99** unjustified.
- [ ] **≥ 1 microstructure-burst feature** certified and joined to `fwd_1d`..`fwd_5d` labels (the thesis).

## F. The dev lifecycle — the definition of winning
- [ ] §1.3 loop demonstrated end-to-end for **1** feature, then **3**, then **5** distinct features.
- [ ] **1 model** trained on backfilled features, **served live**; prediction parity live-vs-replay **≥ 99%**.
- [ ] A brand-new feature goes **idea → certified within 1 working session**.
- [ ] `docs/LIFECYCLE_DEMOS.md` holds **≥ 5** complete demo trails (speed, distribution, QA note,
      NaN audit, parity report, model + serve proof).

## G. QA adversarial loop — proven to bite
- [ ] QA adversarially reviews **100%** of certified features with a logged common-sense check.
- [ ] QA catches **≥ 1** real logical error and forces a throw-out-and-re-collect (proof it bites).

## H. Extended hours & storage
- [ ] Premarket (04:00–09:30) + afterhours (16:00–20:00) **captured AND backfilled**, completeness ≥ 98%/session.
- [ ] Rolling raw-tick window holds **free disk ≥ 15%**; achieved window **≥ 30 days** at 10k (recorded).
