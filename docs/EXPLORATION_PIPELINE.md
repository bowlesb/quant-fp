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
