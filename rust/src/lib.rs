//! quant_tick — Rust kernels for sequential per-tick features that are infeasible to vectorize in
//! Python at 10k-ticker scale. Called identically from the live tape and the historical backfill via
//! one Python FeatureGroup, so parity holds by construction; a pure-Python reference pins the output
//! (tests/test_fp_rust.py). The kernel is a single ordered pass — exactly the shape Polars can't do.

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray3};
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
    symbol: PyReadonlyArray1<i64>,
    minute: PyReadonlyArray1<i64>,
    value: PyReadonlyArray1<f64>,
    windows: Vec<i64>,
    t: i64,
) -> PyResult<(
    Vec<i64>,
    Vec<i64>,
    Vec<f64>,
    Vec<f64>,
    Vec<f64>,
    Vec<f64>,
    Vec<f64>,
)> {
    let symbol = symbol.as_slice()?;
    let minute = minute.as_slice()?;
    let value = value.as_slice()?;
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
    Ok((
        out_sym, out_win, out_n, out_sum, out_sumsq, out_min, out_max,
    ))
}

/// Generic single-pass windowed SUMS of MANY columns at once — the kernel every reduction feature
/// group calls instead of one-buffer-scan-per-window Polars slicing. For each (symbol, window) it
/// returns the count + the sum of each value column, computed in ONE backward pass per symbol (snapshot
/// at each window boundary). The caller precomputes whatever columns it needs (x, y, x*y, x², r, r², r³,
/// r⁴, signed volume, …) and derives means / std / OLS slope-corr-r² / moments from the sums. Fresh
/// each minute (no accumulator drift). ~n_windows× less work than per-window slicing.
///
/// Inputs sorted by (symbol, minute). ``values`` is a list of columns (each len == n_rows). ``windows``
/// ascending seconds; ``t`` the latest minute (epoch s); window w covers minutes in (t-w, t].
/// Returns (out_symbol, out_window, n, sums) where ``sums`` has one column per input value column, each
/// flattened in (symbol, ascending-window) order.
#[pyfunction]
fn windowed_sums(
    symbol: PyReadonlyArray1<i64>,
    minute: PyReadonlyArray1<i64>,
    values: Vec<PyReadonlyArray1<f64>>,
    windows: Vec<i64>,
    t: i64,
) -> PyResult<(Vec<i64>, Vec<i64>, Vec<f64>, Vec<Vec<f64>>)> {
    let symbol = symbol.as_slice()?;
    let minute = minute.as_slice()?;
    let values: Vec<&[f64]> = values.iter().map(|v| v.as_slice().unwrap()).collect();
    let n_rows = symbol.len();
    let nw = windows.len();
    let nc = values.len();
    let mut out_sym: Vec<i64> = Vec::new();
    let mut out_win: Vec<i64> = Vec::new();
    let mut out_n: Vec<f64> = Vec::new();
    let mut out_sums: Vec<Vec<f64>> = (0..nc).map(|_| Vec::new()).collect();

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        let mut count = 0.0;
        let mut acc = vec![0.0f64; nc];
        let mut k: usize = 0;
        let mut r = j;
        while r > i {
            r -= 1;
            let d = t - minute[r];
            while k < nw && d >= windows[k] {
                out_sym.push(s);
                out_win.push(windows[k]);
                out_n.push(count);
                for c in 0..nc {
                    out_sums[c].push(acc[c]);
                }
                k += 1;
            }
            count += 1.0;
            for c in 0..nc {
                acc[c] += values[c][r];
            }
        }
        while k < nw {
            out_sym.push(s);
            out_win.push(windows[k]);
            out_n.push(count);
            for c in 0..nc {
                out_sums[c].push(acc[c]);
            }
            k += 1;
        }
        i = j;
    }
    Ok((out_sym, out_win, out_n, out_sums))
}

