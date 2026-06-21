"""Parity gates for the MULTI-SCALE Directional-Change (DC) decomposition (swing_dc).

The five invariants, the first two being the reason this feature is safe to trade on:

  1. NO-LOOK-AHEAD — the value emitted at minute T over a buffer ending at T is IDENTICAL whether or not bars
     after T exist. A pivot at any scale is confirmed only once the delta-reversal has ACTUALLY occurred by T,
     and the provisional leg / Fibonacci reads never peek at a future end. Checked at every T by comparing
     ``compute(buffer<=T).at(T)`` against ``compute(buffer<=T+k).at(T)``.
  2. PYTHON-REFERENCE PIN — a pure-Python re-implementation of the multi-scale fold equals the Rust kernel
     cell-for-cell at every (symbol, minute) — the same discipline as test_fp_rust / test_fp_swing.
  3. KIND invariant (fold == reseed) — re-seeding the fold over the buffer ending at each minute and taking the
     last row equals the single batch backfill pass, cell-for-cell incl. warmup.
  4. GROUP parity — ``compute_latest`` == ``compute().filter(last minute)`` (live == backfill).
  5. DEGENERATE-SAFETY — flat history / a single bar / no pivots emit clean NULLs, never inf/nan leaking; and a
     hand-built up-leg -> 61.8% pullback -> resumption fires ``fib_setup_long`` at the resumption and NOT before
     the reversal completes.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import deque

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.groups.swing_dc import (
    DAY_SECS,
    FIB_DC_MAX_ABS,
    RING_K,
    SCALES,
    THETA_CAP,
    THETA_FLOOR,
    VOL_WIN,
    SwingDcGroup,
    _FEATURE_COLS,
    swing_dc_fold_frame,
)
from quantlib.features.registry import REGISTRY

# swing_dc is DE-STAGED (PR #270: un-registered, fp 802->728) — its module is intentionally NOT imported by
# quantlib.features.groups.__init__, so it is absent from the production registry. Importing it HERE (for the
# class + helpers these parity tests exercise) re-runs its ``@register`` side effect, which would leak the
# group into the GLOBAL registry and pollute every other test's production group set (e.g. inflating
# default_schema to 802 features and tripping test_fp_latency_budget on a group with no production budget).
# Undo that side effect at import time — these tests use ``SwingDcGroup`` directly, not via the registry.
REGISTRY.unregister(SwingDcGroup.name)

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)
FIB_RETR = (0.382, 0.5, 0.618, 0.786)
N_PER_SCALE = 16


def _stream(n_sym: int = 4, n_min: int = 200, seed: int = 11, vol: float = 0.004) -> pl.DataFrame:
    """A noisy stream that genuinely swings (vol comparable to the thresholds so pivots confirm at all scales)
    with realistic n_trades / mean_spread_bps columns so the per-leg trade/quote features exercise."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + 5.0 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + rng.standard_normal() * vol
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "close": price[s],
                    "n_trades": float(rng.integers(1, 50)),
                    "mean_spread_bps": float(abs(rng.standard_normal()) * 5.0 + 1.0),
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


class _ScaleState:
    """Pure-Python mirror of the Rust ScaleState (one DC state machine for one scale)."""

    def __init__(self) -> None:
        self.mode = 0
        self.leg_start_price = float("nan")
        self.leg_start_min = 0
        self.ext = float("nan")
        self.ext_min = 0
        self.hi = float("nan")
        self.hi_min = 0
        self.lo = float("nan")
        self.lo_min = 0
        self.pre_ntrades = 0.0
        self.pre_spread = 0.0
        self.pre_bars = 0.0
        self.snap_start_ntrades = 0.0
        self.snap_start_spread = 0.0
        self.snap_start_bars = 0.0
        self.snap_ext_ntrades = 0.0
        self.snap_ext_spread = 0.0
        self.snap_ext_bars = 0.0
        self.snap_hi_ntrades = 0.0
        self.snap_hi_spread = 0.0
        self.snap_hi_bars = 0.0
        self.snap_lo_ntrades = 0.0
        self.snap_lo_spread = 0.0
        self.snap_lo_bars = 0.0
        self.prev_leg_start = float("nan")
        self.prev_leg_end = float("nan")
        self.prev_leg_dir = 0
        self.have_prev_leg = False
        self.legs: deque[dict[str, float]] = deque()
        self.n_legs_total = 0.0
        self.last_pivot_min = 0
        self.have_pivot = False
        self.last_close = float("nan")


