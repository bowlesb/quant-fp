# Backfill parity validation — backfill is near-real-time (Elite)

## Key finding (2026-06-15)
Alpaca Elite's historical bars API returns TODAY's data with only ~1 minute lag (not T+1):
`backfill_bars('2026-06-15', [AAPL,SPY,...])` at 19:07 ET returned the full day 08:00→23:06 UTC,
latest bar 1.1 min old. So **live↔backfill parity can be validated continuously**, all day — not next-day.

## How to run a parity check (today, --allow-today)
1. Materialize backfill features: `materialize_alpaca_bars('/store', '<day>', symbols)` → `source=backfill`.
2. Validate: `python -m quantlib.features.validate <day> /store <val_root> --allow-today` → per-cell
   verdicts (match/near/mismatch) → per-feature trust grades, written to the validation DB.

## What today's parity revealed (5 liquid symbols, settled 18:00–19:00 UTC)
- `dollar_volume_1m` (point feature): 97% match — trustworthy.
- `volume_ratio_30m` / `volume_zscore_30m` (30m WINDOW features): only ~41% match — DIVERGENT.

Root cause: the 8 capture restarts today each cold-started the ring buffer, so window features were
computed from incomplete history while backfill has the full tape. This is the validation guarantee
working as intended — it correctly flags the restart-contaminated window features as untrustworthy.

## Implication
`FP_WARM_START=1` (rehydrate the ring from backfill on restart) is what makes live==backfill ACROSS
restarts. With warm-start on + minimal restarts, parity should be near-100%. Stand up warm-start +
a continuous validation cron (backfill is near-real-time, so it need not wait for T+1).