/// Per-symbol LAG values of many columns at the latest row — the slice-derive primitive that lets the
/// incremental engine drop Polars' costly ``shift(k).over("symbol")`` partitioning.
///
/// The incremental V2 slice-derive computes ~short-lag value columns over a tiny trailing slice. Every
/// per-symbol-grouped operation in that derive is a ``shift(k).over("symbol")`` (e.g. the prior close for
/// a one-minute return); the elementwise arithmetic around it needs no grouping. Polars still re-partitions
/// the whole slice by symbol for each shift expression, which DOMINATES the minute (~53ms at 1250×60). This
/// kernel resolves those shifts in ONE ordered pass so the engine can evaluate the derive globally (no
/// ``over``) on the one-row-per-symbol latest frame.
///
/// Inputs are PARALLEL arrays sorted by (symbol, minute):
///   symbol   — integer code per symbol (a contiguous block per symbol)
///   values   — a list of columns (each len == n_rows) to lag
///   max_lag  — the largest lag needed (lags 1..=max_lag are returned)
/// Returns, one row per symbol in ASCENDING first-seen symbol-block order:
///   out_symbol — the symbol code
///   out_lags   — ``values.len()`` columns, each a Vec<f64> of length n_symbols; column c holds, for every
///                symbol's LATEST row, the lag-(lag) value of input column c, flattened as
///                lag index 0..max_lag-1 outer? No — see layout below.
///
/// Layout of ``out_lags``: ``out_lags[c]`` is a flat Vec of length ``n_symbols * max_lag``; for symbol
/// index ``si`` (0-based, block order) and lag ``L`` in 1..=max_lag, the value sits at
/// ``out_lags[c][si * max_lag + (L - 1)]``. A lag that reaches before the symbol's first row in the slice
/// is ``f64::NAN`` — EXACTLY the ``null`` Polars ``shift(L).over("symbol")`` produces at warmup / a
/// missing prior bar. (The caller fills the lag columns into a frame and rewrites
/// ``col.shift(L).over("symbol")`` -> ``col(__lagL_<name>)``; the derive then runs with no partition.)
///
/// The slice is assumed minute-CONTIGUOUS per symbol (the engine's trailing buffer is), so the L-th prior
/// ROW is the L-th prior MINUTE — matching Polars ``shift`` (which is positional, not time-aware). Symbol
/// blocks must be contiguous and minute-ascending within a block (the engine sorts (symbol, minute)).
#[pyfunction]
fn slice_derive_lags(
    symbol: PyReadonlyArray1<i64>,
    values: Vec<PyReadonlyArray1<f64>>,
    max_lag: usize,
) -> PyResult<(Vec<i64>, Vec<Vec<f64>>)> {
    let symbol = symbol.as_slice()?;
    let values: Vec<&[f64]> = values.iter().map(|v| v.as_slice().unwrap()).collect();
    let n_rows = symbol.len();
    let nc = values.len();
    let mut out_sym: Vec<i64> = Vec::new();
    let mut out_lags: Vec<Vec<f64>> = (0..nc).map(|_| Vec::new()).collect();

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        // block [i, j) for this symbol; last row of the block (j-1) is the latest minute
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        let latest = j - 1;
        out_sym.push(s);
        for c in 0..nc {
            for lag in 1..=max_lag {
                // lag-L value of the latest row = value at row (latest - L), if still inside this block
                let val = if latest >= lag && (latest - lag) >= i {
                    values[c][latest - lag]
                } else {
                    f64::NAN
                };
                out_lags[c].push(val);
            }
        }
        i = j;
    }
    Ok((out_sym, out_lags))
}