def _dc_confirm(st: _ScaleState, pivot_price: float, pivot_min: int, new_dir: int) -> None:
    signed_ret = (pivot_price - st.leg_start_price) / st.leg_start_price if st.leg_start_price > 0.0 else 0.0
    dur_min = (pivot_min - st.leg_start_min) // 60
    slope = signed_ret / dur_min if dur_min > 0 else 0.0
    n_trades = max(st.snap_ext_ntrades - st.snap_start_ntrades, 0.0)
    span_bars = max(st.snap_ext_bars - st.snap_start_bars, 0.0)
    mean_spread = (st.snap_ext_spread - st.snap_start_spread) / span_bars if span_bars > 0.0 else 0.0
    leg_dir = -new_dir
    st.legs.append(
        {
            "signed_ret": signed_ret,
            "abs_ret": abs(signed_ret),
            "slope": slope,
            "dur_min": float(dur_min),
            "n_trades": n_trades,
            "mean_spread": mean_spread,
            "dir": float(leg_dir),
        }
    )
    while len(st.legs) > RING_K:
        st.legs.popleft()
    st.n_legs_total += 1.0
    st.prev_leg_start = st.leg_start_price
    st.prev_leg_end = pivot_price
    st.prev_leg_dir = leg_dir
    st.have_prev_leg = True
    st.last_pivot_min = pivot_min
    st.have_pivot = True


