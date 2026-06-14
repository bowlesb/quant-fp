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

#[pymodule]
fn quant_tick(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tick_run_features, m)?)?;
    Ok(())
}