/// Per-(symbol, window) trailing rolling EXTREMA (max AND min) ending at the latest minute T — the
/// rolling-extrema state KIND's fold moved into Rust so the per-symbol monotonic-deque loop in
/// ``ExtremaState`` (pure Python, O(symbols) per minute) leaves the critical path. One backward pass per
/// symbol snapshots each window's running max/min as the scan crosses its boundary (no per-window re-scan,
/// no deque, no hashing) — fresh each minute, so NO running-accumulator drift (parity-safe).
///
/// Inputs are PARALLEL arrays sorted by (symbol, minute):
///   symbol   — integer code per symbol (a contiguous block per symbol)
///   minute   — epoch seconds (per-minute)
///   value    — the column to take extrema of
///   windows  — window sizes in SECONDS, strictly ASCENDING
///   t        — the latest minute (epoch seconds); window w covers minutes in (t - w, t]
/// Returns, one entry per (symbol, window) in (symbol, ascending-window) order:
///   out_symbol, out_window, max, min
/// A window with no PRESENT (non-NaN) bar reads NaN for both — exactly what ``rolling_max_by`` /
/// ``rolling_min_by`` (and the ``ExtremaState`` empty-deque) produce. NaN input values are skipped (never
/// enter the max/min), matching the Python fold's ``value == value`` present-bar guard, so the caller may
/// pass NaN where a bar is absent (it is ignored) rather than pre-filtering.
#[pyfunction]
fn rolling_extrema(
    symbol: PyReadonlyArray1<i64>,
    minute: PyReadonlyArray1<i64>,
    value: PyReadonlyArray1<f64>,
    windows: Vec<i64>,
    t: i64,
) -> PyResult<(Vec<i64>, Vec<i64>, Vec<f64>, Vec<f64>)> {
    let symbol = symbol.as_slice()?;
    let minute = minute.as_slice()?;
    let value = value.as_slice()?;
    let n_rows = symbol.len();
    let nw = windows.len();
    let mut out_sym: Vec<i64> = Vec::new();
    let mut out_win: Vec<i64> = Vec::new();
    let mut out_max: Vec<f64> = Vec::new();
    let mut out_min: Vec<f64> = Vec::new();

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        let mut count = 0.0f64; // present (non-NaN) bars seen so far in the expanding window
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
                out_max.push(if count > 0.0 { mx } else { f64::NAN });
                out_min.push(if count > 0.0 { mn } else { f64::NAN });
                k += 1;
            }
            let v = value[r];
            if v == v {
                // present bar only (NaN absent-bar skipped, matching the Python fold)
                count += 1.0;
                if v < mn {
                    mn = v;
                }
                if v > mx {
                    mx = v;
                }
            }
        }
        while k < nw {
            out_sym.push(s);
            out_win.push(windows[k]);
            out_max.push(if count > 0.0 { mx } else { f64::NAN });
            out_min.push(if count > 0.0 { mn } else { f64::NAN });
            k += 1;
        }
        i = j;
    }
    Ok((out_sym, out_win, out_max, out_min))
}

/// Per-(symbol, lag) TIME-based lag gather at the latest minute T — the lag/last-k state KIND's read moved
/// into Rust so ``LastKState``'s per-symbol epoch-keyed ring leaves the Python critical path. For each
/// requested lag L (minutes), returns each symbol's value as of minute (T − L·60), or NaN when that EXACT
/// minute is absent for the symbol (the ``base.lagged`` self-join contract — time-based, correct on gappy
/// grids, NOT positional like ``slice_derive_lags``).
///
/// Inputs are PARALLEL arrays sorted by (symbol, minute):
///   symbol   — integer code per symbol (a contiguous block per symbol)
///   minute   — epoch seconds (per-minute)
///   values   — a list of columns (each len == n_rows) to lag
///   lags     — the lag offsets in SECONDS (one per requested (column-independent) lag), any order
///   t        — the latest minute (epoch seconds)
/// Returns, one row per symbol in ASCENDING first-seen symbol-block order:
///   out_symbol — the symbol code
///   out_lags   — ``values.len() * lags.len()`` columns of length n_symbols; the value of column ``c`` at
///                lag index ``li`` for symbol index ``si`` (block order) sits in
///                ``out_lags[c * n_lags + li][si]``. A target minute not present for the symbol is NaN
///                (the caller restores it to Polars null, matching the self-join).
///
/// Within each symbol block the scan walks backward from the latest row; because rows are minute-ascending
/// and unique per minute, a simple pointer per lag finds (or misses) the exact target epoch in one pass.
#[pyfunction]
fn time_lag_gather(
    symbol: PyReadonlyArray1<i64>,
    minute: PyReadonlyArray1<i64>,
    values: Vec<PyReadonlyArray1<f64>>,
    lags: Vec<i64>,
    t: i64,
) -> PyResult<(Vec<i64>, Vec<Vec<f64>>)> {
    let symbol = symbol.as_slice()?;
    let minute = minute.as_slice()?;
    let values: Vec<&[f64]> = values.iter().map(|v| v.as_slice().unwrap()).collect();
    let n_rows = symbol.len();
    let nc = values.len();
    let nl = lags.len();
    let mut out_sym: Vec<i64> = Vec::new();
    let mut out_lags: Vec<Vec<f64>> = (0..nc * nl).map(|_| Vec::new()).collect();

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        out_sym.push(s);
        for li in 0..nl {
            let target = t - lags[li];
            // find the row in [i, j) whose minute == target (block is minute-ascending, unique per minute)
            let mut found: Option<usize> = None;
            let mut r = j;
            while r > i {
                r -= 1;
                if minute[r] == target {
                    found = Some(r);
                    break;
                }
                if minute[r] < target {
                    break; // gone past it (ascending block) — the exact minute is absent
                }
            }
            for c in 0..nc {
                let val = match found {
                    Some(row) => values[c][row],
                    None => f64::NAN,
                };
                out_lags[c * nl + li].push(val);
            }
        }
        i = j;
    }
    Ok((out_sym, out_lags))
}