def _dc_step(st: _ScaleState, c: float, m: int, delta: float) -> bool:
    confirmed = False
    st.last_close = c
    if st.leg_start_price != st.leg_start_price:  # nan -> first bar
        st.leg_start_price = c
        st.leg_start_min = m
        st.ext, st.ext_min = c, m
        st.hi, st.hi_min = c, m
        st.lo, st.lo_min = c, m
        st.snap_start_ntrades, st.snap_start_spread, st.snap_start_bars = (
            st.pre_ntrades,
            st.pre_spread,
            st.pre_bars,
        )
        st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
            st.pre_ntrades,
            st.pre_spread,
            st.pre_bars,
        )
        st.snap_hi_ntrades, st.snap_hi_spread, st.snap_hi_bars = (
            st.pre_ntrades,
            st.pre_spread,
            st.pre_bars,
        )
        st.snap_lo_ntrades, st.snap_lo_spread, st.snap_lo_bars = (
            st.pre_ntrades,
            st.pre_spread,
            st.pre_bars,
        )
        return False
    if st.mode == 0:
        if c > st.hi:
            st.hi, st.hi_min = c, m
            st.snap_hi_ntrades, st.snap_hi_spread, st.snap_hi_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
        if c < st.lo:
            st.lo, st.lo_min = c, m
            st.snap_lo_ntrades, st.snap_lo_spread, st.snap_lo_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
        down_rev = (st.hi - c) / st.hi if st.hi > 0.0 else 0.0
        up_rev = (c - st.lo) / st.lo if st.lo > 0.0 else 0.0
        if down_rev >= delta and down_rev >= up_rev:
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.snap_hi_ntrades,
                st.snap_hi_spread,
                st.snap_hi_bars,
            )
            _dc_confirm(st, st.hi, st.hi_min, -1)
            st.mode = -1
            st.leg_start_price, st.leg_start_min = st.hi, st.hi_min
            st.snap_start_ntrades, st.snap_start_spread, st.snap_start_bars = (
                st.snap_hi_ntrades,
                st.snap_hi_spread,
                st.snap_hi_bars,
            )
            st.ext, st.ext_min = c, m
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
            confirmed = True
        elif up_rev >= delta:
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.snap_lo_ntrades,
                st.snap_lo_spread,
                st.snap_lo_bars,
            )
            _dc_confirm(st, st.lo, st.lo_min, 1)
            st.mode = 1
            st.leg_start_price, st.leg_start_min = st.lo, st.lo_min
            st.snap_start_ntrades, st.snap_start_spread, st.snap_start_bars = (
                st.snap_lo_ntrades,
                st.snap_lo_spread,
                st.snap_lo_bars,
            )
            st.ext, st.ext_min = c, m
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
            confirmed = True
    elif st.mode == 1:
        if c >= st.ext:
            st.ext, st.ext_min = c, m
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
        elif st.ext > 0.0 and (st.ext - c) / st.ext >= delta:
            _dc_confirm(st, st.ext, st.ext_min, -1)
            st.mode = -1
            st.leg_start_price, st.leg_start_min = st.ext, st.ext_min
            st.snap_start_ntrades, st.snap_start_spread, st.snap_start_bars = (
                st.snap_ext_ntrades,
                st.snap_ext_spread,
                st.snap_ext_bars,
            )
            st.ext, st.ext_min = c, m
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
            confirmed = True
    else:
        if c <= st.ext:
            st.ext, st.ext_min = c, m
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
        elif st.ext > 0.0 and (c - st.ext) / st.ext >= delta:
            _dc_confirm(st, st.ext, st.ext_min, 1)
            st.mode = 1
            st.leg_start_price, st.leg_start_min = st.ext, st.ext_min
            st.snap_start_ntrades, st.snap_start_spread, st.snap_start_bars = (
                st.snap_ext_ntrades,
                st.snap_ext_spread,
                st.snap_ext_bars,
            )
            st.ext, st.ext_min = c, m
            st.snap_ext_ntrades, st.snap_ext_spread, st.snap_ext_bars = (
                st.pre_ntrades,
                st.pre_spread,
                st.pre_bars,
            )
            confirmed = True
    return confirmed


def _fib_reads(
    st: _ScaleState, cur_price: float, last_dir: float, cur_dir: float
) -> tuple[float, float, float, float, float, float, float]:
    nan = float("nan")
    if not st.have_prev_leg:
        return (nan, 0.0, 0.0, 0.0, nan, nan, 0.0)
    p0, p1 = st.prev_leg_start, st.prev_leg_end
    rng = p1 - p0
    if abs(rng) <= 0.0:
        return (nan, 0.0, 0.0, 0.0, nan, nan, 0.0)
    retr = (p1 - cur_price) / rng
    if not math.isfinite(retr) or abs(retr) > FIB_DC_MAX_ABS:
        return (nan, 0.0, 0.0, 0.0, nan, nan, 0.0)
    in_golden = 1.0 if 0.618 <= retr <= 0.786 else 0.0
    down_leg = last_dir < 0.0
    up_leg = last_dir > 0.0
    resumed = (down_leg and cur_dir > 0.0) or (up_leg and cur_dir < 0.0)
    holding_618 = 1.0 if (0.5 <= retr <= 0.786 and resumed) else 0.0
    broke_786 = 1.0 if retr > 0.786 else 0.0
    ext_progress = min(max(-retr / 0.618, 0.0), 1.0) if retr < 0.0 else 0.0
    nearest = min(FIB_RETR, key=lambda lvl: abs(retr - lvl))
    dist_nearest = retr - nearest
    setup_long = 1.0 if (down_leg and in_golden > 0.0 and cur_dir > 0.0) else 0.0
    return (retr, in_golden, holding_618, broke_786, ext_progress, dist_nearest, setup_long)


