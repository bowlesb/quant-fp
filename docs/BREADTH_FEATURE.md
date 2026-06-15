# Market & sector breadth — design spec

> A gather-group feature: what fraction of equities are moving up/down over each horizon,
> both market-wide AND within the ticker's own sector. Status: BUILT (2026-06-14,
> `quantlib/features/groups/breadth.py`, 30 features, parity-gated by `tests/test_fp_breadth.py`).
> Builds on the cross-sectional gather kind (`market_context`) + `reference` sector + `universe`.

## Built form (vs this spec)

`BreadthGroup` ships `breadth_{up,down,net}_{W}` (market-wide, broadcast) and
`sector_breadth_{up,down,net}_{W}` (per-ticker, joined by sector) for `W ∈ {5m, 30m, 60m, 1d, 5d}` —
30 features. The optional `breadth_vs_sector_{W}` is deferred (the up/down/net pair already exposes the
divergence). The dead-band (`EPS = 1e-4`, §4 below) is the parity mechanism, unit-tested with a
boundary case: names ending the window inside ±EPS are FLAT, and a sub-EPS perturbation of those names
leaves breadth unchanged (the noise that would flip a naive `sign(return)>0` count). `compute_latest`
shares the identical per-minute reduce with `compute` and is held to cell-equality by
`tests/test_fp_latest.py`. The universe pin and sector frame are the same `universe`/`reference` the
T+1 parity harness already feeds both sides (`quantlib/features/parity.py`). Caveat: `reference`'s
sector map is a CURRENT snapshot (no `as_of`), so deep-history sector breadth uses today's sectors for
an old day — same-day-honest now, deep-history needs the map historized (spec §2).

## The features

For each horizon `W ∈ {5m, 30m, 60m, 1d, 5d}` and each minute `T`:
- **`breadth_up_{W}`** — fraction of the universe with `return_W > 0` (market-wide), broadcast to every
  ticker. (`breadth_down_{W}` = the negative side; `breadth_net_{W}` = up − down.)
- **`sector_breadth_up_{W}`** — fraction of THIS ticker's SECTOR that is up over `W` (per-ticker, because
  each ticker belongs to a sector). Captures "is my sector moving together, and am I with it?"
- **`breadth_vs_sector_{W}`** — optional: this ticker's sign vs its sector breadth (am I confirming or
  diverging from my peers?).

Intraday horizons (5/30/60m) use the intraday return; 1d/5d use daily-bar returns.

## Architecture — it's a gather, computed once per minute

Market breadth is a **single scalar per (minute, horizon)** = `count(up) / count(valid)` over the
universe — the same per-minute universe reduce as `market_context`'s index return. Sector breadth is the
same reduce **grouped by sector** (one scalar per (minute, horizon, sector)), then joined onto each
ticker by its sector. Both are O(universe) once per minute — cheap, and they ride the cross-section
state kind, not a per-symbol fold.

```
per minute T, per horizon W:
  ret_W   = each symbol's return over W            (intraday: lag kind; 1d/5d: daily)
  up      = sign_with_deadband(ret_W)              (see parity §4)
  market  = mean(up over universe_at_T)            -> broadcast to all
  sector  = mean(up) GROUP BY sector_at_T          -> join onto each ticker by its sector
```

## Parity issues (the interesting part)

1. **Universe must be pinned point-in-time.** "Fraction of all equities" is only defined against a fixed
   membership. Compute over `universe_membership[trade_date=T.date]` — the SAME pin validation already
   uses. Live and backfill share it, so the denominator can't drift.
2. **Sector mapping must be point-in-time.** Sector breadth needs each ticker's sector AT T. `sector_map`
   is currently a CURRENT snapshot (symbol PK, no `as_of`) — fine for live + same-day backfill, but a
   deep backfill would use today's sectors for an old day (the reference-historization hazard). For now:
   sector breadth is same-day-honest; deep-history sector breadth needs `sector_map` historized (or is
   flagged `deep_trust=no`). Null-sector tickers bucket to an "UNKNOWN" sector, never dropped.
3. **Coverage coupling.** Breadth is a ratio over the universe, so a missing symbol-minute moves the
   count. The denominator must be "symbols with a VALID `return_W` (present at both T and T−W),"
   computed identically both sides. Breadth is therefore *sensitive to the capture-coverage gap* — a
   symbol live didn't capture at T−W is excluded live but present in backfill → divergence. Mitigation:
   pin to the universe AND require the same presence test; surface coverage so a low-coverage minute's
   breadth is flagged, not silently wrong.
4. **THE NOVEL ONE — an aggregate of a discontinuous function.** Breadth counts `sign(return)`, and sign
   jumps at 0. A return that differs by less than the feature's tolerance between live and backfill
   (legit float/tick-order noise) can still **flip a symbol across zero**, changing the integer count.
   So cell-level tolerance does NOT compose into the aggregate — a "within-tolerance" world can produce a
   different breadth value. Three handling options, pick per taste:
   - **Dead-band (recommended):** count `up = ret_W > +ε`, `down = ret_W < −ε`, else "flat" (in the
     denominator but neither up nor down), with `ε` a small fixed return (e.g. 1 bp). A return within ε
     of zero is genuinely sign-ambiguous, so excluding it from up/down makes the count *robust* to
     the noise that would flip it. This is the cleanest and also economically sensible.
   - **Distributional/count tolerance:** validate breadth with a ±k-count tolerance on the aggregate
     rather than the cell predicate (the validation ledger's distributional path).
   - Accept it's noisy near a 50/50 market and document it.
5. **Long horizons inherit multi-day/split parity** (1d/5d): "up over 5d" needs the split-adjusted
   5-day-ago close — the raw-intraday-vs-adjusted-daily hazard (CORPORATE_ACTIONS_PARITY.md). Same
   handling as the existing multi-day features.

## Build (buildable now)
- A `BreadthGroup` (gather kind): declares the horizons; `compute_latest` does the per-minute count over
  the pinned universe (+ a `sector` join from `sector_map`), with the dead-band sign. `compute`
  (backfill) does the identical reduce over the day's minutes. Broadcast scalars → join onto symbols.
- Inputs: `minute_agg` (returns), `universe` (the pin), `reference`/`sector_map` (sector). Daily frame
  for 1d/5d.
- Parity: market breadth validates cleanly with the dead-band; sector breadth same-day-only until
  `sector_map` is historized; the long horizons follow the multi-day parity rules.
- It's a great first **modelling-agent feature request** target — economically intuitive, cheap, and it
  exercises the gather kind + the novel sign-aggregate parity handling.
