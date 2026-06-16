# signed_trade_ratio — verdict (engineering deliverable)

**Built + parity-verified.** `SignedTradeRatioGroup` ships `signed_trade_ratio_{5,15,30,60}m` =
sum(signed_volume, w) / sum(volume, w) ∈ [-1, 1], null on a zero-volume window. Case-A (one file over the
existing `signed_volume` + `volume` minute_agg columns; no aggregates/loaders change).

## Against the pre-registration
- DoD #1 (group + register): DONE — `quantlib/features/groups/signed_trade_ratio.py`, registered in
  `groups/__init__.py`.
- DoD #2 (unit test, incl. zero-volume→null edge): DONE — `tests/test_signed_trade_ratio.py`, 4 tests,
  covering single-minute, rolling-window, ±1 bound, and the zero-volume→null edge. Green.
- DoD #3 (parity + look-ahead): `tests/test_fp_latest.py` auto-discovered the group and PASSED
  (`compute_latest == compute().filter(last minute)` cell-equal — parity by construction for a
  ReductionGroup). The trailing rolling sums cannot look ahead.
- DoD #4 (PR): folded into PR #29 for Lead review.

## Honest caveats / what is NOT claimed
- This is an ENGINEERING/correctness deliverable: a clean, cheap, universe-wide trade-flow-imbalance
  primitive the platform lacked — NOT a proven edge. The feature SHIPS on parity + correctness.
- The EDGE question (does it add IC over raw `signed_volume_{w}m`, and marginal lift over vwap_dev
  net-of-cost?) is DEFERRED and shares the same liquid-tier cost wall as the OFI/H2 family. It will be
  IC-tested alongside the OFI family on the panel; if it adds nothing, it remains an available-but-unused
  feature (an input, not a signal) — a fine outcome.

## Note flagged to the Lead (UNRELATED pre-existing bug)
`tests/test_fp_lookahead.py::test_no_lookahead_bar_features` FAILS on **origin/main** (verified on the
unmodified checkout) — the differing cells are `residual_analysis`'s `residual_skew_{5,10,15,20,30,60}m`,
which change when future data is appended (a look-ahead leak in that group). NOT caused by this work; my
group self-excludes when its inputs are absent (the test's minute_agg has no signed_volume/volume), so the
shape is unchanged. Flagging for a separate fix.