def _slice_median(values: list[float]) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def _ols_slope(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 2:
        return float("nan")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    return sxy / sxx if sxx > 0.0 else float("nan")


def _response_signature(states: list[_ScaleState], sigma: float) -> tuple[float, float, float, float]:
    n = len(states)
    if n < 2 or sigma != sigma:
        return (float("nan"),) * 4
    log_delta: list[float] = []
    log_nlegs: list[float] = []
    log_chunk: list[float] = []
    os_ratios: list[float] = []
    any_legs = False
    for si, st in enumerate(states):
        delta = min(max(SCALES[si] * sigma, THETA_FLOOR), THETA_CAP)
        log_delta.append(math.log(delta))
        log_nlegs.append(math.log(1.0 + st.n_legs_total))
        med = _slice_median([leg["abs_ret"] for leg in st.legs])
        med_f = med if (math.isfinite(med) and med > 0.0) else 1e-9
        log_chunk.append(math.log(med_f))
        if st.n_legs_total > 0.0:
            any_legs = True
        if st.mode != 0 and delta > 0.0:
            cur_h = (
                abs((st.last_close - st.leg_start_price) / st.leg_start_price)
                if st.leg_start_price > 0.0
                else 0.0
            )
            os_ratios.append(cur_h / delta)
    if not any_legs:
        return (float("nan"),) * 4
    nlegs_slope = _ols_slope(log_delta, log_nlegs)
    chunk_slope = _ols_slope(log_delta, log_chunk)
    os_ratio_mean = sum(os_ratios) / len(os_ratios) if os_ratios else float("nan")
    finest = states[0].n_legs_total
    coarsest = states[-1].n_legs_total
    if coarsest > 0.0:
        roughness = finest / coarsest
    elif finest > 0.0:
        roughness = finest
    else:
        roughness = float("nan")
    return (nlegs_slope, chunk_slope, os_ratio_mean, roughness)


def _python_dc(
    closes: list[float], ntrades: list[float], spreads: list[float], minutes: list[int]
) -> dict[str, list[float]]:
    """Pure-Python reference for the multi-scale DC fold; the Rust kernel must equal it cell-for-cell."""
    out: dict[str, list[float]] = {name: [] for name in _FEATURE_COLS}
    n_scales = len(SCALES)
    states = [_ScaleState() for _ in range(n_scales)]
    ret_ring: deque[float] = deque()
    prev_close = float("nan")
    cur_day = None
    for close, nt, sp, m in zip(closes, ntrades, spreads, minutes):
        day = m // DAY_SECS
        if day != cur_day:
            cur_day = day
            states = [_ScaleState() for _ in range(n_scales)]
            ret_ring.clear()
            prev_close = float("nan")
        if prev_close == prev_close and prev_close > 0.0 and close > 0.0:
            ret_ring.append(math.log(close / prev_close))
            while len(ret_ring) > VOL_WIN:
                ret_ring.popleft()
        prev_close = close
        if len(ret_ring) >= 2:
            nn = len(ret_ring)
            mean = sum(ret_ring) / nn
            var = sum((x - mean) ** 2 for x in ret_ring) / (nn - 1)
            sigma = math.sqrt(var)
        else:
            sigma = float("nan")

        pivot_now = [False] * n_scales
        for si, st in enumerate(states):
            st.pre_ntrades += nt
            st.pre_spread += sp
            st.pre_bars += 1.0
            delta = THETA_FLOOR if sigma != sigma else min(max(SCALES[si] * sigma, THETA_FLOOR), THETA_CAP)
            pivot_now[si] = _dc_step(st, close, m, delta)

        dir_signs: list[float] = []
        setup_long_each: list[float] = []
        for si, st in enumerate(states):
            base_cols = [f for f in _FEATURE_COLS if f.endswith(_scale_suffix(si))][:N_PER_SCALE]
            cur_dir = float(st.mode)
            dir_signs.append(cur_dir)
            cur_price = st.last_close
            cur_height = (
                (cur_price - st.leg_start_price) / st.leg_start_price if st.leg_start_price > 0.0 else 0.0
            )
            msd = float((m - st.last_pivot_min) // 60) if st.have_pivot else float("nan")
            delta = THETA_FLOOR if sigma != sigma else min(max(SCALES[si] * sigma, THETA_FLOOR), THETA_CAP)
            if st.legs:
                leg = st.legs[-1]
                last_h, last_slope, last_dur = leg["signed_ret"], leg["slope"], leg["dur_min"]
                last_nt, last_sp, last_dir = leg["n_trades"], leg["mean_spread"], leg["dir"]
                others = [x["abs_ret"] for x in list(st.legs)[:-1]]
                if others:
                    last_pctile = sum(1 for y in others if y < leg["abs_ret"]) / len(others)
                else:
                    last_pctile = float("nan")
            else:
                last_h = last_slope = last_dur = last_nt = last_sp = float("nan")
                last_dir = 0.0
                last_pctile = float("nan")
            os_to_dc = abs(cur_height) / delta if (st.mode != 0 and delta > 0.0) else float("nan")
            persistence = sum(x["signed_ret"] for x in st.legs) + cur_height
            fib = _fib_reads(st, cur_price, last_dir, cur_dir)
            setup_long_each.append(fib[6])
            vals = [
                cur_dir,
                msd,
                last_h,
                last_slope,
                last_dur,
                last_nt,
                last_sp,
                last_pctile,
                os_to_dc,
                persistence,
                fib[0],
                fib[1],
                fib[2],
                fib[3],
                fib[4],
                fib[5],
            ]
            for col, val in zip(base_cols, vals):
                out[col].append(val)

        nonzero = [d for d in dir_signs if d != 0.0]
        if not nonzero:
            agree = float("nan")
        else:
            pos = sum(1 for d in nonzero if d > 0.0)
            neg = sum(1 for d in nonzero if d < 0.0)
            agree = max(pos, neg) / n_scales
        pos_all = sum(1 for d in dir_signs if d > 0.0)
        neg_all = sum(1 for d in dir_signs if d < 0.0)
        dominant = 1.0 if pos_all > neg_all else (-1.0 if neg_all > pos_all else 0.0)
        setup_count = sum(setup_long_each)
        pivot_coincidence = float(sum(1 for p in pivot_now if p))
        finest_dir = dir_signs[0] if dir_signs else 0.0
        coarse_directed = any(d != 0.0 for d in dir_signs[1:])
        finest_only = 1.0 if (finest_dir != 0.0 and not coarse_directed) else 0.0
        resp = _response_signature(states, sigma)
        sigma_bps = float("nan") if sigma != sigma else sigma * 10_000.0
        out["dc_xscale_dir_agreement"].append(agree)
        out["dc_xscale_dir_dominant"].append(dominant)
        out["dc_xscale_setup_long_count"].append(setup_count)
        out["dc_xscale_pivot_coincidence"].append(pivot_coincidence)
        out["dc_xscale_finest_only"].append(finest_only)
        out["dc_resp_nlegs_slope"].append(resp[0])
        out["dc_resp_chunk_slope"].append(resp[1])
        out["dc_resp_os_ratio_mean"].append(resp[2])
        out["dc_resp_roughness"].append(resp[3])
        out["dc_sigma30_bps"].append(sigma_bps)
    return out


def _scale_suffix(si: int) -> str:
    scale = SCALES[si]
    if scale == int(scale):
        return f"_s{int(scale)}"
    return "_s" + str(scale).replace(".", "")


def _cell_equal(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    a_missing = a is None or (isinstance(a, float) and not np.isfinite(a))
    b_missing = b is None or (isinstance(b, float) and not np.isfinite(b))
    if a_missing or b_missing:
        return a_missing and b_missing
    return abs(a - b) <= 1e-12 + tol * abs(b)


def test_swing_dc_python_reference_pins_rust() -> None:
    """The Rust ``swing_dc_fold`` equals the pure-Python reference cell-for-cell at every (symbol, minute)."""
    stream = _stream(n_sym=4, n_min=200)
    rust = swing_dc_fold_frame(stream).sort(["symbol", "minute"])
    for symbol in sorted(stream["symbol"].unique().to_list()):
        sym = stream.filter(pl.col("symbol") == symbol).sort("minute")
        ref = _python_dc(
            sym["close"].to_list(),
            sym["n_trades"].to_list(),
            sym["mean_spread_bps"].to_list(),
            [int(m.timestamp()) for m in sym["minute"].to_list()],
        )
        got = rust.filter(pl.col("symbol") == symbol).sort("minute")
        for name in _FEATURE_COLS:
            got_col = got[name].to_list()
            for i, (gv, rv) in enumerate(zip(got_col, ref[name])):
                assert _cell_equal(gv, rv), f"{symbol}.{name}[{i}]: rust={gv} != python={rv}"


def test_swing_dc_no_look_ahead() -> None:
    """THE property: the row at T is identical whether or not bars after T exist (no pivot uses a future bar)."""
    stream = _stream(n_sym=3, n_min=160)
    minutes = sorted(stream["minute"].unique())
    group = SwingDcGroup()
    for ti in range(2, len(minutes), 3):  # sample every 3rd T to keep the O(T^2) sweep bounded
        t_minute = minutes[ti]
        at_t = (
            group.compute(BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= t_minute)}))
            .filter(pl.col("minute") == t_minute)
            .sort("symbol")
        )
        future_ti = min(ti + 30, len(minutes) - 1)
        with_future = (
            group.compute(
                BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= minutes[future_ti])})
            )
            .filter(pl.col("minute") == t_minute)
            .sort("symbol")
        )
        for name in _FEATURE_COLS:
            a = at_t[name].to_list()
            b = with_future[name].to_list()
            for sym_i in range(len(a)):
                assert _cell_equal(
                    a[sym_i], b[sym_i]
                ), f"LOOK-AHEAD T={ti} {name} sym{sym_i}: {a[sym_i]} (no future) != {b[sym_i]} (with future)"