/// The CANONICAL reduction-emit moved into Rust — build a group set's per-(symbol, window) canonical
/// statistic columns DIRECTLY from the running per-(window, symbol, value-col) sums, eliminating the
/// per-column numpy Python overhead (and the polars wide-frame pivot) that dominates the fast-path emit.
///
/// This computes the SAME canonical columns ``emit_numpy`` (``_canonical_numpy`` / ``_ols_stat_numpy``)
/// produces, character-identical algebra, with ``f64::NAN`` exactly where the numpy/polars path emits a
/// null. The Python caller flattens its plan/reg_plan into one row per OUTPUT column (in the order the
/// caller wants the result columns), then slices the returned ``(n_symbols, n_out)`` matrix per group.
///
/// Inputs (all parallel, length ``n_out`` except ``running``):
///   running   — (n_windows, n_symbols, n_value_cols) running sums (``WindowedSumState.running``)
///   win       — per output column: the WINDOW INDEX (0..n_windows) to read from ``running``
///   kind      — per output column: the statistic to compute, coded:
///                 0 = sum     (idx0 = base value-col index)
///                 1 = mean    (idx0 = base, idx1 = base__p [presence count])
///                 2 = std     (idx0 = base, idx1 = base__p, idx2 = base__sq)
///                 3 = slope   (idx0..5 = b, x, y, xy, xx, yy)
///                 4 = corr    (idx0..5 = b, x, y, xy, xx, yy)
///                 5 = r2      (idx0..5 = b, x, y, xy, xx, yy)
///                 6 = mean_y  (idx0..5 = b, x, y, xy, xx, yy)
///   idx0..idx5 — value-col indices into ``running``'s last axis; only the ones the kind needs are read.
/// Returns a (n_symbols, n_out) row-major matrix; column j holds the kind[j] statistic over window win[j],
/// one value per symbol, NaN where the (numpy/polars) emit is null.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn assemble_canonical<'py>(
    py: Python<'py>,
    running: PyReadonlyArray3<f64>,
    win: Vec<usize>,
    kind: Vec<u8>,
    idx0: Vec<usize>,
    idx1: Vec<usize>,
    idx2: Vec<usize>,
    idx3: Vec<usize>,
    idx4: Vec<usize>,
    idx5: Vec<usize>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let running = running.as_array(); // (n_windows, n_symbols, n_value_cols)
    let n_sym = running.shape()[1];
    let n_out = win.len();
    let mut out: Array2<f64> = Array2::zeros((n_sym, n_out));

    for j in 0..n_out {
        let wi = win[j];
        let k = kind[j];
        let plane = running.index_axis(numpy::ndarray::Axis(0), wi); // (n_symbols, n_value_cols)
        match k {
            0 => {
                // sum: the raw running total (no guard — matches _canonical_numpy "__c_sum")
                let c0 = idx0[j];
                for s in 0..n_sym {
                    out[[s, j]] = plane[[s, c0]];
                }
            }
            1 => {
                // mean = sum/count, NaN where count <= 0 (matches _canonical_numpy guard count>0)
                let (c0, c1) = (idx0[j], idx1[j]);
                for s in 0..n_sym {
                    let total = plane[[s, c0]];
                    let count = plane[[s, c1]];
                    out[[s, j]] = if count > 0.0 { total / count } else { f64::NAN };
                }
            }
            2 => {
                // std(ddof=1) = sqrt((sumsq - total^2/count)/(count-1)); NaN where count <= 1
                let (c0, c1, c2) = (idx0[j], idx1[j], idx2[j]);
                for s in 0..n_sym {
                    let total = plane[[s, c0]];
                    let count = plane[[s, c1]];
                    let sumsq = plane[[s, c2]];
                    out[[s, j]] = if count > 1.0 {
                        ((sumsq - total * total / count) / (count - 1.0)).sqrt()
                    } else {
                        f64::NAN
                    };
                }
            }
            3..=6 => {
                // OLS: the six paired sums b, x, y, xy, xx, yy -> slope/corr/r2/mean_y
                let (cb, cx, cy, cxy, cxx, cyy) =
                    (idx0[j], idx1[j], idx2[j], idx3[j], idx4[j], idx5[j]);
                for s in 0..n_sym {
                    let b = plane[[s, cb]];
                    let sx = plane[[s, cx]];
                    let sy = plane[[s, cy]];
                    let sxy = plane[[s, cxy]];
                    let sxx = plane[[s, cxx]];
                    let syy = plane[[s, cyy]];
                    let denom_x = b * sxx - sx * sx;
                    let denom_y = b * syy - sy * sy;
                    let cov_n = b * sxy - sx * sy;
                    let defined = b >= 2.0 && denom_x > 0.0;
                    let defined_corr = defined && denom_y > 0.0;
                    out[[s, j]] = match k {
                        3 => {
                            if defined {
                                cov_n / denom_x
                            } else {
                                f64::NAN
                            }
                        }
                        4 => {
                            if defined_corr {
                                cov_n / (denom_x * denom_y).sqrt()
                            } else {
                                f64::NAN
                            }
                        }
                        5 => {
                            if defined_corr {
                                (cov_n * cov_n) / (denom_x * denom_y)
                            } else {
                                f64::NAN
                            }
                        }
                        _ => {
                            // mean_y = sy/b, NaN where b <= 0 (matches numpy guard b>0)
                            if b > 0.0 {
                                sy / b
                            } else {
                                f64::NAN
                            }
                        }
                    };
                }
            }
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "assemble_canonical: unknown kind {k}"
                )))
            }
        }
    }
    Ok(out.into_pyarray_bound(py))
}

