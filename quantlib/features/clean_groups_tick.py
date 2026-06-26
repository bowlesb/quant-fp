"""Tick-tape (trade-tape) feature groups ported to the ``CleanEngine`` interface (family: MICROSTRUCTURE).

These 8 legacy groups read the per-trade ``trades`` tape, NOT minute bars — the clean ring carries only minute
bars. They port via DERIVED BAR COLUMNS: the per-minute trade-derived primitive is computed from the raw tape in
the enrich step (``tick_capture.aggregate_symbol_minute`` — runs live AND backfill, the same shared boundary that
already produces ``signed_volume``/``mean_spread_bps``), carried as a bar column, and the clean group reads it.

The decomposition is PER-GROUP, by what the legacy rolling op is OVER (verified against each compute()):

  WINDOWED (carry a per-minute primitive, reduce over a trailing time-window):
    * print_hhi         — carry ``_hhi`` (within-minute notional Herfindahl scalar); trailing-MEAN (NULL-skip).
    * subminute_gap_fano— carry ``_gap_fano`` (within-minute inter-trade-gap Fano scalar); trailing-MEAN.
    * size_entropy      — carry the 6 LINEAR per-minute size-bin counts ``_sz_c0.._sz_c5``; trailing-SUM each,
                          then the Shannon-entropy assemble over the window-summed counts (the moment-sum of a
                          non-linear scalar would DIVERGE — the counts are the faithful linear carry).

  ATOMIC (1-minute features, NO window — the enrich step computes the final per-minute value; the clean group
  reads it straight off the latest bar):
    * inter_arrival          (rapid_fire_ratio_1m, p10_inter_arrival_ms_1m, trade_timing_entropy_1m)
    * large_print_burst      (large_print_ratio_1m, large_print_volume_share_1m, max_print_size_ratio_1m)
    * microstructure_burst   (peak_trades_per_second_1m, active_seconds_1m, inter_arrival_cv_1m, max_runup_1m)
    * tick_runlength         (max_signed_run_1m, signed_run_count_1m, tick_signed_volume_1m) — Rust state-machine,
                              not linearly reducible, so the per-minute triple is carried opaquely.
    * trade_size_dist        (odd_lot_ratio_1m, round_lot_ratio_1m, institutional_trade_ratio_1m)

Each derived bar column is NaN on a tradeless minute (the honest "no trades", per the legacy nan_policy) and the
clean group propagates that NaN. The enrich step (worker-populated, like ``daily_open``) is the input contract;
the per-minute primitive names below are what it must emit.
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window

_HHI_WINDOWS: tuple[int, ...] = (30, 60)
_SIZE_ENTROPY_WINDOWS: tuple[int, ...] = (30, 60)
_GAP_FANO_WINDOW = 60
_N_SIZE_BINS = 6


def _trailing_nanmean(window: Window, col: str, minutes: int) -> np.ndarray:
    """Trailing-``minutes`` MEAN of a per-minute scalar column, skipping NaN minutes (legacy
    ``rolling_mean_by("minute")`` excludes the per-minute nulls). NaN where no finite minute is in the window.
    """
    mat = window.trailing_time(col, minutes)  # (n_sym, w), out-of-window + tradeless cells NaN
    mask = np.isfinite(mat)
    count = mask.sum(axis=1).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        total = np.where(mask, mat, 0.0).sum(axis=1)
        return np.where(count > 0.0, total / count, np.nan)


class PrintHHIClean:
    """WINDOWED: trailing-w-minute mean of the within-minute notional Herfindahl. print_hhi_{30,60}m. Reads the
    enrich-derived per-minute scalar ``_hhi``. Legacy: ``PrintHHIGroup``."""

    name = "print_hhi"
    input_cols = ("_hhi",)
    feature_names = tuple(f"print_hhi_{w}m" for w in _HHI_WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        return {f"print_hhi_{w}m": _trailing_nanmean(window, "_hhi", w) for w in _HHI_WINDOWS}


class SubminuteGapFanoClean:
    """WINDOWED: trailing-60m mean of the within-minute inter-trade-gap Fano factor (var/mean of microsecond
    gaps). subminute_gap_fano_60m. Reads the enrich-derived per-minute scalar ``_gap_fano``. Legacy:
    ``SubminuteGapFanoGroup``."""

    name = "subminute_gap_fano"
    input_cols = ("_gap_fano",)
    feature_names = (f"subminute_gap_fano_{_GAP_FANO_WINDOW}m",)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        return {
            f"subminute_gap_fano_{_GAP_FANO_WINDOW}m": _trailing_nanmean(
                window, "_gap_fano", _GAP_FANO_WINDOW
            )
        }


class SizeEntropyClean:
    """WINDOWED: Shannon entropy of the trade-size order-of-magnitude distribution over the trailing window.
    size_entropy_{30,60}m = entropy of the WINDOW-SUMMED 6 size-bin counts (``ln N − Σ c_b ln c_b / N``). Reads
    the 6 enrich-derived per-minute LINEAR bin counts ``_sz_c0.._sz_c5`` (trailing-SUM each, then assemble — the
    counts are linear so the windowed sum is faithful, unlike a per-minute entropy mean). Legacy:
    ``SizeEntropyGroup``."""

    name = "size_entropy"
    input_cols = tuple(f"_sz_c{b}" for b in range(_N_SIZE_BINS))
    feature_names = tuple(f"size_entropy_{w}m" for w in _SIZE_ENTROPY_WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for w in _SIZE_ENTROPY_WINDOWS:
            # window-sum each bin count over the trailing w minutes (NaN minutes contribute 0).
            summed = np.zeros((window.n, _N_SIZE_BINS), dtype=np.float64)
            for b in range(_N_SIZE_BINS):
                mat = window.trailing_time(f"_sz_c{b}", w)
                summed[:, b] = np.where(np.isfinite(mat), mat, 0.0).sum(axis=1)
            total = summed.sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                # Shannon entropy = ln(N) − Σ c_b ln(c_b) / N, the c_b ln c_b term 0 where c_b == 0.
                clogc = np.where(summed > 0.0, summed * np.log(summed), 0.0).sum(axis=1)
                entropy = np.log(total) - clogc / total
            out[f"size_entropy_{w}m"] = np.where(total > 0.0, entropy, np.nan)
        return out


class _AtomicTickGroup:
    """ATOMIC base: a 1-minute trade-tape group with NO window — the enrich step computes the final per-minute
    feature values, carried as derived bar columns; the clean group reads the latest bar straight through. Each
    feature name IS its derived column name (the enrich step emits exactly the declared features)."""

    name: str = ""
    feature_names: tuple[str, ...] = ()

    @property
    def input_cols(self) -> tuple[str, ...]:
        return self.feature_names

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        return {feature: window.latest(feature) for feature in self.feature_names}


class InterArrivalClean(_AtomicTickGroup):
    """ATOMIC: within-minute inter-trade-arrival features. rapid_fire_ratio_1m (frac gaps <100ms),
    p10_inter_arrival_ms_1m, trade_timing_entropy_1m (entropy of trade count over the 60 one-second buckets).
    Legacy: ``InterArrivalGroup``."""

    name = "inter_arrival"
    feature_names = ("rapid_fire_ratio_1m", "p10_inter_arrival_ms_1m", "trade_timing_entropy_1m")


class LargePrintBurstClean(_AtomicTickGroup):
    """ATOMIC: within-minute large-print features (size ≥ 4× the minute's mean). large_print_ratio_1m,
    large_print_volume_share_1m, max_print_size_ratio_1m. Legacy: ``LargePrintBurstGroup``."""

    name = "large_print_burst"
    feature_names = ("large_print_ratio_1m", "large_print_volume_share_1m", "max_print_size_ratio_1m")


class MicrostructureBurstClean(_AtomicTickGroup):
    """ATOMIC: within-minute microstructure-burst features. peak_trades_per_second_1m, active_seconds_1m,
    inter_arrival_cv_1m, max_runup_1m. Legacy: ``MicrostructureBurstGroup``."""

    name = "microstructure_burst"
    feature_names = (
        "peak_trades_per_second_1m",
        "active_seconds_1m",
        "inter_arrival_cv_1m",
        "max_runup_1m",
    )


class TickRunlengthClean(_AtomicTickGroup):
    """ATOMIC: within-minute tick-rule signed-run features (Rust state-machine, not linearly reducible → carried
    opaque). max_signed_run_1m, signed_run_count_1m, tick_signed_volume_1m. Legacy: ``TickRunlengthGroup``.
    """

    name = "tick_runlength"
    feature_names = ("max_signed_run_1m", "signed_run_count_1m", "tick_signed_volume_1m")


class TradeSizeDistClean(_AtomicTickGroup):
    """ATOMIC: within-minute trade-size distribution. odd_lot_ratio_1m (size<100), round_lot_ratio_1m (size%100==0),
    institutional_trade_ratio_1m (size≥10000). Legacy: ``TradeSizeDistGroup``."""

    name = "trade_size_dist"
    feature_names = ("odd_lot_ratio_1m", "round_lot_ratio_1m", "institutional_trade_ratio_1m")