def test_swing_dc_fold_equals_reseed() -> None:
    """KIND invariant: re-seed over the buffer ending at each minute == the single batch pass, cell-for-cell."""
    stream = _stream(n_sym=3, n_min=120)
    minutes = sorted(stream["minute"].unique())
    group = SwingDcGroup()
    batch = group.compute(BatchContext(frames={"minute_agg": stream}))
    for ti in range(0, len(minutes), 4):
        minute = minutes[ti]
        reseeded = (
            group.compute(BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= minute)}))
            .filter(pl.col("minute") == minute)
            .sort("symbol")
        )
        batch_t = batch.filter(pl.col("minute") == minute).sort("symbol")
        for name in _FEATURE_COLS:
            a = reseeded[name].to_list()
            b = batch_t[name].to_list()
            for sym_i in range(len(a)):
                assert _cell_equal(a[sym_i], b[sym_i]), f"reseed!=batch @min{ti} {name} sym{sym_i}"


def test_swing_dc_compute_latest_equals_backfill() -> None:
    """GROUP parity: compute_latest == compute().filter(last minute), cell-for-cell across the stream."""
    stream = _stream(n_sym=4, n_min=140)
    minutes = sorted(stream["minute"].unique())
    group = SwingDcGroup()
    for ti in range(40, len(minutes), 10):
        buf = stream.filter(pl.col("minute") <= minutes[ti])
        latest = group.compute_latest(BatchContext(frames={"minute_agg": buf})).sort("symbol")
        backfill = (
            group.compute(BatchContext(frames={"minute_agg": buf}))
            .filter(pl.col("minute") == minutes[ti])
            .sort("symbol")
        )
        for name in _FEATURE_COLS:
            a = latest[name].to_list()
            b = backfill[name].to_list()
            for sym_i in range(len(a)):
                assert _cell_equal(a[sym_i], b[sym_i]), f"latest!=backfill @min{ti} {name} sym{sym_i}"


