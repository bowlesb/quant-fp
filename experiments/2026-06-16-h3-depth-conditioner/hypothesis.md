# H3 — Quote-depth / spread state as a vwap_dev reversion CONDITIONER — pre-registration

**Registered:** 2026-06-16 (before any data run). Now UNBLOCKED — quotes are in `/store/raw` (2,504 names
× ~21d). Ready to dispatch to an explorer the moment H2 scoring frees the box.

## Hypothesis

The vwap_dev mean-reversion is the platform's one proven (if uneconomic) price signal. H3 asks whether
**book state CONDITIONS which deviations revert vs continue** — NOT whether depth/spread is a standalone
ranker (it is not the claim). Specifically: a price stretched below VWAP on a **deep, tight, bid-heavy
book** reverts more reliably (and more cheaply) than the same deviation on a **thin, wide, ask-heavy book**.

Conditioners (all point-in-time, from quotes via the existing aggregates + the H2 OFI panel):
- `spread_bps` regime (tight vs wide tercile),
- `quote_imbalance` sign / size-imbalance (bid-heavy vs ask-heavy),
- `book_depth` (deep vs thin tercile).

## Test design

Within-minute decile L/S on `vwap_dev` (the H2 liquid panel), then **CONDITION**: split each book-state
tercile and measure net-of-cost reversion separately. The load-bearing comparison is
conditioned-vwap_dev net Sharpe / breakeven vs FLAT vwap_dev — does a book-state cut LIFT the tradeable
reversion above its own measured (spread) cost in any cell? Horizon-matched 15-30 min holding. Same 10-seed
shuffle canary + day-clustered t.

## Prior

Quoted depth + spread carry inventory / adverse-selection information distinct from realized trade flow
(a book can be imbalanced before any trade prints). Reversion is stronger + cheaper in calm, deep,
tight-spread microstructure regimes (the cost wall is itself lower there) — so conditioning attacks the
cost wall by DESIGN, not by gating an illiquid-concentrated signal (the H1 trap).

## Expected / confidence

- Confidence a book-state cell lifts vwap_dev breakeven above its measured cost: ~30%. The honest risk is
  that the liquid-tier vwap_dev signal is ALREADY too weak (−0.017/−0.014) for any conditioning to rescue
  net-of-cost — same wall as H1/H2.
- Pre-committed numeric: a KEEP needs at least one book-state cell with net-of-cost Sharpe > 0 AND breakeven
  > its measured spread, clearing canary, at H15 or H30.

## Kill

KILL if NO book-state cell beats flat vwap_dev net-of-cost beyond canary (depth/spread add no tradeable
conditioning) — then H3 is closed and depth is documented as non-additive for reversion at our latency.

## Dependency / ordering

Reuses the H2 liquid panel (OFI + vwap_dev + forward returns already built in
`experiments/2026-06-16-h2-retest-ofi-orthogonal/data/panel.parquet`) — just needs the book-state columns
(spread/imbalance/depth per minute) joined on. Dispatch AFTER H2 scoring frees the box (one GPU/CPU-heavy
job at a time per the charter). If H2 KILLs OFI, H3 is the next microstructure probe; if H2 KEEPs, H3 still
runs as the orthogonal conditioning question.