/// POINT-IN-TIME SWING / ZIGZAG structure fold — a per-symbol O(1)-per-bar state machine that models the
/// up-down-up-down ("Fibonacci-style") swing structure of the close series and flags when it resolves into a
/// clean directional move. The ENTIRE reason this kernel exists is the look-ahead property: a standard ZigZag
/// REPAINTS (it confirms a pivot using FUTURE bars), so it cannot be used point-in-time. THIS fold confirms a
/// pivot ONLY once the theta-reversal has actually occurred by the current bar — it never reads a bar after the
/// row it emits. So the value emitted at minute T over a buffer ending at T is identical whether or not bars
/// after T exist (fold == reseed, live == backfill), which is the platform's parity invariant by construction.
///
/// A ZigZag filter ignores moves smaller than ``theta`` (a fractional return, e.g. 0.005 = 0.5%) and marks
/// PIVOTS (confirmed local extrema) where price reverses by >= theta from the running leg extreme. Between
/// pivots price runs one direction (a LEG). The current leg is PROVISIONAL: its extreme can still extend, so
/// the most recent pivot is the last CONFIRMED one, never the current running extreme.
///
/// Inputs are PARALLEL arrays sorted by (symbol, minute):
///   symbol     — integer code per symbol (a contiguous block per symbol)
///   minute     — epoch seconds (per-minute)
///   close      — the per-minute close
///   theta      — the reversal threshold as a fractional return (e.g. 0.005)
///   day_secs   — the seconds-of-day epoch boundary so ``n_pivots_today`` resets at the session date change
///                (a pivot's day = minute / 86400; counters reset when the row's day differs from the prior)
///   ring_k     — max confirmed pivots kept per symbol for the persistence / alternation reads (bounded ring)
/// Returns ONE ROW PER INPUT (symbol, minute) — the point-in-time fold value at THAT minute, in input order:
///   swing_dir         — +1 in a (provisional) up-leg, -1 in a down-leg, 0 before any direction is established
///   swing_steepness   — slope of the current leg as a per-minute fractional return:
///                        (close − leg_start_price)/leg_start_price / minutes_since_leg_start (0 at the start)
///   swing_len_pct     — current leg size as a signed fractional return from the leg start to ``close``
///   minutes_since_pivot — minutes since the last CONFIRMED pivot (since leg start), NaN before the first pivot
///   n_pivots_today    — count of confirmed pivots so far on the row's session day
///   n_alternations    — count of direction flips (each confirmed pivot is one alternation) over the kept ring
///   swing_persistence — net signed leg progression over the last ``ring_k`` legs: sum of signed leg returns
///                        (a clean trend has same-signed legs accumulating; chop cancels toward 0)
///   fib_retracement   — where ``close`` sits within the PRIOR completed leg's price range (the 0/0.382/0.5/
///                        0.618/1 read), measured from the prior leg's END back toward its START; NaN until a
///                        leg has completed (no prior leg yet)
///   trend_resolved    — 1.0 when, after tight alternation, the CURRENT leg exceeds the recent legs in BOTH
///                        length AND steepness AND its direction persists; else 0.0
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn swing_fold(
    symbol: PyReadonlyArray1<i64>,
    minute: PyReadonlyArray1<i64>,
    close: PyReadonlyArray1<f64>,
    theta: f64,
    day_secs: i64,
    ring_k: usize,
) -> PyResult<(
    Vec<f64>, // swing_dir
    Vec<f64>, // swing_steepness
    Vec<f64>, // swing_len_pct
    Vec<f64>, // minutes_since_pivot
    Vec<f64>, // n_pivots_today
    Vec<f64>, // n_alternations
    Vec<f64>, // swing_persistence
    Vec<f64>, // fib_retracement
    Vec<f64>, // trend_resolved
)> {
    let symbol = symbol.as_slice()?;
    let minute = minute.as_slice()?;
    let close = close.as_slice()?;
    let n_rows = symbol.len();

    let mut out_dir: Vec<f64> = vec![0.0; n_rows];
    let mut out_steep: Vec<f64> = vec![0.0; n_rows];
    let mut out_len: Vec<f64> = vec![0.0; n_rows];
    let mut out_msp: Vec<f64> = vec![f64::NAN; n_rows];
    let mut out_npt: Vec<f64> = vec![0.0; n_rows];
    let mut out_nalt: Vec<f64> = vec![0.0; n_rows];
    let mut out_pers: Vec<f64> = vec![0.0; n_rows];
    let mut out_fib: Vec<f64> = vec![f64::NAN; n_rows];
    let mut out_resolved: Vec<f64> = vec![0.0; n_rows];

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        // Per-symbol fold state (reset at each symbol block).
        let mut dir: i64 = 0; // 0 undirected, +1 up-leg, -1 down-leg
        let mut leg_start_price = f64::NAN;
        let mut leg_start_min: i64 = 0;
        let mut extreme = f64::NAN; // provisional running extreme of the current leg
        let mut extreme_min: i64 = 0;
        // undirected bootstrap: track BOTH a running high and low from the first bar; the first reversal of
        // theta from EITHER establishes the direction (and that extreme is the first confirmed pivot).
        let mut hi = f64::NAN;
        let mut hi_min: i64 = 0;
        let mut lo = f64::NAN;
        let mut lo_min: i64 = 0;
        // last COMPLETED leg geometry (for fib_retracement): the start & end price of the prior leg.
        let mut prev_leg_start = f64::NAN;
        let mut prev_leg_end = f64::NAN;
        let mut have_prev_leg = false;
        let mut n_pivots_today: f64 = 0.0;
        let mut cur_day: i64 = i64::MIN;
        // bounded ring of recent CONFIRMED leg signed returns (the per-leg net move), for persistence + the
        // trend-resolved length/steepness comparison.
        let mut leg_returns: std::collections::VecDeque<f64> = std::collections::VecDeque::new();
        let mut leg_steeps: std::collections::VecDeque<f64> = std::collections::VecDeque::new();
        let mut n_alternations: f64 = 0.0;

        let mut r = i;
        while r < j {
            let c = close[r];
            let m = minute[r];
            let day = m.div_euclid(day_secs);
            if day != cur_day {
                cur_day = day;
                n_pivots_today = 0.0;
            }

            if leg_start_price.is_nan() {
                // first bar of the block: seed everything, no direction yet.
                leg_start_price = c;
                leg_start_min = m;
                extreme = c;
                extreme_min = m;
                hi = c;
                hi_min = m;
                lo = c;
                lo_min = m;
            } else if dir == 0 {
                if c > hi {
                    hi = c;
                    hi_min = m;
                }
                if c < lo {
                    lo = c;
                    lo_min = m;
                }
                let down_rev = if hi > 0.0 { (hi - c) / hi } else { 0.0 };
                let up_rev = if lo > 0.0 { (c - lo) / lo } else { 0.0 };
                // pick the reversal that fired with the LARGER magnitude (deterministic if both cross).
                if down_rev >= theta && down_rev >= up_rev {
                    // we were rising; confirm a HIGH pivot at hi, flip to a down-leg starting from hi.
                    confirm_pivot(
                        hi,
                        leg_start_price,
                        hi_min - leg_start_min,
                        &mut prev_leg_start,
                        &mut prev_leg_end,
                        &mut have_prev_leg,
                        &mut leg_returns,
                        &mut leg_steeps,
                        ring_k,
                    );
                    n_pivots_today += 1.0;
                    n_alternations += 1.0;
                    dir = -1;
                    leg_start_price = hi;
                    leg_start_min = hi_min;
                    extreme = c;
                    extreme_min = m;
                } else if up_rev >= theta {
                    confirm_pivot(
                        lo,
                        leg_start_price,
                        lo_min - leg_start_min,
                        &mut prev_leg_start,
                        &mut prev_leg_end,
                        &mut have_prev_leg,
                        &mut leg_returns,
                        &mut leg_steeps,
                        ring_k,
                    );
                    n_pivots_today += 1.0;
                    n_alternations += 1.0;
                    dir = 1;
                    leg_start_price = lo;
                    leg_start_min = lo_min;
                    extreme = c;
                    extreme_min = m;
                }
            } else if dir == 1 {
                if c >= extreme {
                    extreme = c;
                    extreme_min = m;
                } else if extreme > 0.0 && (extreme - c) / extreme >= theta {
                    confirm_pivot(
                        extreme,
                        leg_start_price,
                        extreme_min - leg_start_min,
                        &mut prev_leg_start,
                        &mut prev_leg_end,
                        &mut have_prev_leg,
                        &mut leg_returns,
                        &mut leg_steeps,
                        ring_k,
                    );
                    n_pivots_today += 1.0;
                    n_alternations += 1.0;
                    dir = -1;
                    leg_start_price = extreme;
                    leg_start_min = extreme_min;
                    extreme = c;
                    extreme_min = m;
                }
            } else {
                // dir == -1 (down-leg): mirror.
                if c <= extreme {
                    extreme = c;
                    extreme_min = m;
                } else if extreme > 0.0 && (c - extreme) / extreme >= theta {
                    confirm_pivot(
                        extreme,
                        leg_start_price,
                        extreme_min - leg_start_min,
                        &mut prev_leg_start,
                        &mut prev_leg_end,
                        &mut have_prev_leg,
                        &mut leg_returns,
                        &mut leg_steeps,
                        ring_k,
                    );
                    n_pivots_today += 1.0;
                    n_alternations += 1.0;
                    dir = 1;
                    leg_start_price = extreme;
                    leg_start_min = extreme_min;
                    extreme = c;
                    extreme_min = m;
                }
            }

            // ---- emit the point-in-time features for THIS row from the (already-updated) state ----
            out_dir[r] = dir as f64;
            let len_pct = if leg_start_price > 0.0 {
                (c - leg_start_price) / leg_start_price
            } else {
                0.0
            };
            out_len[r] = len_pct;
            let mins = ((m - leg_start_min) / 60) as f64;
            out_steep[r] = if mins > 0.0 { len_pct / mins } else { 0.0 };
            // minutes_since_pivot is defined only once a pivot has been confirmed (dir != 0).
            out_msp[r] = if dir != 0 { mins } else { f64::NAN };
            out_npt[r] = n_pivots_today;
            out_nalt[r] = n_alternations;
            out_pers[r] = leg_returns.iter().sum::<f64>() + len_pct;
            // fib_retracement: where c sits within the PRIOR completed leg's [end..start] range. 0 at the
            // prior leg's END (the last confirmed pivot), 1 back at its START. NaN until a leg completes.
            if have_prev_leg && (prev_leg_start - prev_leg_end).abs() > 0.0 {
                out_fib[r] = (c - prev_leg_end) / (prev_leg_start - prev_leg_end);
            }
            // trend_resolved: after tight alternation (>= 2 prior legs in the ring), the CURRENT leg exceeds
            // the recent legs' median length AND steepness, AND its direction persists (sign of len matches dir).
            if leg_returns.len() >= 2 && dir != 0 {
                let cur_abs_len = len_pct.abs();
                let cur_abs_steep = out_steep[r].abs();
                let max_prior_len = leg_returns.iter().map(|x| x.abs()).fold(0.0_f64, f64::max);
                let max_prior_steep = leg_steeps.iter().map(|x| x.abs()).fold(0.0_f64, f64::max);
                let persists = (len_pct > 0.0 && dir == 1) || (len_pct < 0.0 && dir == -1);
                if persists && cur_abs_len > max_prior_len && cur_abs_steep > max_prior_steep {
                    out_resolved[r] = 1.0;
                }
            }
            r += 1;
        }
        i = j;
    }
    Ok((
        out_dir,
        out_steep,
        out_len,
        out_msp,
        out_npt,
        out_nalt,
        out_pers,
        out_fib,
        out_resolved,
    ))
}

