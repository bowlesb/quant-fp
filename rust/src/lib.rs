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
///                 7 = resid_std (idx0..5 = b, x, y, xy, xx, yy) — OLS residual std, percent of mean y
///   idx0..idx5 — value-col indices into ``running``'s last axis; only the ones the kind needs are read.
/// Returns a (n_symbols, n_out) row-major matrix; column j holds the kind[j] statistic over window win[j],
/// one value per symbol, NaN where the (numpy/polars) emit is null.
/// OLS residual std (percent of mean y) from the six paired sums — the rust twin of declarative.py's
/// ``_ols_stat_exprs``/``_ols_stat_numpy`` ``resid_std`` (the residual_analysis Lever-2 stat). Uses the EXACT
/// centered-sum algebra (Σ.._c = Σ.. − Σa·Σb/n) the hand-written group used so the difference-of-sums rounds
/// identically. Σr² = syy_c − slope·sxy_c (clipped ≥0); resid_var = Σr²/n; std% = √resid_var/ȳ·100. NaN
/// (==null) where n < 4 (``_RESID_MIN_POINTS``), the x-axis is flat (sxx_c ≤ 0), or resid_var is below the
/// relative floor (``_RESID_REL_FLOOR``² of mean y²) — mirrors declarative.py's resid_defined guard.
fn resid_std_pct(b: f64, sx: f64, sy: f64, sxy: f64, sxx: f64, syy: f64) -> f64 {
    if b <= 0.0 {
        return f64::NAN;
    }
    let sxx_c = sxx - sx * sx / b;
    let sxy_c = sxy - sx * sy / b;
    let syy_c = syy - sy * sy / b;
    if !(sxx_c > 0.0) {
        return f64::NAN;
    }
    let slope = sxy_c / sxx_c;
    let mut ssr = syy_c - slope * sxy_c;
    if ssr < 0.0 {
        ssr = 0.0;
    }
    let mean_y = sy / b;
    let resid_var = ssr / b;
    let floor = (1e-6 * mean_y) * (1e-6 * mean_y); // _RESID_REL_FLOOR² · ȳ²
    if b >= 4.0 && resid_var > floor {
        resid_var.sqrt() / mean_y * 100.0
    } else {
        f64::NAN
    }
}

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
            3..=7 => {
                // OLS: the six paired sums b, x, y, xy, xx, yy -> slope/corr/r2/mean_y/resid_std
                let (cb, cx, cy, cxy, cxx, cyy) =
                    (idx0[j], idx1[j], idx2[j], idx3[j], idx4[j], idx5[j]);
                for s in 0..n_sym {
                    let b = plane[[s, cb]];
                    let sx = plane[[s, cx]];
                    let sy = plane[[s, cy]];
                    let sxy = plane[[s, cxy]];
                    let sxx = plane[[s, cxx]];
                    let syy = plane[[s, cyy]];
                    // resid_std (kind 7) uses the EXACT centered-sum residual-std algebra (Σ.._c = Σ.. −
                    // Σa·Σb/n) the hand-written residual_analysis used, so the difference-of-sums rounds
                    // identically to the polars/numpy twins; computed separately below from these same sums.
                    if k == 7 {
                        out[[s, j]] = resid_std_pct(b, sx, sy, sxy, sxx, syy);
                        continue;
                    }
                    let denom_x = b * sxx - sx * sx;
                    let denom_y = b * syy - sy * sy;
                    let cov_n = b * sxy - sx * sy;
                    // Relative variance floors (mirror _OLS_DENOM_X/Y_REL_EPS in declarative.py): on a
                    // near-flat regressor (or regressand) denom_x/denom_y is a cancellation difference whose
                    // sign is float-noise, so a bare `> 0.0` would diverge from the polars/numpy paths. Gate
                    // each on a fraction of its own scale ((Σx)² / (Σy)²).
                    let defined = b >= 2.0 && denom_x > 1e-12 * (sx * sx);
                    let defined_corr = defined && denom_y > 1e-12 * (sy * sy);
                    // n==2 perfect-fit corner (mirror _OLS_PERFECT_FIT_COUNT in declarative.py): a line through
                    // two distinct points is an EXACT fit, so r2==1.0 and corr==sign(cov). From the sums the
                    // cov²/(denom_x·denom_y) ratio is noise/noise landing at 1.0±ε; emit the exact value so the
                    // rust/numpy/polars twins agree cell-for-cell AND the value is correct (touches only b==2).
                    let perfect = defined_corr && b == 2.0;
                    out[[s, j]] = match k {
                        3 => {
                            if defined {
                                cov_n / denom_x
                            } else {
                                f64::NAN
                            }
                        }
                        4 => {
                            if perfect {
                                cov_n.signum()
                            } else if defined_corr {
                                cov_n / (denom_x * denom_y).sqrt()
                            } else {
                                f64::NAN
                            }
                        }
                        5 => {
                            if perfect {
                                1.0
                            } else if defined_corr {
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

/// A bounded record of one COMPLETED directional-change leg (pivot-to-pivot "chunk") at one scale, kept in a
/// ring for the per-scale aggregates, the percentile read, and the persistence sum. All fields are sealed at
/// the moment the leg's terminal pivot confirms — point-in-time, never revised.
#[derive(Clone, Copy)]
struct DcLeg {
    signed_ret: f64, // (pivot_price - leg_start_price) / leg_start_price
    abs_ret: f64,
    slope: f64,    // signed_ret per minute over the leg's span
    dur_min: f64,  // leg span in minutes
    n_trades: f64, // sum of minute n_trades over the leg's bars
    mean_spread: f64,
    dir: i64, // +1 up-leg, -1 down-leg
}

/// Per-SCALE directional-change fold state. One independent DC state machine per threshold in the ladder,
/// all advanced in lockstep over the SAME single pass of the close series (they share only the incremental
/// volatility that scales their thresholds). ``mode`` is the current (provisional) leg direction; the running
/// ``ext`` is the provisional extreme that a future >= delta reversal would confirm as the next pivot.
struct ScaleState {
    mode: i64, // 0 undirected, +1 up-leg, -1 down-leg
    leg_start_price: f64,
    leg_start_min: i64,
    ext: f64,
    ext_min: i64,
    // undirected bootstrap: track a running high and low; the first delta-reversal from EITHER seeds direction.
    hi: f64,
    hi_min: i64,
    lo: f64,
    lo_min: i64,
    // prefix accumulators (since the symbol block start) of minute n_trades and minute mean_spread, plus a bar
    // count, so any leg's totals = (prefix at its end) - (prefix at its start). Snapshots are taken at leg
    // start and at the running extreme so a confirmed leg (which ends at the PAST extreme) gets its true totals.
    pre_ntrades: f64,
    pre_spread: f64,
    pre_bars: f64,
    snap_start_ntrades: f64,
    snap_start_spread: f64,
    snap_start_bars: f64,
    snap_ext_ntrades: f64,
    snap_ext_spread: f64,
    snap_ext_bars: f64,
    // undirected-bootstrap prefix snapshots taken AT the running hi / lo (so the first confirmed leg, which
    // ends at the past hi or lo, gets its true trades/spread totals — the directed-mode ``snap_ext`` analogue).
    snap_hi_ntrades: f64,
    snap_hi_spread: f64,
    snap_hi_bars: f64,
    snap_lo_ntrades: f64,
    snap_lo_spread: f64,
    snap_lo_bars: f64,
    // last COMPLETED leg geometry for the Fibonacci grid.
    prev_leg_start: f64,
    prev_leg_end: f64,
    prev_leg_dir: i64,
    have_prev_leg: bool,
    legs: std::collections::VecDeque<DcLeg>,
    n_legs_total: f64, // count of all confirmed legs this block (not just the ring)
    last_pivot_min: i64,
    have_pivot: bool,
    last_close: f64, // the most recent bar's close (the provisional price for the feature reads)
}

impl ScaleState {
    fn new() -> Self {
        ScaleState {
            mode: 0,
            leg_start_price: f64::NAN,
            leg_start_min: 0,
            ext: f64::NAN,
            ext_min: 0,
            hi: f64::NAN,
            hi_min: 0,
            lo: f64::NAN,
            lo_min: 0,
            pre_ntrades: 0.0,
            pre_spread: 0.0,
            pre_bars: 0.0,
            snap_start_ntrades: 0.0,
            snap_start_spread: 0.0,
            snap_start_bars: 0.0,
            snap_ext_ntrades: 0.0,
            snap_ext_spread: 0.0,
            snap_ext_bars: 0.0,
            snap_hi_ntrades: 0.0,
            snap_hi_spread: 0.0,
            snap_hi_bars: 0.0,
            snap_lo_ntrades: 0.0,
            snap_lo_spread: 0.0,
            snap_lo_bars: 0.0,
            prev_leg_start: f64::NAN,
            prev_leg_end: f64::NAN,
            prev_leg_dir: 0,
            have_prev_leg: false,
            legs: std::collections::VecDeque::new(),
            n_legs_total: 0.0,
            last_pivot_min: 0,
            have_pivot: false,
            last_close: f64::NAN,
        }
    }
}

/// Fibonacci ratios used for the retracement read against the last completed leg (measured from the leg's END
/// back toward its START): 0.382 / 0.5 / 0.618 / 0.786.
const FIB_RETR: [f64; 4] = [0.382, 0.5, 0.618, 0.786];
/// Degenerate-basis guard mirroring the existing swing group: when the prior leg's range is a near-zero
/// fraction the retracement ratio explodes; beyond this absolute magnitude the read is UNDEFINED (NaN -> null).
const FIB_DC_MAX_ABS: f64 = 10.0;

/// Snapshot the running prefix accumulators as the leg-START anchor (called whenever a new leg begins).
fn snap_leg_start(st: &mut ScaleState) {
    st.snap_start_ntrades = st.pre_ntrades;
    st.snap_start_spread = st.pre_spread;
    st.snap_start_bars = st.pre_bars;
}

/// Snapshot the running prefix accumulators as the current-EXTREME anchor (called whenever the extreme moves).
/// The leg that confirms at this extreme reads its totals from these snapshots — so a leg ending at a PAST
/// extreme still gets exactly the trades/spread that occurred up to that extreme, not up to the confirming bar.
fn snap_leg_ext(st: &mut ScaleState) {
    st.snap_ext_ntrades = st.pre_ntrades;
    st.snap_ext_spread = st.pre_spread;
    st.snap_ext_bars = st.pre_bars;
}

/// Seal a freshly confirmed leg (leg_start .. pivot at the extreme) into the ring + last-leg geometry.
#[allow(clippy::too_many_arguments)]
fn dc_confirm(
    st: &mut ScaleState,
    pivot_price: f64,
    pivot_min: i64,
    new_dir: i64,
    ring_k: usize,
) {
    let signed_ret = if st.leg_start_price > 0.0 {
        (pivot_price - st.leg_start_price) / st.leg_start_price
    } else {
        0.0
    };
    let dur_min = ((pivot_min - st.leg_start_min) / 60) as f64;
    let slope = if dur_min > 0.0 { signed_ret / dur_min } else { 0.0 };
    // leg totals = (snapshot at the extreme/pivot) - (snapshot at the leg start), clamped non-negative.
    let n_trades = (st.snap_ext_ntrades - st.snap_start_ntrades).max(0.0);
    let span_bars = (st.snap_ext_bars - st.snap_start_bars).max(0.0);
    let mean_spread = if span_bars > 0.0 {
        (st.snap_ext_spread - st.snap_start_spread) / span_bars
    } else {
        0.0
    };
    let leg_dir = -new_dir; // the leg that just ENDED ran opposite to the new leg's direction
    st.legs.push_back(DcLeg {
        signed_ret,
        abs_ret: signed_ret.abs(),
        slope,
        dur_min,
        n_trades,
        mean_spread,
        dir: leg_dir,
    });
    while st.legs.len() > ring_k {
        st.legs.pop_front();
    }
    st.n_legs_total += 1.0;
    st.prev_leg_start = st.leg_start_price;
    st.prev_leg_end = pivot_price;
    st.prev_leg_dir = leg_dir;
    st.have_prev_leg = true;
    st.last_pivot_min = pivot_min;
    st.have_pivot = true;
}

/// Advance ONE scale's DC state machine by one bar at price ``c`` / minute ``m`` with the scale's current
/// threshold ``delta``. Returns whether a pivot confirmed THIS bar (for the cross-scale coincidence read).
fn dc_step(st: &mut ScaleState, c: f64, m: i64, delta: f64, ring_k: usize) -> bool {
    let mut confirmed = false;
    st.last_close = c;
    if st.leg_start_price.is_nan() {
        st.leg_start_price = c;
        st.leg_start_min = m;
        st.ext = c;
        st.ext_min = m;
        st.hi = c;
        st.hi_min = m;
        st.lo = c;
        st.lo_min = m;
        snap_leg_start(st);
        snap_leg_ext(st);
        st.snap_hi_ntrades = st.pre_ntrades;
        st.snap_hi_spread = st.pre_spread;
        st.snap_hi_bars = st.pre_bars;
        st.snap_lo_ntrades = st.pre_ntrades;
        st.snap_lo_spread = st.pre_spread;
        st.snap_lo_bars = st.pre_bars;
        return false;
    }
    if st.mode == 0 {
        if c > st.hi {
            st.hi = c;
            st.hi_min = m;
            st.snap_hi_ntrades = st.pre_ntrades;
            st.snap_hi_spread = st.pre_spread;
            st.snap_hi_bars = st.pre_bars;
        }
        if c < st.lo {
            st.lo = c;
            st.lo_min = m;
            st.snap_lo_ntrades = st.pre_ntrades;
            st.snap_lo_spread = st.pre_spread;
            st.snap_lo_bars = st.pre_bars;
        }
        let down_rev = if st.hi > 0.0 { (st.hi - c) / st.hi } else { 0.0 };
        let up_rev = if st.lo > 0.0 { (c - st.lo) / st.lo } else { 0.0 };
        if down_rev >= delta && down_rev >= up_rev {
            // was rising: confirm a HIGH pivot at hi, start a down-leg from hi. The just-ended bootstrap leg
            // ran leg_start..hi, so its totals come from the hi-anchored snapshot.
            st.snap_ext_ntrades = st.snap_hi_ntrades;
            st.snap_ext_spread = st.snap_hi_spread;
            st.snap_ext_bars = st.snap_hi_bars;
            dc_confirm(st, st.hi, st.hi_min, -1, ring_k);
            st.mode = -1;
            st.leg_start_price = st.hi;
            st.leg_start_min = st.hi_min;
            // the new down-leg starts at hi; its start-snapshot is the hi-anchored snapshot.
            st.snap_start_ntrades = st.snap_hi_ntrades;
            st.snap_start_spread = st.snap_hi_spread;
            st.snap_start_bars = st.snap_hi_bars;
            st.ext = c;
            st.ext_min = m;
            snap_leg_ext(st);
            confirmed = true;
        } else if up_rev >= delta {
            st.snap_ext_ntrades = st.snap_lo_ntrades;
            st.snap_ext_spread = st.snap_lo_spread;
            st.snap_ext_bars = st.snap_lo_bars;
            dc_confirm(st, st.lo, st.lo_min, 1, ring_k);
            st.mode = 1;
            st.leg_start_price = st.lo;
            st.leg_start_min = st.lo_min;
            st.snap_start_ntrades = st.snap_lo_ntrades;
            st.snap_start_spread = st.snap_lo_spread;
            st.snap_start_bars = st.snap_lo_bars;
            st.ext = c;
            st.ext_min = m;
            snap_leg_ext(st);
            confirmed = true;
        }
    } else if st.mode == 1 {
        if c >= st.ext {
            st.ext = c;
            st.ext_min = m;
            snap_leg_ext(st);
        } else if st.ext > 0.0 && (st.ext - c) / st.ext >= delta {
            dc_confirm(st, st.ext, st.ext_min, -1, ring_k);
            st.mode = -1;
            st.leg_start_price = st.ext;
            st.leg_start_min = st.ext_min;
            // the new leg starts at the prior extreme; its start-snapshot is the prior ext-snapshot.
            st.snap_start_ntrades = st.snap_ext_ntrades;
            st.snap_start_spread = st.snap_ext_spread;
            st.snap_start_bars = st.snap_ext_bars;
            st.ext = c;
            st.ext_min = m;
            snap_leg_ext(st);
            confirmed = true;
        }
    } else {
        // mode == -1 (down-leg): mirror.
        if c <= st.ext {
            st.ext = c;
            st.ext_min = m;
            snap_leg_ext(st);
        } else if st.ext > 0.0 && (c - st.ext) / st.ext >= delta {
            dc_confirm(st, st.ext, st.ext_min, 1, ring_k);
            st.mode = 1;
            st.leg_start_price = st.ext;
            st.leg_start_min = st.ext_min;
            st.snap_start_ntrades = st.snap_ext_ntrades;
            st.snap_start_spread = st.snap_ext_spread;
            st.snap_start_bars = st.snap_ext_bars;
            st.ext = c;
            st.ext_min = m;
            snap_leg_ext(st);
            confirmed = true;
        }
    }
    confirmed
}

/// Number of per-scale features emitted (the per-scale chunk + Fibonacci block). Kept in lockstep with the
/// Python ``_PER_SCALE_COLS`` ordering; the pure-Python oracle pins this column-for-column.
const N_PER_SCALE: usize = 16;
/// Cross-scale (5) + threshold-response (4) + shared sigma observability (1) = 10 scale-agnostic features.
const N_GLOBAL: usize = 10;

/// Median of a non-empty slice's values via a cheap copy-sort (the ring is tiny, <= ring_k).
fn slice_median(values: &[f64]) -> f64 {
    if values.is_empty() {
        return f64::NAN;
    }
    let mut v: Vec<f64> = values.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        0.5 * (v[n / 2 - 1] + v[n / 2])
    }
}

/// Fraction of ring legs whose abs-height is strictly below ``x`` (the percentile rank of the just-completed
/// leg vs the name's recent leg-history at this scale). NaN when the ring is empty.
fn pctile_below(values: &[f64], x: f64) -> f64 {
    if values.is_empty() {
        return f64::NAN;
    }
    let below = values.iter().filter(|&&y| y < x).count() as f64;
    below / values.len() as f64
}

/// Point-in-time MULTI-SCALE directional-change (DC) intrinsic-time decomposition fold.
///
/// Inputs are PARALLEL arrays already sorted by (symbol, minute):
///   symbol         — integer code per symbol
///   minute         — minute bucket (epoch seconds)
///   close          — minute close
///   n_trades       — minute trade count (from minute_agg; per-leg trade totals)
///   mean_spread    — minute mean spread in bps (from minute_agg; per-leg quote activity)
///   scales         — the volatility-multiple ladder, e.g. [0.5, 1.0, 2.0, 4.0]
///   vol_win        — trailing minutes for the realized per-minute log-return sigma
///   theta_floor    — minimum threshold (fraction) so a flat name doesn't pivot on every tick
///   theta_cap      — maximum threshold (fraction) so a garbage print can't set an absurd threshold
///   ring_k         — confirmed legs kept per scale (>= the 3 detailed + percentile history)
///   day_secs       — seconds per session day (intraday-only resets — DC state persists; legs reset per day)
///
/// Emits one row per input bar, each a flat vector of N_PER_SCALE*n_scales per-scale features followed by
/// N_GLOBAL scale-agnostic features. The fold reads only bars <= the emitted bar, so a pivot is confirmed only
/// once the delta-reversal has ACTUALLY occurred — the current leg is always provisional (no look-ahead).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn swing_dc_fold(
    symbol: PyReadonlyArray1<i64>,
    minute: PyReadonlyArray1<i64>,
    close: PyReadonlyArray1<f64>,
    n_trades: PyReadonlyArray1<f64>,
    mean_spread: PyReadonlyArray1<f64>,
    scales: Vec<f64>,
    vol_win: usize,
    theta_floor: f64,
    theta_cap: f64,
    ring_k: usize,
    day_secs: i64,
) -> PyResult<Vec<Vec<f64>>> {
    let symbol = symbol.as_slice()?;
    let minute = minute.as_slice()?;
    let close = close.as_slice()?;
    let n_trades = n_trades.as_slice()?;
    let mean_spread = mean_spread.as_slice()?;
    let n_rows = symbol.len();
    let n_scales = scales.len();
    let total_cols = N_PER_SCALE * n_scales + N_GLOBAL;
    let mut out: Vec<Vec<f64>> = (0..total_cols).map(|_| vec![f64::NAN; n_rows]).collect();

    let mut i: usize = 0;
    while i < n_rows {
        let s = symbol[i];
        let mut j = i;
        while j < n_rows && symbol[j] == s {
            j += 1;
        }
        let mut states: Vec<ScaleState> = (0..n_scales).map(|_| ScaleState::new()).collect();
        // shared incremental sigma over the trailing vol_win one-minute LOG returns.
        let mut ret_ring: std::collections::VecDeque<f64> = std::collections::VecDeque::new();
        let mut prev_close = f64::NAN;
        let mut cur_day: i64 = i64::MIN;

        let mut r = i;
        while r < j {
            let c = close[r];
            let m = minute[r];
            let nt = n_trades[r];
            let sp = mean_spread[r];
            let day = m.div_euclid(day_secs);
            if day != cur_day {
                cur_day = day;
                // intraday-only: reset per-scale DC state + sigma history at the session boundary so a leg
                // never straddles the overnight gap (mirrors n_pivots_today's daily reset in swing).
                states = (0..n_scales).map(|_| ScaleState::new()).collect();
                ret_ring.clear();
                prev_close = f64::NAN;
            }

            // sigma update (point-in-time: the return into THIS bar, then the trailing-window std).
            if !prev_close.is_nan() && prev_close > 0.0 && c > 0.0 {
                ret_ring.push_back((c / prev_close).ln());
                while ret_ring.len() > vol_win {
                    ret_ring.pop_front();
                }
            }
            prev_close = c;
            let sigma = if ret_ring.len() >= 2 {
                let n = ret_ring.len() as f64;
                let mean = ret_ring.iter().sum::<f64>() / n;
                let var = ret_ring.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0);
                var.sqrt()
            } else {
                f64::NAN
            };

            // advance every scale by this bar, tracking which confirmed a pivot this minute.
            let mut pivot_now: Vec<bool> = vec![false; n_scales];
            for (si, st) in states.iter_mut().enumerate() {
                st.pre_ntrades += nt;
                st.pre_spread += sp;
                st.pre_bars += 1.0;
                let delta = if sigma.is_nan() {
                    theta_floor // before sigma exists, use the floor so structure can still seed
                } else {
                    (scales[si] * sigma).clamp(theta_floor, theta_cap)
                };
                pivot_now[si] = dc_step(st, c, m, delta, ring_k);
            }

            // ---- emit the point-in-time 74-vector for THIS bar ----
            emit_row(
                &mut out, r, &states, &scales, sigma, m, &pivot_now, theta_floor, theta_cap,
            );
            r += 1;
        }
        i = j;
    }
    Ok(out)
}

/// The Fibonacci reads against the last completed leg (P0 = prev_leg_start, P1 = prev_leg_end), evaluated at
/// the provisional ``cur_price``. Returns, in order:
///   fib_retr_now    — current pullback as a fraction of the prior leg's range, measured from P1 back to P0
///                     (0 at the leg END, 1 a full retrace to the leg START). NaN until a leg completes or on
///                     a degenerate near-zero-range basis (|read| > FIB_DC_MAX_ABS -> NaN).
///   fib_in_golden   — 1.0 if cur sits in the 61.8..78.6% retracement band of the prior leg, else 0.0.
///   fib_holding_618 — 1.0 if retraced to ~[0.5, 0.786] AND the provisional leg has resumed in the PRIOR leg's
///                     direction (a held golden-ratio pullback that resumed), else 0.0.
///   fib_broke_786   — 1.0 if cur retraced PAST 78.6% (setup invalidated / likely full reversal), else 0.0.
///   fib_ext_progress— travel toward the 127.2/161.8 extension once cur passes P1 in the prior-leg direction
///                     (0 at P1, ~1 at 161.8%); NaN until a leg completes; 0 before any extension.
///   fib_dist_nearest— signed distance (in leg-range fractions) from fib_retr_now to the nearest FIB_RETR
///                     level (small magnitude = sitting on a level). NaN with fib_retr_now.
///   fib_setup_long  — 1.0 when the prior leg was UP (P1>P0) AND cur is in the golden zone AND the provisional
///                     leg has turned back up (cur_dir==+1): "right at the beginning of a likely-UP chunk".
fn fib_reads(
    st: &ScaleState,
    cur_price: f64,
    last_dir: f64,
    cur_dir: f64,
) -> (f64, f64, f64, f64, f64, f64, f64) {
    let nan = f64::NAN;
    if !st.have_prev_leg {
        return (nan, 0.0, 0.0, 0.0, nan, nan, 0.0);
    }
    let p0 = st.prev_leg_start;
    let p1 = st.prev_leg_end;
    let range = p1 - p0;
    if range.abs() <= 0.0 {
        return (nan, 0.0, 0.0, 0.0, nan, nan, 0.0);
    }
    // retracement fraction: 0 at P1 (the pivot/leg end), 1 back at P0 (the leg start).
    let retr = (p1 - cur_price) / range;
    if !retr.is_finite() || retr.abs() > FIB_DC_MAX_ABS {
        return (nan, 0.0, 0.0, 0.0, nan, nan, 0.0);
    }
    let in_golden = if (0.618..=0.786).contains(&retr) { 1.0 } else { 0.0 };
    // The last completed leg is the most recent counter-trend move (a "pullback"); the CONTINUATION resumes in
    // the OPPOSITE direction (a completed DOWN pullback continues UP, and vice versa). A long continuation =
    // the completed leg was DOWN and the provisional leg has resumed UP.
    let down_leg = last_dir < 0.0;
    let up_leg = last_dir > 0.0;
    let resumed = (down_leg && cur_dir > 0.0) || (up_leg && cur_dir < 0.0);
    let holding_618 = if (0.5..=0.786).contains(&retr) && resumed { 1.0 } else { 0.0 };
    let broke_786 = if retr > 0.786 { 1.0 } else { 0.0 };
    // extension progress: cur extended the completed leg PAST its own end P1 in the leg's direction. retr < 0
    // means cur passed P1; map retr in (0 .. -0.618) to (0 .. 1): 127.2% ext -> retr -0.272, 161.8% -> -0.618.
    let ext_progress = if retr < 0.0 {
        (-retr / 0.618).clamp(0.0, 1.0)
    } else {
        0.0
    };
    // nearest standard retracement level + signed distance.
    let mut nearest = FIB_RETR[0];
    let mut best = (retr - FIB_RETR[0]).abs();
    for &lvl in FIB_RETR.iter().skip(1) {
        let d = (retr - lvl).abs();
        if d < best {
            best = d;
            nearest = lvl;
        }
    }
    let dist_nearest = retr - nearest;
    // "right at the beginning of a likely-UP chunk": a DOWN pullback retraced into the golden zone and the
    // provisional leg has turned back UP.
    let setup_long = if down_leg && in_golden > 0.0 && cur_dir > 0.0 { 1.0 } else { 0.0 };
    (retr, in_golden, holding_618, broke_786, ext_progress, dist_nearest, setup_long)
}

/// The threshold-RESPONSE signature across the scale ladder (parameter-free roughness fingerprint):
///   nlegs_slope    — OLS slope of log(1+n_legs) on log(delta) across scales (how fast leg-count falls as the
///                    threshold coarsens; the empirical DC scaling-law exponent — steeper = choppier path).
///   chunk_slope    — OLS slope of log(median |leg height|) on log(delta) across scales.
///   os_ratio_mean  — mean across scales of the provisional overshoot/delta ratio (the overshoot scaling-law
///                    observable; ~1 normal, >1 trending, <1 mean-reverting recently).
///   roughness      — n_legs at the finest scale / n_legs at the coarsest (high = rough/choppy).
/// NaN where there is insufficient cross-scale structure (no sigma yet, or no legs anywhere).
fn response_signature(
    states: &[ScaleState],
    scales: &[f64],
    sigma: f64,
    theta_floor: f64,
    theta_cap: f64,
) -> (f64, f64, f64, f64) {
    let n = states.len();
    if n < 2 || sigma.is_nan() {
        return (f64::NAN, f64::NAN, f64::NAN, f64::NAN);
    }
    let mut log_delta: Vec<f64> = Vec::with_capacity(n);
    let mut log_nlegs: Vec<f64> = Vec::with_capacity(n);
    let mut log_chunk: Vec<f64> = Vec::with_capacity(n);
    let mut os_ratios: Vec<f64> = Vec::new();
    let mut any_legs = false;
    for (si, st) in states.iter().enumerate() {
        let delta = (scales[si] * sigma).clamp(theta_floor, theta_cap);
        log_delta.push(delta.ln());
        log_nlegs.push((1.0 + st.n_legs_total).ln());
        let med = slice_median(&st.legs.iter().map(|l| l.abs_ret).collect::<Vec<f64>>());
        // floor the median at a tiny positive so the log is finite when the ring is empty/zero.
        let med_f = if med.is_finite() && med > 0.0 { med } else { 1e-9 };
        log_chunk.push(med_f.ln());
        if st.n_legs_total > 0.0 {
            any_legs = true;
        }
        if st.mode != 0 && delta > 0.0 {
            let cur_price = st.last_close;
            let cur_height = if st.leg_start_price > 0.0 {
                ((cur_price - st.leg_start_price) / st.leg_start_price).abs()
            } else {
                0.0
            };
            os_ratios.push(cur_height / delta);
        }
    }
    if !any_legs {
        return (f64::NAN, f64::NAN, f64::NAN, f64::NAN);
    }
    let nlegs_slope = ols_slope(&log_delta, &log_nlegs);
    let chunk_slope = ols_slope(&log_delta, &log_chunk);
    let os_ratio_mean = if os_ratios.is_empty() {
        f64::NAN
    } else {
        os_ratios.iter().sum::<f64>() / os_ratios.len() as f64
    };
    let finest = states.first().map(|s| s.n_legs_total).unwrap_or(0.0);
    let coarsest = states.last().map(|s| s.n_legs_total).unwrap_or(0.0);
    let roughness = if coarsest > 0.0 {
        finest / coarsest
    } else if finest > 0.0 {
        // coarsest found no legs but the finest did — maximal roughness; cap to keep it finite.
        finest
    } else {
        f64::NAN
    };
    (nlegs_slope, chunk_slope, os_ratio_mean, roughness)
}

/// Ordinary-least-squares slope of y on x (both length n >= 2). NaN if x has zero variance.
fn ols_slope(x: &[f64], y: &[f64]) -> f64 {
    let n = x.len() as f64;
    if n < 2.0 {
        return f64::NAN;
    }
    let mx = x.iter().sum::<f64>() / n;
    let my = y.iter().sum::<f64>() / n;
    let mut sxx = 0.0;
    let mut sxy = 0.0;
    for k in 0..x.len() {
        sxx += (x[k] - mx) * (x[k] - mx);
        sxy += (x[k] - mx) * (y[k] - my);
    }
    if sxx <= 0.0 {
        f64::NAN
    } else {
        sxy / sxx
    }
}

/// Write one bar's flat feature vector from the (already-updated) per-scale states. Kept in a helper so the
/// column ordering lives in ONE place; the Python oracle mirrors this exact ordering.
#[allow(clippy::too_many_arguments)]
fn emit_row(
    out: &mut [Vec<f64>],
    r: usize,
    states: &[ScaleState],
    scales: &[f64],
    sigma: f64,
    m: i64,
    pivot_now: &[bool],
    theta_floor: f64,
    theta_cap: f64,
) {
    let n_scales = states.len();
    // per-scale block
    let mut dir_signs: Vec<f64> = vec![0.0; n_scales];
    let mut nlegs_each: Vec<f64> = vec![0.0; n_scales];
    let mut setup_long_each: Vec<f64> = vec![0.0; n_scales];
    for (si, st) in states.iter().enumerate() {
        let base = si * N_PER_SCALE;
        let delta = if sigma.is_nan() {
            theta_floor
        } else {
            (scales[si] * sigma).clamp(theta_floor, theta_cap)
        };
        // provisional current leg
        let cur_dir = st.mode as f64;
        dir_signs[si] = cur_dir;
        let cur_price = st.last_close;
        let cur_height = if st.leg_start_price > 0.0 {
            (cur_price - st.leg_start_price) / st.leg_start_price
        } else {
            0.0
        };
        let minutes_since_dc = if st.have_pivot {
            ((m - st.last_pivot_min) / 60) as f64
        } else {
            f64::NAN
        };
        // last completed leg descriptors (from the ring's back)
        let (last_h, last_slope, last_dur, last_nt, last_sp, last_dir) = match st.legs.back() {
            Some(leg) => (leg.signed_ret, leg.slope, leg.dur_min, leg.n_trades, leg.mean_spread, leg.dir as f64),
            None => (f64::NAN, f64::NAN, f64::NAN, f64::NAN, f64::NAN, 0.0),
        };
        // percentile of the last completed leg's |height| vs the ring (excluding itself)
        let last_pctile = match st.legs.back() {
            Some(leg) => {
                let others: Vec<f64> = st
                    .legs
                    .iter()
                    .take(st.legs.len().saturating_sub(1))
                    .map(|l| l.abs_ret)
                    .collect();
                pctile_below(&others, leg.abs_ret)
            }
            None => f64::NAN,
        };
        // overshoot-to-dc ratio: current provisional overshoot magnitude / the scale's delta.
        let os_to_dc = if st.mode != 0 && delta > 0.0 {
            cur_height.abs() / delta
        } else {
            f64::NAN
        };
        // persistence: net signed leg progression over the ring plus the provisional leg.
        let persistence: f64 = st.legs.iter().map(|l| l.signed_ret).sum::<f64>() + cur_height;
        nlegs_each[si] = st.n_legs_total;

        // Fibonacci grid on the last completed leg (start P0 .. end P1), reading where cur_price sits.
        let (fib_retr, fib_golden, fib_hold618, fib_broke786, fib_ext, fib_dist, fib_setup_long) =
            fib_reads(st, cur_price, last_dir, cur_dir);
        setup_long_each[si] = fib_setup_long;

        // write the 16 per-scale features in the fixed order (mirrored by the oracle).
        out[base][r] = cur_dir;
        out[base + 1][r] = minutes_since_dc;
        out[base + 2][r] = last_h;
        out[base + 3][r] = last_slope;
        out[base + 4][r] = last_dur;
        out[base + 5][r] = last_nt;
        out[base + 6][r] = last_sp;
        out[base + 7][r] = last_pctile;
        out[base + 8][r] = os_to_dc;
        out[base + 9][r] = persistence;
        out[base + 10][r] = fib_retr;
        out[base + 11][r] = fib_golden;
        out[base + 12][r] = fib_hold618;
        out[base + 13][r] = fib_broke786;
        out[base + 14][r] = fib_ext;
        out[base + 15][r] = fib_dist;
    }

    // ---- global block ----
    let g = N_PER_SCALE * n_scales;
    // (b) cross-scale consistency
    let nonzero: Vec<f64> = dir_signs.iter().cloned().filter(|d| *d != 0.0).collect();
    let agree_frac = if nonzero.is_empty() {
        f64::NAN
    } else {
        let pos = nonzero.iter().filter(|d| **d > 0.0).count() as f64;
        let neg = nonzero.iter().filter(|d| **d < 0.0).count() as f64;
        pos.max(neg) / n_scales as f64
    };
    let pos = dir_signs.iter().filter(|d| **d > 0.0).count() as f64;
    let neg = dir_signs.iter().filter(|d| **d < 0.0).count() as f64;
    let dominant = if pos > neg {
        1.0
    } else if neg > pos {
        -1.0
    } else {
        0.0
    };
    let setup_count: f64 = setup_long_each.iter().sum();
    let pivot_coincidence: f64 = pivot_now.iter().filter(|p| **p).count() as f64;
    // finest-only: structure (a directed leg) at the finest scale (index 0) but not at the >= 2x scales.
    let finest_dir = dir_signs.first().cloned().unwrap_or(0.0);
    let coarse_directed = dir_signs.iter().skip(1).any(|d| *d != 0.0);
    let finest_only = if finest_dir != 0.0 && !coarse_directed { 1.0 } else { 0.0 };
    out[g][r] = agree_frac;
    out[g + 1][r] = dominant;
    out[g + 2][r] = setup_count;
    out[g + 3][r] = pivot_coincidence;
    out[g + 4][r] = finest_only;

    // (c) threshold-response signature: slope of log(n_legs) and log(mean chunk) vs log(delta) across scales,
    // the mean overshoot/dc ratio, and a roughness ratio (finest n_legs / coarsest n_legs).
    let (nlegs_slope, chunk_slope, os_ratio_mean, roughness) =
        response_signature(states, scales, sigma, theta_floor, theta_cap);
    out[g + 5][r] = nlegs_slope;
    out[g + 6][r] = chunk_slope;
    out[g + 7][r] = os_ratio_mean;
    out[g + 8][r] = roughness;
    // (d) sigma observability in bps
    out[g + 9][r] = if sigma.is_nan() { f64::NAN } else { sigma * 10_000.0 };
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
    m.add_function(wrap_pyfunction!(swing_dc_fold, m)?)?;
    Ok(())
}
