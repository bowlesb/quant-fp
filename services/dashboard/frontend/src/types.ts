// Shapes returned by the dashboard's /api/store-grid/* endpoints (see services/dashboard/store_grid.py, v3).

import type { ColumnKind } from "./theme";

export interface GridColumn {
  key: string;
  label: string;
  kind: ColumnKind; // "raw" (tape layer) | "group" (feature group)
  trusted: boolean; // groups only: true iff all the group's features are trusted
  features: string[]; // groups only: the feature inventory, for the horizontal expand
}

export interface GridSummary {
  n_dates: number;
  n_columns: number;
  n_groups: number;
  n_trusted_groups: number;
  n_raw: number;
  mean_coverage_pct: number;
  universe_size: number;
}

// `coverage[i][j]` is a 0..255 byte = (tickers with column j on date i) / universe_size. Columns are raw
// layers first (the substrate), then feature groups trusted-first. group columns carry their `features`.
export interface StoreGridMatrix {
  generated_at: string;
  store_root: string;
  anchor_date: string | null;
  lookback_days: number;
  universe_size: number;
  n_groups: number;
  n_trusted_groups: number;
  dates: string[];
  columns: GridColumn[];
  coverage: number[][];
  column_coverage_pct: number[];
  summary: GridSummary;
}

export interface GridMeta {
  generated_at: string;
  anchor_date: string | null;
  lookback_days: number;
  universe_size: number;
  n_dates: number;
  n_columns: number;
  n_groups: number;
  n_trusted_groups: number;
  mean_coverage_pct: number;
  raw_bytes: number;
  gzip_bytes: number;
  drills_written: number;
  build_seconds: number;
}

// One (date x group) cell's per-ticker breakdown (the secondary drill, still available on a group cell click).
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

export interface BootingResponse {
  booting: true;
  detail?: string;
}
