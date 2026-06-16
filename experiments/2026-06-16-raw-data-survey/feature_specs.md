# Feature specs — order-flow microstructure (proposals to the Lead), 2026-06-16

Two specs grounded in the now-complete raw store. Each follows `docs/ADDING_A_FEATURE.md`: idea / prior /
exact computation / parity plan / expected value / kill. Spec 1 (OFI) is the load-bearing one and is
**Case B** (a new per-minute input → touches the parity cornerstone, Lead-reviewed). Spec 2 (signed-trade
ratio) is **Case A** (a one-file group over existing columns) and is implementable today.

Decision gate for BOTH: the H2-RETEST (`experiments/2026-06-16-h2-retest-ofi-orthogonal/`) must show OFI /
signed-flow adds IC ORTHOGONAL to vwap_dev in the LIQUID tier and clears the cost gate. If it does not,
these stay specs, not merged features (per the charter — a feature lands only when the lead clears the
marginal gate).

---

## Spec 1 — `order_flow_imbalance` group (windowed OFI from quotes)  ★ load-bearing

**Idea.** Per-minute **Order-Flow Imbalance** (Cont–Kukanov–Stoikov): the signed change in the top-of-book
across consecutive quote updates, summed over the minute, then rolled over windows. This is the *signed
pressure on the book* — distinct from `quote_spread`'s `quote_imbalance`, which is a level snapshot
(bid_size−ask_size averaged over the minute), and from `trade_flow`'s `signed_volume`, which is realized
trade flow. OFI is the leading microstructure pressure measure and was the first standalone signal to clear
a shuffle canary on our data (H2, `experiments/2026-06-15-ofi-marginal-lift/`: `ofi_15` rank-IC +0.0185,
t +3.96, positive = continuation).

**Prior.** Cont–Kukanov–Stoikov (2014): OFI is a near-linear predictor of short-horizon price change, more
informative than trade imbalance alone because it captures book pressure before a trade prints. Our own H2
probe corroborated the sign and magnitude on 3 days.

**Exact computation.** In `quantlib.aggregates`, add a threaded `QuoteState` (mirrors `TickState`) and an
`ofi` field to `QuoteAgg`. For each consecutive quote tick within the minute (state carried across
minutes):
```
bid_e = bid_size                     if bid_price > prev_bid_price
        bid_size - prev_bid_size     if bid_price == prev_bid_price
        -prev_bid_size               if bid_price < prev_bid_price
ask_e = ask_size                     if ask_price < prev_ask_price
        ask_size - prev_ask_size     if ask_price == prev_ask_price
        -prev_ask_size               if ask_price > prev_ask_price
ofi  += bid_e - ask_e
```
Then an `OrderFlowImbalanceGroup(ReductionGroup)` over the new `ofi` minute_agg column:
- `ofi_{w}m` = rolling SUM of per-minute OFI over `w ∈ {5,10,15,20,30,45,60}` (the H2-significant windows).
- `ofi_norm_{w}m` = `ofi_{w}m / (total share volume over w)` — scale-free, comparable cross-name
  (null when window volume is 0).

**Parity plan.**
- Layer-C cornerstone: the threaded `QuoteState` makes live (minute-by-minute) == backfill (batch) by
  construction — add a `test_aggregates.py` test mirroring `test_live_batch_parity` for OFI.
- The group is a `ReductionGroup`, so `compute_latest == compute().filter(last minute)` is auto-guarded by
  `tests/test_fp_latest.py`.
- Point-in-time: OFI for minute T reads only quotes ≤ T; no look-ahead.
- TRUST gate: settled-day quote-agg parity (live vs REST) must pass for the new `ofi` column before the
  feature is trusted-for-trading (`docs/ORDER_FLOW.md` I2b). The feature can publish before that; it is not
  trusted until the T+1 parity test is green on it.

**Files (Case B).** `quantlib/aggregates.py` (+QuoteState, +ofi); `tick_capture.py` (+"ofi" in
TICK_COLUMNS + the live dict); the `quote_agg_1m` schema + writer + `loaders._MINUTE_AGG_SQL` (Lead-owned
pipeline); `quantlib/features/groups/order_flow_imbalance.py` (+ register in `groups/__init__.py`); tests.

**Expected value.** Standalone rank-IC ~0.015–0.02 at H=15 (from H2), positive = continuation. The
LOAD-BEARING question — marginal IC orthogonal to vwap_dev in the liquid tier, net-of-cost positive at a
15–30 min horizon — is what the H2-RETEST decides. **KILL** if the retest shows OFI marginal IC ≤ canary OR
net-of-cost ≤ 0 in the liquid tier (then OFI is a conditioner at best, not an additive feature).

---

## Spec 2 — `signed_trade_ratio` group (normalized signed-trade imbalance)  — Case A, implementable today

**Idea.** A scale-free, cross-name-comparable companion to the existing raw `signed_volume_{w}m`:
`signed_trade_ratio_{w}m` = net signed volume / total volume over the window ∈ [−1, 1]. The raw signed
volume is in shares, so it is dominated by high-ADV names and not comparable cross-sectionally; the ratio
is the natural input for a cross-sectional ranker.

**Prior.** Trade imbalance is a weaker microstructure predictor than OFI but is universe-wide (trades cover
all 7,668 names, quotes only 2,504). Normalizing by volume is standard (it is the "trade-flow imbalance"
used throughout the microstructure literature). This is the cheap, full-universe complement to OFI.

**Exact computation.** `SignedTradeRatioGroup(ReductionGroup)` over EXISTING minute_agg columns
(`signed_volume`, `volume`):
- `signed_trade_ratio_{w}m` = `sum(signed_volume, w) / sum(volume, w)` for `w ∈ {5,15,30,60}`; null when
  window volume is 0 (mathematically undefined, not 0).
Worked example in `docs/ADDING_A_FEATURE.md` (`buy_pressure_{w}m`) is essentially this spec.

**Parity plan.** Pure Case A — `ReductionGroup` over existing columns, no aggregates/loaders change. Parity
auto-guarded by `tests/test_fp_latest.py`; a unit test with a hand-built frame covers the value + the
zero-volume→null edge.

**Files (Case A).** `quantlib/features/groups/signed_trade_ratio.py` + register; one unit test.

**Expected value.** Modest standalone IC (trade imbalance < OFI); its real value is as a full-universe
flow proxy where quotes are absent. **KILL** if it adds no IC over `signed_volume_{w}m` AND no marginal
lift over vwap_dev (i.e. the normalization buys nothing). Lower priority than OFI; ship only if the H2
family proves out.
