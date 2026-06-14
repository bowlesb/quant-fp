//! quant_tick — Rust kernels for sequential per-tick features that are infeasible to vectorize in
//! Python at 10k-ticker scale. Called identically from the live tape and the historical backfill via
//! one Python FeatureGroup, so parity holds by construction; a pure-Python reference pins the output
//! (tests/test_fp_rust.py). The kernel is a single ordered pass — exactly the shape Polars can't do.

use pyo3::prelude::*;

/// Per-(symbol, minute) tick run-length + signed-flow features.
///
/// Inputs are PARALLEL arrays already sorted by (symbol, minute, ts):
///   symbol  — integer code per symbol
///   minute  — minute bucket (epoch seconds, truncated to the minute)
///   price   — trade price
///   size    — trade size
/// Returns (out_symbol, out_minute, max_signed_run, signed_run_count, signed_volume), one row per
/// (symbol, minute) group. Sign is the tick rule: +1 uptick, -1 downtick, 0 on the first trade or a
/// zero-tick (a zero-tick breaks a run). max_signed_run = longest consecutive same-sign run;
/// signed_run_count = number of runs; signed_volume = sum(sign * size).
#[pyfunction]
fn tick_run_features(
    symbol: Vec<i64>,
    minute: Vec<i64>,
    price: Vec<f64>,
    size: Vec<f64>,
) -> PyResult<(Vec<i64>, Vec<i64>, Vec<f64>, Vec<f64>, Vec<f64>)> {
    let n = symbol.len();
    let mut out_sym: Vec<i64> = Vec::new();
    let mut out_min: Vec<i64> = Vec::new();
    let mut out_max: Vec<f64> = Vec::new();
    let mut out_cnt: Vec<f64> = Vec::new();
    let mut out_sv: Vec<f64> = Vec::new();

    let mut i: usize = 0;
    while i < n {
        let s = symbol[i];
        let m = minute[i];
        let mut prev_price = f64::NAN;
        let mut cur_sign: i64 = 0;
        let mut cur_len: f64 = 0.0;
        let mut max_run: f64 = 0.0;
        let mut run_count: f64 = 0.0;
        let mut signed_vol: f64 = 0.0;

        while i < n && symbol[i] == s && minute[i] == m {
            let p = price[i];
            let sign: i64 = if prev_price.is_nan() {
                0
            } else if p > prev_price {
                1
            } else if p < prev_price {
                -1
            } else {
                0
            };
            if sign != 0 && sign == cur_sign {
                cur_len += 1.0;
            } else if sign != 0 {
                cur_sign = sign;
                cur_len = 1.0;
                run_count += 1.0;
            } else {
                cur_sign = 0;
                cur_len = 0.0;
            }
            if cur_len > max_run {
                max_run = cur_len;
            }
            signed_vol += (sign as f64) * size[i];
            prev_price = p;
            i += 1;
        }

        out_sym.push(s);
        out_min.push(m);
        out_max.push(max_run);
        out_cnt.push(run_count);
        out_sv.push(signed_vol);
    }
    Ok((out_sym, out_min, out_max, out_cnt, out_sv))
}

/// Per-(symbol, window) windowed reductions ending at the latest minute T — the reusable kernel a
/// reduction feature group calls instead of Polars rolling, to move its heavy compute into Rust.
///
/// Inputs are PARALLEL arrays sorted by (symbol, minute):
///   symbol   — integer code per symbol
///   minute   — epoch seconds (per-minute)
///   value    — the column to reduce
///   windows  — window sizes in SECONDS, strictly ASCENDING
///   t        — the latest minute (epoch seconds); window w covers minutes in (t - w, t]
/// Returns, one entry per (symbol, window) in (symbol, ascending-window) order:
///   out_symbol, out_window, n, sum, sumsq, min, max
/// The caller derives mean = sum/n, std(ddof=1) = sqrt((sumsq - sum*sum/n)/(n-1)), etc. A single
/// backward pass per symbol snapshots each window as the scan crosses its boundary (no per-window
/// re-scan, no hashing) — fresh each minute, so NO running-accumulator drift (parity-safe).
#[pyfunction]
fn windowed_reduce(
    symbol: Vec<i64>,
    minute: Vec<i64>,
    value: Vec<f64>,
    windows: Vec<i64>,
    t: i64,
) -> PyResult<(Vec<i64>, Vec<i64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>)> {
    let n_rows = symbol.len();
    let nw = windows.len();
    let mut out_sym: Vec<i64> = Vec::new();
    let mut out_win: Vec<i64> = Vec::new();
    let mut out_n: Vec<f64> = Vec::new();
    let mut out_sum: Vec<f64> = Vec::new();
    let mut out_sumsq: Vec<f64> = Vec::new();
    let mut out_min: Vec<f64> = Vec::new();
    let mut out_max: Vec<f64> = Vec::new();

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        // block [i, j) for this symbol
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        let mut count = 0.0;
        let mut sum = 0.0;
        let mut sumsq = 0.0;
        let mut mn = f64::INFINITY;
        let mut mx = f64::NEG_INFINITY;
        let mut k: usize = 0;
        // scan backward (increasing distance d = t - minute); snapshot window k when d >= windows[k]
        let mut r = j;
        while r > i {
            r -= 1;
            let d = t - minute[r];
            while k < nw && d >= windows[k] {
                out_sym.push(s);
                out_win.push(windows[k]);
                out_n.push(count);
                out_sum.push(sum);
                out_sumsq.push(sumsq);
                out_min.push(if count > 0.0 { mn } else { f64::NAN });
                out_max.push(if count > 0.0 { mx } else { f64::NAN });
                k += 1;
            }
            let v = value[r];
            count += 1.0;
            sum += v;
            sumsq += v * v;
            if v < mn {
                mn = v;
            }
            if v > mx {
                mx = v;
            }
        }
        while k < nw {
            out_sym.push(s);
            out_win.push(windows[k]);
            out_n.push(count);
            out_sum.push(sum);
            out_sumsq.push(sumsq);
            out_min.push(if count > 0.0 { mn } else { f64::NAN });
            out_max.push(if count > 0.0 { mx } else { f64::NAN });
            k += 1;
        }
        i = j;
    }
    Ok((out_sym, out_win, out_n, out_sum, out_sumsq, out_min, out_max))
}

#[pymodule]
fn quant_tick(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tick_run_features, m)?)?;
    m.add_function(wrap_pyfunction!(windowed_reduce, m)?)?;
    Ok(())
}
