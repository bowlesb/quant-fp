// Shapes returned by the dashboard's /api/store-grid/* endpoints (see services/dashboard/store_grid.py).

export interface GridLegend {
  coverage_scale: string;
  trust_overlay: string;
  depth_note: string;
}

export interface GridSummary {
  n_dates: number;
  n_tickers: number;
  n_groups: number;
  n_trusted_groups: number;
  mean_coverage_pct: number;
}

// The matrix endpoint serves coverage/trusted as nested byte/bit rows (rows aligned to `dates`, columns to
// `tickers`). coverage[i][j] is a 0..255 darkness byte; trusted[i][j] is 1 iff every group covering that
// ticker x date is fully trusted. coverage_pct[j] is ticker j's mean coverage over its present dates.
export interface StoreGridMatrix {
  generated_at: string;
  store_root: string;
  anchor_date: string | null;
  lookback_days: number;
  n_groups: number;
  n_trusted_groups: number;
  dates: string[];
  tickers: string[];
  coverage: number[][];
  trusted: number[][];
  coverage_pct: number[];
  summary: GridSummary;
  legend: GridLegend;
}

export interface GridMeta {
  generated_at: string;
  anchor_date: string | null;
  lookback_days: number;
  n_dates: number;
  n_tickers: number;
  n_groups: number;
  n_trusted_groups: number;
  mean_coverage_pct: number;
  raw_bytes: number;
  gzip_bytes: number;
  drills_prewarmed: number;
  build_seconds: number;
}

// One ticker's per-(date x group) presence drill. `cells[date][group] === true` when the ticker had that
// group on that date; `groups[].trusted` is the group's binary-trust flag.
export interface DrillGroup {
  group: string;
  trusted: boolean;
}

export interface TickerDrill {
  generated_at: string;
  symbol: string;
  anchor_date: string | null;
  lookback_days: number;
  groups: DrillGroup[];
  dates: string[];
  cells: Record<string, Record<string, boolean>>;
}

// The matrix / meta endpoints return 503 with this body before the worker's first build lands.
export interface BootingResponse {
  booting: true;
  detail?: string;
}