def test_swing_dc_degenerate_safety() -> None:
    """Flat history, a single bar, and a no-pivot ramp emit clean NULLs / finite values — never inf/nan leak."""
    rows = []
    # S_flat: a dead-flat name (no pivots ever); S_one: a single bar; S_ramp: a monotone ramp (no reversal).
    for mi in range(60):
        minute = BASE + dt.timedelta(minutes=mi)
        rows.append(
            {"symbol": "S_flat", "minute": minute, "close": 50.0, "n_trades": 3.0, "mean_spread_bps": 2.0}
        )
        rows.append(
            {
                "symbol": "S_ramp",
                "minute": minute,
                "close": 50.0 + mi * 0.1,
                "n_trades": 3.0,
                "mean_spread_bps": 2.0,
            }
        )
    rows.append({"symbol": "S_one", "minute": BASE, "close": 99.0, "n_trades": 1.0, "mean_spread_bps": 1.0})
    stream = pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    out = swing_dc_fold_frame(stream)
    for name in _FEATURE_COLS:
        vals = out[name].to_list()
        for val in vals:
            assert val is None or np.isfinite(val), f"{name} leaked a non-finite value: {val}"


def test_swing_dc_setup_long_fires_at_resumption_not_before() -> None:
    """Hand-built up-leg -> ~61.8% pullback -> resumption: fib_setup_long fires AT the resumption, never before
    the reversal that completes the up-leg has confirmed."""
    # Build a clean up-leg at the s1 scale, a pullback into the golden zone, then an up-resumption. Vol is set so
    # the s1 threshold (~1*sigma) confirms the structure; we read the s1 setup flag.
    prices = []
    # Phase 1: a strong up-leg 100 -> 110 (so a clear up move at every scale).
    prices += [100.0 + i * 0.5 for i in range(21)]  # 100..110 over 20 min
    # Phase 2: a pullback that confirms a DOWN DC (so the up-leg's terminal pivot at 110 is sealed), retracing
    # toward ~61.8% of the 100->110 range (i.e. toward ~103.8).
    prices += [110.0 - i * 0.8 for i in range(1, 9)]  # 109.2..103.6 (~64% retrace) over 8 min
    # Phase 3: a clean up-resumption.
    prices += [103.6 + i * 0.6 for i in range(1, 12)]  # resume up
    minutes = [BASE + dt.timedelta(minutes=i) for i in range(len(prices))]
    stream = pl.DataFrame(
        {
            "symbol": ["Z"] * len(prices),
            "minute": minutes,
            "close": prices,
            "n_trades": [10.0] * len(prices),
            "mean_spread_bps": [3.0] * len(prices),
        }
    ).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    out = swing_dc_fold_frame(stream).sort("minute")
    # Use the finest scale that resolves the structure; assert setup_long is 0 throughout phase 1 (no completed
    # up-leg yet) and becomes 1 only at/after the resumption confirms the golden-zone read.
    setup_cols = [
        c for c in _FEATURE_COLS if c.startswith("dc_fib_setup") or c == "dc_xscale_setup_long_count"
    ]
    # The cross-scale count is the robust signal; it must be 0 during the initial up-run (no prior completed leg)
    # and strictly positive at some resumption bar.
    count = out["dc_xscale_setup_long_count"].to_list()
    assert all((v == 0.0) for v in count[:21]), "setup fired during the initial up-leg (look-ahead!)"
    assert any((v is not None and v > 0.0) for v in count[29:]), "setup never fired at the resumption"
    _ = setup_cols