/// Push a freshly CONFIRMED pivot: record the just-completed leg's geometry (start price -> pivot price) for
/// the fib read and the bounded persistence/steepness rings. The signed leg return is
/// (pivot − leg_start)/leg_start; the per-minute steepness is that return over the leg's minute span (the
/// SAME basis as the live ``swing_steepness`` so the trend-resolved length/steepness comparison is apples-to-
/// apples). ``leg_span_secs`` is the leg's (pivot_min − leg_start_min) in seconds.
#[allow(clippy::too_many_arguments)]
fn confirm_pivot(
    pivot_price: f64,
    leg_start_price: f64,
    leg_span_secs: i64,
    prev_leg_start: &mut f64,
    prev_leg_end: &mut f64,
    have_prev_leg: &mut bool,
    leg_returns: &mut std::collections::VecDeque<f64>,
    leg_steeps: &mut std::collections::VecDeque<f64>,
    ring_k: usize,
) {
    let signed_ret = if leg_start_price > 0.0 {
        (pivot_price - leg_start_price) / leg_start_price
    } else {
        0.0
    };
    let mins = (leg_span_secs / 60) as f64;
    let steep = if mins > 0.0 { signed_ret / mins } else { 0.0 };
    *prev_leg_start = leg_start_price;
    *prev_leg_end = pivot_price;
    *have_prev_leg = true;
    leg_returns.push_back(signed_ret);
    leg_steeps.push_back(steep);
    while leg_returns.len() > ring_k {
        leg_returns.pop_front();
    }
    while leg_steeps.len() > ring_k {
        leg_steeps.pop_front();
    }
}

#[pymodule]
fn quant_tick(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tick_run_features, m)?)?;
    m.add_function(wrap_pyfunction!(windowed_reduce, m)?)?;
    m.add_function(wrap_pyfunction!(windowed_sums, m)?)?;
    m.add_function(wrap_pyfunction!(slice_derive_lags, m)?)?;
    m.add_function(wrap_pyfunction!(rolling_extrema, m)?)?;
    m.add_function(wrap_pyfunction!(time_lag_gather, m)?)?;
    m.add_function(wrap_pyfunction!(assemble_canonical, m)?)?;
    m.add_function(wrap_pyfunction!(swing_fold, m)?)?;
    Ok(())
}
