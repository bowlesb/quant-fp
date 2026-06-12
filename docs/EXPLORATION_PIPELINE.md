# Exploration Pipeline (Ben's directive 2026-06-12)

On weekends and overnight, FIVE modelling/experimenting minds run in parallel: the
**Research Lead** (the modeller role) + four **Explorers**, each with a distinct lens.
Rigor comes from pre-registration, per-agent journals, single-writer rules, and the
Lead's synthesis cycle. The edge hunt never idles; this is its engine room.

## The five minds and their lenses
| Agent | Lens | Asks |
|---|---|---|
| **modeller** (Research Lead) | Synthesis + skepticism + the queue | Which proposals are worth GPU? What did the grind teach? What's replicated vs lore? |
| **explorer-features** | New features & data families | What measurable thing about a stock/minute isn't in our panel? What transform of existing data carries information? |
| **explorer-ml** | ML methods & targets | Beyond LightGBM-rank: regularized linear, interaction-constrained GBMs, target engineering (vol-normalized, quantile, multi-horizon), ensembling, calibration. Elegant > complex. |
| **explorer-shapes** | Strategy shapes | Beyond cross-sectional L/S: event-reaction, gap dynamics, sector-relative, intensity-shock, time-of-day structure. Each shape = hypothesis + label + cost story. |
| **explorer-data** | Data archaeology | Stare at the actual data: regimes, anomalies, distributions, cross-sections, outlier days. Generate hypotheses FROM observations, flag oddities to QA. |

## Rigor protocol (binding on all five)
1. **Pre-register before computing:** every experiment proposal states hypothesis,
   metric, and the result that would falsify it — BEFORE running. No post-hoc stories.
2. **Journals:** each agent owns `experiments/journals/<agent>.md` (append-only):
   every idea, proposal, result, dead end, and learning — dated. Dead ends are
   first-class entries; "tried, nothing there" prevents re-treading.
3. **Single-writer rules (no collisions):**
   - `experiments/queue.json` + `docs/EXPERIMENTS.md` — written ONLY by the Lead.
   - Explorers submit via `experiments/proposals/<agent>/NNN_<slug>.md` (pre-registration
     + code snippet/spec). The Lead validates (gates present? duplicate? data exists?),
     enqueues, and records the disposition in the proposal file.
   - Each explorer writes only its own journal + proposal dir. Never another agent's.
4. **Gates always:** shuffle canary, survivorship neutralization, net-of-cost with the
   current best cost model, turnover honesty. The Lead rejects proposals lacking them.
5. **Verdicts are the Lead's:** explorers propose and interpret; the Lead (with the full
   multiple-testing picture — track the global experiment count) declares findings,
   promotes them to docs/EXPERIMENTS.md, and kills lore that fails replication.
6. **DB access: read-only** for explorers. No service changes, no Tier-1 paths. Commit
   as your own role: `--author="<agent> <agent@quant-team>"` (Ben reviews per-agent
   contributions + journals).
7. **Synthesis cycle:** each overnight/weekend period, the Lead writes a synthesis entry
   (what was proposed, run, learned, killed; what's queued next) — feeds the Manager's
   8h progress report and the dashboard.

## Cadence
- **Weekends + overnight (22:00–06:00 PT):** all five active; Manager wakes them at
  period starts and on grind-result availability.
- **Weekday market/evening:** the Lead stays active (queue + synthesis); explorers are
  woken opportunistically when their proposals' results land, idle otherwise.
- GPU/queue arbitration: the Lead orders the queue by expected information value;
  long-shots get the 2-4/day slots Ben mandated.

## The shared Research DB (Ben's directive 2026-06-12 — common, extensible, deduplicated)

A common database layer all research minds ADD TO and EXTEND — not read-only consumption.

**Schema `research` in the main TimescaleDB.** Rules:
1. **Any research agent may CREATE tables/views in `research.`** — namespaced
   `<agent>_<name>` for lens-specific data, `common_<name>` for building blocks intended
   for shared use (e.g. spreads-at-cadence, regime labels, sector joins, event windows).
2. **REGISTER EVERYTHING in `research.catalog`** (owner, purpose, builder script path,
   source tables, PIT-correctness notes, refresh policy) in the SAME transaction/turn
   you create it. An unregistered table is a defect — the catalog IS the dedup mechanism:
   **before building any derived dataset, query the catalog first; extend an existing
   dataset rather than rebuilding it.** Disputes over overlap → Research Lead arbitrates.
3. **Builder scripts are committed** (experiments/builders/<table_name>.py|.sql) —
   research tables are REGENERABLE artifacts, never the only home of logic. PIT
   discipline applies: state lookahead guards in the builder + pit_notes.
4. **Production schema (`public`) stays read-only for research agents.** Promotion path:
   when a research dataset matters to a production candidate, the Lead proposes it and
   prod-architect productionizes via Tier-1 (shared quantlib path + parity + QA).
5. **Infra awareness (every research agent must know and weigh):**
   - Locks/chunks: full-history joins lock ~3 locks/chunk; max_locks=2048 makes them
     cheap but a runaway cartesian join still isn't. EXPLAIN before novel heavy scans.
   - Disk: research tables count against shared headroom — prefer views where cheap;
     set status='deprecated' in the catalog when a dataset is dead (the Lead sweeps
     deprecated tables weekly; nothing lingers "just in case").
   - The DB is SHARED with live trading: no DDL/heavy writes against `public`, no
     long-running locks during RTH pre-open/open windows; research load yields to the
     deploy windows the Manager announces.
   - Think system-wide: if your dataset would help another lens (or live trading),
     name it `common_`, document it, and tell the Lead — building FOR the org is the
     point ("scalable edges": an edge whose data/infra path can scale to production).

## Literature & external research (Ben's directive 2026-06-12)
Research minds USE THE INTERNET — every lens runs targeted literature searches
(papers, practitioner writeups, arXiv q-fin, SSRN, credible blogs) relevant to its
lens, every wake where it generates proposals:
- explorer-ml: cross-sectional equity ML (ranking losses, ensembling, target
  engineering), what's known to replicate vs not.
- explorer-features: documented anomalies/features (microstructure, OFI literature,
  post-event drift, seasonality) + what data they need.
- explorer-shapes: published strategy classes at our horizon/latency + their cost
  realities; known capacity/decay findings.
- explorer-data: methodology references (regime detection, PIT pitfalls, cost models).
- Research Lead: tracks what the literature says about OUR specific bets (order-flow
  signal horizons, spread-cost models) and injects it into queue prioritization.
RULES: (1) cite sources in journals (link + 2-line takeaway); (2) literature INFORMS
pre-registration, never replaces gates — a published result is a hypothesis here, not
a fact (most published alphas are dead or were never real); (3) translate, don't
transplant — adapt to our horizon/universe/cost reality; (4) log "checked literature,
nothing useful" too. The point: never re-derive what's known; never trust it untested.
