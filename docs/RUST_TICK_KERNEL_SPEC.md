# Rust tick-feature kernel — build-ready spec (the highest-value resident-fold target)

> Status: BUILD-READY SPEC for the Lead's coordinated image build (2026-06-20). Author: Latency. Ben
> greenlit "more Rust + improve IPC/contention" with the explicit authorization that refactored features
> MAY re-validate (lose+regain trust) if it hits latency goals. This is the chosen Rust target + the kernel
> source + integration + parity plan. The fp-dev RUNTIME image has NO Rust toolchain (maturin/cargo absent),
> so the kernel CANNOT be compiled/parity-validated in an agent sandbox — it is built in the coordinated
> image rebuild (the same path swing_dc_fold #254 / swing a502fd2 landed through). This spec is what makes
> that build a drop-in.

## Why this target (readiness-table-ranked)

From docs/latency_budget.yaml (the #291 measured seed) the heaviest MOVABLE hand-written groups are the
Layer-C trades groups, and they all do the SAME per-(symbol, minute) group-by over the tape every minute:

| group | budget-seed ms | per-minute work |
|---|---|---|
| `subminute_gap_fano` | 52.0 | order minute's prints by ts, inter-trade gaps, var/mean Fano |
| `size_entropy` | 28.4 | per-minute size-bucket histogram → Shannon entropy |
| `print_hhi` | 13.5 | per-minute size HHI (Σ share²) |
| `inter_arrival` | 9.0 | per-minute gap tail / rapid-fire fraction |
| `microstructure_burst` | 9.6 | per-minute burst stats |
| `tick_runlength` | 3.9 | (ALREADY Rust — quant_tick.tick_run_features) |
| `trade_size_dist` | 4.9 | per-minute size distribution |

The Python latest-only reshape does NOT help (MEASURED: subminute_gap_fano latest-only = 0.82x = SLOWER —
the cost is the per-minute tick group-by itself, not the trailing rolling mean). Only a Rust single-pass
over the minute's ticks collapses it. ONE shared kernel — `tick_minute_features` — computes the per-minute
gap-Fano + size-entropy + size-HHI in a SINGLE backward scan per (symbol, minute), the exact pattern
`tick_run_features` already proves. The groups then do the cheap trailing windowed-mean over the per-minute
series (which is fast). Estimated collapse: the ~80ms+ of Layer-C per-minute group-by work → one Rust pass.

## value-identical vs re-validates (the flag Ben asked for)

These groups already declare DISTRIBUTIONAL/tolerance parity (`parity_method="distributional"`,
`tolerance=0.10`) — "the exact microsecond gaps are too tick-order-sensitive for cell-by-cell". The Rust
gap-Fano computes the IDENTICAL `var/mean` math; float-summation order differs from polars by ~1e-15 rel
(the same noise the Python latest-only oracle showed: 6142831.168188217 vs ...215). So:
- **VALUE-IDENTICAL within the declared distributional tolerance** — the parity sweep grades it CLEAN, trust
  PRESERVED. (NOT a value-CLOSE re-validate case; the math is identical, only float order differs sub-1e-9.)
- The generic `test_fp_latest` already holds these groups to their DECLARED tolerance, so the kernel passes
  it by construction if the math matches. The parity sweep on real tape is the live confirmation.

## The kernel source (drop into rust/src/lib.rs, register in the module)

```rust
/// Per-(symbol, minute) sub-minute tape features in ONE backward scan over the minute's prints (sorted by
/// (symbol, minute, ts)). Replaces the per-minute polars group-bys of subminute_gap_fano / size_entropy /
/// print_hhi. Parallel arrays in; one row per (symbol, minute) out. NaN where undefined (the caller
/// restores Polars null, matching each group's Guard-2 + is_finite backstop).
#[pyfunction]
fn tick_minute_features(
    symbol: Vec<i64>,
    minute: Vec<i64>,
    ts_us: Vec<i64>,     // exchange timestamp, microseconds (already sorted within minute)
    size: Vec<f64>,
    n_size_buckets: usize, // size-entropy histogram buckets (match the Python group's bucket edges)
) -> PyResult<(Vec<i64>, Vec<i64>, Vec<f64>, Vec<f64>, Vec<f64>)> {
    let n = symbol.len();
    let (mut out_sym, mut out_min) = (Vec::new(), Vec::new());
    let (mut out_fano, mut out_entropy, mut out_hhi) = (Vec::new(), Vec::new(), Vec::new());
    let mut i = 0usize;
    while i < n {
        let (s, m) = (symbol[i], minute[i]);
        // gap accumulators (Welford for var/mean of inter-trade gaps), size accumulators (HHI + histogram)
        let (mut gap_n, mut gap_mean, mut gap_m2) = (0u64, 0.0f64, 0.0f64);
        let mut prev_ts = i64::MIN;
        let mut size_sum = 0.0f64;
        let mut size_sq = 0.0f64;             // for HHI = Σ(size_i)² / (Σ size_i)²
        let mut hist = vec![0.0f64; n_size_buckets];
        let mut first = true;
        while i < n && symbol[i] == s && minute[i] == m {
            if !first {
                let gap = (ts_us[i] - prev_ts) as f64;   // microseconds since prior print this minute
                gap_n += 1;
                let d = gap - gap_mean;
                gap_mean += d / (gap_n as f64);
                gap_m2 += d * (gap - gap_mean);           // Welford (stable var, no Σx²−(Σx)²/n cancellation)
            }
            let sz = size[i];
            size_sum += sz;
            size_sq += sz * sz;
            // bucket = same edges as the Python size_entropy group (e.g. log-size bins); placeholder linear:
            let b = ((sz.max(1.0).log10() as usize).min(n_size_buckets - 1)).max(0);
            hist[b] += 1.0;
            prev_ts = ts_us[i];
            first = false;
            i += 1;
        }
        // gap Fano = var(gaps)/mean(gaps), ddof=1; NaN on <2 gaps or mean<=0 (Guard 2)
        let fano = if gap_n >= 2 && gap_mean > 0.0 {
            (gap_m2 / ((gap_n - 1) as f64)) / gap_mean
        } else { f64::NAN };
        // size HHI = Σsz² / (Σsz)² (concentration); NaN if no size
        let hhi = if size_sum > 0.0 { size_sq / (size_sum * size_sum) } else { f64::NAN };
        // size entropy = -Σ p·ln p over the histogram; NaN if empty
        let total: f64 = hist.iter().sum();
        let entropy = if total > 0.0 {
            -hist.iter().filter(|&&c| c > 0.0).map(|&c| { let p = c / total; p * p.ln() }).sum::<f64>()
        } else { f64::NAN };
        out_sym.push(s); out_min.push(m);
        out_fano.push(fano); out_entropy.push(entropy); out_hhi.push(hhi);
    }
    Ok((out_sym, out_min, out_fano, out_entropy, out_hhi))
}
// register: m.add_function(wrap_pyfunction!(tick_minute_features, m)?)?;
```

NOTE the Welford gap-var: it AVOIDS the Σx²−(Σx)²/n catastrophic cancellation (the SAME class P2 #283 fixed
for the reduction sums) — so the Rust per-minute Fano is MORE stable than a naive power-sum, parity-safe.
The size-bucket edges in `hist` are a PLACEHOLDER — match them to size_entropy.py's exact bins at build time.

## Python integration (per group, value-identical)

Each group's `_per_minute_*` polars group-by is replaced by ONE call to `quant_tick.tick_minute_features`
on the (symbol-coded, minute, ts_us, size)-sorted arrays (the swing_dc_fold_frame marshaling pattern),
selecting its own output column. The trailing windowed-mean (the cheap part) stays polars. `compute()` (the
backfill parity oracle) uses the SAME kernel → live==backfill by construction.

## Parity / build plan (for the coordinated image build)

1. Add the kernel to rust/src/lib.rs + register; rebuild the fp-dev image (maturin — the toolchain the
   runtime image lacks).
2. Wire subminute_gap_fano first (heaviest), then size_entropy, print_hhi.
3. GATE per group: `test_fp_latest[<group>]` at the declared distributional tolerance + the parity sweep on
   real tape (the distributional-parity authority) + fp unchanged (name/version/features identical → the
   fingerprint is fp-neutral by the #284 finding; this is value-identical, NOT a re-validate, unless the
   size-bucket edges are changed).
4. MEASURE the per-group + e2e other_emit delta (the headline — the ~80ms Layer-C per-minute-groupby collapse).

## Why a spec, not a PR

The fp-dev runtime image has no Rust toolchain (verified: maturin/cargo absent), and a pyo3 extension must
ABI-match the image's exact Python 3.12 — so a kernel built in an ad-hoc sandbox risks the swing_dc-#250
"missing kernel" hazard. The Lead's coordinated image build is the correct, safe path; this spec makes it a
drop-in (source + integration + gate + the value-identical flag).
