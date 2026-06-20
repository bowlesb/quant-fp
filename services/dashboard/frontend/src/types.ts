// Shapes returned by the dashboard's /api/store-grid/* endpoints (see services/dashboard/store_grid.py).

export interface GridLegend {
  coverage_scale: string;
  trust_overlay: string;
  depth_note: string;
}

export interface GridSummary {
  n_dates: number;
  n_groups: number;
  n_trusted_groups: number;
  mean_coverage_pct: number;
}

// The matrix endpoint serves `coverage` as nested byte rows: rows aligned to `dates`, columns to `groups`.
// coverage[i][j] is a 0..255 darkness byte = the fraction of date i's captured tickers that have group j.
// group_trusted[j] is the binary trust bit for column j (1 = all the group's features trusted). universe[i]
// is date i's captured-universe size (the coverage denominator). group_coverage_pct[j] is the group's mean
// coverage over the dates it appears.
export interface StoreGridMatrix {
  generated_at: string;
  store_root: string;
  anchor_date: string | null;
  lookback_days: number;
  n_groups: number;
  n_trusted_groups: number;
  dates: string[];
  groups: string[];
  group_trusted: number[];
  coverage: number[][];
  universe: number[];
  group_coverage_pct: number[];
  summary: GridSummary;
  legend: GridLegend;
}

export interface GridMeta {
  generated_at: string;
  anchor_date: string | null;
  lookback_days: number;
  n_dates: number;
  n_groups: number;
  n_trusted_groups: number;
  mean_coverage_pct: number;
  raw_bytes: number;
  gzip_bytes: number;
  drills_written: number;
  build_seconds: number;
}

// One (date x group) cell's per-ticker breakdown: which tickers have that group's features on that date.
export interface CellDrill {
  generated_at: string | null;
  group: string;
  date: string;
  trusted: boolean;
  n_tickers: number;
  universe: number;
  coverage_pct: number;
  limit: number;
  tickers: string[];
}

// The matrix / meta endpoints return 503 with this body before the worker's first build lands.
export interface BootingResponse {
  booting: true;
  detail?: string;
}
