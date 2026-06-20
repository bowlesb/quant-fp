"""The `Strategy` protocol + the cross-sectional L/S archetype (archetype 1, §1.3).

Every archetype implements `label / signal / backtest` over a SHARED pre-loaded `Panel` and the
SHARED `walk_forward_folds` core — so the leakage core is written exactly once and a new archetype
is ~50 lines. Phase 0 ships only `CrossSectionalLS`; the panel layout + the protocol are the seam
the Phase-1 Rust first-touch / streak archetypes slot into.

`CrossSectionalLS` covers ~5 of Ben's 7 named strategies as one mechanism x (horizon, conditioner,
sizing) parameters: EOD / multi-day / sector-limited / up-down-day / liquidity-cut are all THIS
archetype with a different tuple.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Protocol

import lightgbm as lgb
import numpy as np

from quantlib.backtest import (
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
    walk_forward_folds,
)
from quantlib.battery.cost import cost_curve, long_short_per_name_cost
from quantlib.battery.panel import Panel
from quantlib.battery.result import (
    BacktestResult,
    NullStat,
    SanityReport,
    StratumStat,
    decide_verdict,
)
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon
from quantlib.research import DEFAULT_LGB

WINSOR_Q = 0.005  # per-day symmetric winsorization on the raw return (daily horizons)
MIN_CROSS_SECTION = 20  # per-timestamp breadth floor for the cross-sectional median
N_FOLDS = 5
NUM_ROUNDS = 200
TRADEABLE_TERCILE_FRAC = 1.0 / 3.0


class Strategy(Protocol):
    spec: ArchetypeSpec

    def label(self, panel: Panel) -> tuple[np.ndarray, np.ndarray]:
        """Forward/path label POINT-IN-TIME. Returns (label, keep_mask) over panel rows."""
        ...

    def backtest(self, panel: Panel) -> BacktestResult:
        """Net-of-cost evidence bundle for this archetype over the shared panel + walk-forward."""
        ...


def _per_day_winsor_excess(raw: np.ndarray, day_code: np.ndarray) -> np.ndarray:
    """Per-day symmetric winsorization (trap #3) then cross-sectional median demean (the excess
    label). Operates on the raw forward return grouped by integer day code. Days below the breadth
    floor are nulled (NaN)."""
    out = np.full_like(raw, np.nan)
    for day in np.unique(day_code):
        idx = np.where(day_code == day)[0]
        vals = raw[idx]
        finite = vals[np.isfinite(vals)]
        if finite.size < MIN_CROSS_SECTION:
            continue
        lo, hi = np.quantile(finite, [WINSOR_Q, 1.0 - WINSOR_Q])
        clipped = np.clip(vals, lo, hi)
        median = np.median(clipped[np.isfinite(clipped)])
        out[idx] = clipped - median
    return out


class CrossSectionalLS:
    """Cross-sectional long/short directional-excess archetype.

    label: forward cross-sectional EXCESS return at the spec horizon (intraday `fwd_<h>m` already
    in the panel; daily horizons — EOD/overnight/2d/3d — derived here from the daily execution
    prices, per-day winsorized + demeaned).
    signal: GBM (or raw-mean fast path) through the shared walk-forward, OOS per-timestamp rank-IC.
    backtest: per-name-half-spread L/S P&L + the full BacktestResult bundle.
    """

    def __init__(self, spec: ArchetypeSpec, *, seed: int = 13, use_gbm: bool = True) -> None:
        self.spec = spec
        self.seed = seed
        self.use_gbm = use_gbm

    def label(self, panel: Panel) -> tuple[np.ndarray, np.ndarray]:
        horizon = self.spec.horizon
        if horizon in (Horizon.M30, Horizon.M60):
            col = f"fwd_{self.spec.horizon_minutes}m"
            if col not in panel.extra:
                raise KeyError(f"intraday panel missing label column {col}")
            label = panel.extra[col]
            return label, np.isfinite(label)
        return self._daily_label(panel)

    def _daily_label(self, panel: Panel) -> tuple[np.ndarray, np.ndarray]:
        """EOD / overnight / 2d / 3d label off the daily panel's execution prices, point-in-time:
          - eod:        rth_close_d / exec_0935_d - 1   (TRADEABLE entry at 09:35, exit at the close)
          - overnight:  exec_0935_{d+1} / rth_close_d - 1
          - 2d/3d:      rth_close_{d+k} / rth_close_d - 1
        then per-day winsorize + cross-sectional demean (the excess label).

        EOD enters at the FILLABLE 09:35 print (exec_0935), NOT the open, so the close is genuinely
        future. The same-day open->close feature `ret_1d` embeds today's close (known only at the
        resolve time), so it is EXCLUDED from X for the EOD horizon (see `_eod_feature_mask`)."""
        exec_0935 = panel.extra["exec_0935"]
        rth_close = panel.extra["rth_close"]
        raw = np.full(panel.n_rows, np.nan)
        # forward exit prices are precomputed on the FULL daily grid (gap-safe; see panel.py) — a day
        # dropped by the liquidity/warmup filter cannot corrupt them, unlike a post-filter row-shift.
        if self.spec.horizon == Horizon.EOD:
            raw = _ratio_with_floor(rth_close, exec_0935)  # enter 09:35, exit close (same day)
        elif self.spec.horizon == Horizon.OVERNIGHT:
            raw = _ratio_with_floor(panel.extra["exit_overnight"], rth_close)
        elif self.spec.horizon == Horizon.D2:
            raw = _ratio_with_floor(panel.extra["exit_2d"], rth_close)
        elif self.spec.horizon == Horizon.D3:
            raw = _ratio_with_floor(panel.extra["exit_3d"], rth_close)
        else:
            raise ValueError(f"unsupported daily horizon {self.spec.horizon}")
        day_code = panel.minute_epoch  # one timestamp per trading day in the daily panel
        # map each distinct minute_epoch to a small int for grouping
        _, day_idx = np.unique(day_code, return_inverse=True)
        label = _per_day_winsor_excess(raw, day_idx)
        return label, np.isfinite(label)

    def _conditioner_mask(self, panel: Panel) -> np.ndarray:
        """The point-in-time as-of-t selection predicate for this cell's conditioner."""
        keep = np.ones(panel.n_rows, dtype=bool)
        cond = self.spec.conditioner
        if cond == Conditioner.NONE:
            return keep
        if cond == Conditioner.LIQUIDITY_TERCILE:
            # keep only the most-liquid tercile per timestamp (the genuinely-tradeable cut, trap #1)
            liq = panel.extra.get("rth_dollar_vol")
            if liq is None:
                liq = panel.volume * panel.entry_close
            return _top_tercile_per_group(liq, panel.minute_epoch)
        if cond == Conditioner.UP_DOWN_MARKET:
            # default: take only UP-market days (a regime split; both directions reported in by_regime)
            up = panel.extra.get("up_market_day")
            if up is not None:
                return up.astype(bool)
            return keep
        if cond == Conditioner.SECTOR:
            # sector restriction needs a populated sector_map; absent -> no restriction (reported)
            return keep
        return keep

    def backtest(self, panel: Panel) -> BacktestResult:
        label, label_mask = self.label(panel)
        cond_mask = self._conditioner_mask(panel)
        feature_matrix = self._features_as_of_entry(panel)
        # keep rows with a finite label, passing the conditioner, with at least one usable feature
        # (LightGBM tolerates NaN features natively, so we don't require all-finite).
        feature_usable = np.any(np.isfinite(feature_matrix), axis=1)
        keep = label_mask & cond_mask & feature_usable
        idx = np.where(keep)[0]
        sub = _Subset(feature_matrix, label, panel.minute_epoch, idx)
        return self._run(sub, panel)

    def _features_as_of_entry(self, panel: Panel) -> np.ndarray:
        """The feature matrix POINT-IN-TIME at the bet's ENTRY.

        The daily-reduced features are computed AS-OF THE DAY's close (they use today's `rth_close`).
        That is legitimate for horizons that ENTER at the close (overnight / 2d / 3d). But the EOD
        bet enters at 09:35 and resolves at the SAME day's close, so ANY feature using today's close
        is a look-ahead. For EOD we therefore use the PRIOR day's feature row (a one-day per-symbol
        shift) — only information through yesterday's close is visible at the 09:35 entry."""
        if not (self.spec.horizon == Horizon.EOD and panel.cadence == "daily"):
            return panel.feature_matrix
        shifted = np.full_like(panel.feature_matrix, np.nan)
        symbol_code = panel.symbol_code
        n = panel.n_rows
        if n > 1:
            same_symbol = symbol_code[1:] == symbol_code[:-1]
            shifted[1:][same_symbol] = panel.feature_matrix[:-1][same_symbol]
        return shifted

    def _run(self, sub: "_Subset", panel: Panel) -> BacktestResult:
        spec = self.spec
        ts = sub.ts
        folds = walk_forward_folds(ts, spec.horizon_minutes, N_FOLDS) if len(set(ts)) > N_FOLDS else []
        preds, labels, groups, rows = self._walk_forward(sub, folds)
        real_ic = per_timestamp_ic(preds, labels, groups, min_names=MIN_CROSS_SECTION)
        mean_real = mean_ic(real_ic)
        shuffled = shuffle_within_groups(labels, groups, self.seed)
        shuffle_ic = per_timestamp_ic(preds, shuffled, groups, min_names=MIN_CROSS_SECTION)
        mean_shuf = mean_ic(shuffle_ic)
        lag = max(1, spec.horizon_minutes // spec.cadence_min)
        nw_t = newey_west_tstat(real_ic, lag)
        periods_per_year = 252.0 * (390.0 / spec.cadence_min)
        symbols = [panel.symbol_names[panel.symbol_code[i]] for i in rows]
        spreads = [float(panel.half_spread_bps[i]) for i in rows]
        bt = long_short_per_name_cost(
            preds,
            labels,
            groups,
            symbols,
            spreads,
            frac=spec.frac,
            cost_mult=1.0,
            periods_per_year=periods_per_year,
        )
        curve = cost_curve(
            preds,
            labels,
            groups,
            symbols,
            spreads,
            frac=spec.frac,
            periods_per_year=periods_per_year,
        )
        by_stratum = self._stratify(preds, labels, groups, rows, panel, lag, periods_per_year)
        by_regime = self._regime_split(preds, labels, groups, rows, panel, lag, periods_per_year)
        sanity = self._sanity(sub, panel)
        result = BacktestResult(
            spec=spec,
            net_per_period=_num(bt.get("net_per_period")),
            gross_per_period=_num(bt.get("gross_per_period")),
            sharpe_net=_num(bt.get("sharpe_net")),
            hit_rate=_num(bt.get("hit_rate")),
            mean_turnover=_num(bt.get("mean_turnover")),
            breakeven_cost_bps=_num(bt.get("breakeven_cost_bps")),
            shuffle_canary=NullStat(ic=mean_shuf, n=len(shuffle_ic)),
            predict_zero=NullStat(ic=0.0, n=len(real_ic)),
            edge_vs_shuffle=(mean_real - mean_shuf) if not math.isnan(mean_real) else float("nan"),
            mean_ic=mean_real,
            nw_t=nw_t,
            n_test_ts=len(real_ic),
            n_rows=len(labels),
            directional=True,  # cross-sectional excess IS directional
            up_vs_down_asymmetry=None,
            sanity=sanity,
            by_stratum=by_stratum,
            by_regime=by_regime,
            cost_curve=curve,
            cost_used_bps=float(np.nanmedian(spreads)) if spreads else float("nan"),
        )
        verdict, reason = decide_verdict(result)
        result.verdict = verdict
        result.verdict_reason = reason
        return result

    def _walk_forward(self, sub: "_Subset", folds: list) -> tuple[list[float], list[float], list, list[int]]:
        preds: list[float] = []
        labels: list[float] = []
        groups: list = []
        rows: list[int] = []
        for fold in folds:
            if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
                continue
            tr, te = fold.train_idx, fold.test_idx
            train_X = sub.X[tr]
            train_y = sub.y[tr]
            test_X = sub.X[te]
            if self.use_gbm:
                booster = lgb.train(
                    DEFAULT_LGB,
                    lgb.Dataset(train_X, label=train_y),
                    num_boost_round=NUM_ROUNDS,
                )
                pred = booster.predict(test_X)
            else:
                # raw fast path: a single-feature signal = the feature value itself (no model)
                pred = test_X[:, 0]
            for j, i in enumerate(te):
                preds.append(float(pred[j]))
                labels.append(float(sub.y[i]))
                groups.append(sub.ts[i])
                rows.append(int(sub.row_idx[i]))
        return preds, labels, groups, rows

    def _stratify(
        self, preds, labels, groups, rows, panel: Panel, lag: int, ppy: float
    ) -> dict[str, StratumStat]:
        """by-liquidity-tercile breakdown (trap #1) so "edge ONLY in the illiquid tail" is visible."""
        liq = panel.extra.get("rth_dollar_vol")
        if liq is None:
            liq = panel.volume * panel.entry_close
        row_liq = np.array([liq[r] for r in rows])
        out: dict[str, StratumStat] = {}
        terciles = _assign_terciles(row_liq, groups)
        names = {0: "liq_low", 1: "liq_mid", 2: "liq_high"}
        for level, label_name in names.items():
            positions = [p for p, lv in enumerate(terciles) if lv == level]
            if len(positions) < 50:
                continue
            out[label_name] = self._stratum_stat(
                label_name, preds, labels, groups, rows, positions, panel, lag, ppy
            )
        return out

    def _regime_split(
        self, preds, labels, groups, rows, panel: Panel, lag: int, ppy: float
    ) -> dict[str, StratumStat]:
        up = panel.extra.get("up_market_day")
        if up is None:
            return {}
        out: dict[str, StratumStat] = {}
        for label_name, want in (("up_market", True), ("down_market", False)):
            positions = [p for p, r in enumerate(rows) if bool(up[r]) == want]
            if len(positions) < 50:
                continue
            out[label_name] = self._stratum_stat(
                label_name, preds, labels, groups, rows, positions, panel, lag, ppy
            )
        return out

    def _stratum_stat(
        self, name, preds, labels, groups, rows, positions, panel: Panel, lag: int, ppy: float
    ) -> StratumStat:
        sp = [preds[p] for p in positions]
        sl = [labels[p] for p in positions]
        sg = [groups[p] for p in positions]
        ssym = [panel.symbol_names[panel.symbol_code[rows[p]]] for p in positions]
        sspread = [float(panel.half_spread_bps[rows[p]]) for p in positions]
        real = per_timestamp_ic(sp, sl, sg, min_names=5)
        shuf = per_timestamp_ic(sp, shuffle_within_groups(sl, sg, self.seed), sg, min_names=5)
        bt = long_short_per_name_cost(sp, sl, sg, ssym, sspread, frac=self.spec.frac, periods_per_year=ppy)
        return StratumStat(
            name=name,
            real_ic=mean_ic(real),
            shuffle_ic=mean_ic(shuf),
            nw_t=_num(newey_west_tstat(real, lag)),
            net_per_period=_num(bt.get("net_per_period")),
            breakeven_cost_bps=_num(bt.get("breakeven_cost_bps")),
            n_names=len({s for s in ssym}),
        )

    def _sanity(self, sub: "_Subset", panel: Panel) -> SanityReport:
        label_std = float(np.nanstd(sub.y)) if sub.y.size else float("nan")
        # band: intraday excess ~0.01-0.03; daily overnight ~0.02-0.05; flag the 0.7+ blow-up.
        band_hi = 0.10 if self.spec.is_intraday else 0.20
        label_std_ok = bool(label_std == label_std and 0.0 < label_std < band_hi)
        earliest_minute_ok = _earliest_minute_ok(panel)
        return SanityReport(
            price_floor_applied=True,  # the Panel build enforces the $1 floor on both legs
            winsorized=not self.spec.is_intraday,  # daily horizons winsorize; intraday uses median-excess
            label_std=label_std,
            label_std_ok=label_std_ok,
            entry_minute_ok=earliest_minute_ok,
            tradeable_fraction=1.0,  # the Panel already applied the liquidity floor
        )


def _earliest_minute_ok(panel: Panel) -> bool:
    """Earliest entry minute >= 09:35 ET (13:35 UTC) — never the 09:30 print."""
    if panel.minute_epoch.size == 0:
        return True
    earliest = min(dt.datetime.fromtimestamp(int(ns) / 1e9, tz=dt.timezone.utc) for ns in panel.minute_epoch)
    minute_of_day = earliest.hour * 60 + earliest.minute
    return minute_of_day >= (13 * 60 + 35)


def _ratio_with_floor(exit_price: np.ndarray, entry_price: np.ndarray, floor: float = 1.0) -> np.ndarray:
    """exit/entry - 1, nulled unless BOTH legs >= the $1 price-integrity floor (penny-print guard)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = exit_price / entry_price - 1.0
    bad = ~(np.isfinite(ratio) & (exit_price >= floor) & (entry_price >= floor))
    ratio[bad] = np.nan
    return ratio


def _assign_terciles(values: np.ndarray, groups: list) -> list[int]:
    """Per-timestamp tercile (0/1/2) on `values`, point-in-time (no full-sample quantile)."""
    by_group: dict[object, list[int]] = defaultdict(list)
    for pos, grp in enumerate(groups):
        by_group[grp].append(pos)
    out = [-1] * len(groups)
    for indices in by_group.values():
        finite = [pos for pos in indices if np.isfinite(values[pos])]
        if len(finite) < 3:
            continue
        ordered = sorted(finite, key=lambda pos: values[pos])
        n = len(ordered)
        for rank, pos in enumerate(ordered):
            frac = rank / n
            out[pos] = (
                0 if frac < TRADEABLE_TERCILE_FRAC else (1 if frac < 2 * TRADEABLE_TERCILE_FRAC else 2)
            )
    return out


def _top_tercile_per_group(values: np.ndarray, minute_epoch: np.ndarray) -> np.ndarray:
    """Boolean mask keeping the TOP tercile of `values` within each timestamp (the liquid cut)."""
    groups = list(minute_epoch)
    terciles = _assign_terciles(values, groups)
    return np.array([lv == 2 for lv in terciles], dtype=bool)


def _num(value: object) -> float:
    if value is None:
        return float("nan")
    return float(value)  # type: ignore[arg-type]


class _Subset:
    """A keep-mask view onto the panel arrays: contiguous re-indexed (X, y, ts) for the harness,
    plus `row_idx` mapping back to the parent panel row (for symbol/spread/stratum lookups)."""

    def __init__(
        self, feature_matrix: np.ndarray, label: np.ndarray, minute_epoch: np.ndarray, idx: np.ndarray
    ) -> None:
        self.row_idx = idx
        self.X = feature_matrix[idx]
        self.y = label[idx]
        self.ts = [_epoch_to_dt(minute_epoch[i]) for i in idx]


def _epoch_to_dt(ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(ns) / 1e9, tz=dt.timezone.utc)
