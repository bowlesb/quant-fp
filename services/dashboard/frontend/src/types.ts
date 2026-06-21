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

// One feature's registry description (the "how the features differ" content, authored for every feature).
export interface FeatureDescription {
  name: string;
  description: string;
}

// The curated narrative for a group (docs/feature_group_guide.yaml). null when no entry is written yet.
export interface GroupGuide {
  purpose?: string;
  how_features_differ?: string;
  value?: string;
  example?: string;
}

// Per-column detail-panel content, baked into the matrix doc by the worker (zero request-path cost).
export interface GroupInfo {
  docstring: string;
  type: string;
  layer: string;
  n_features: number;
  features: FeatureDescription[];
  guide: GroupGuide | null;
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
  group_info: Record<string, GroupInfo>;
  coverage: number[][];
  // Parallel to `coverage`: per feature cell the STREAM fraction byte (0..255) of its covered tickers
  // (0 = entirely backfill-only, 255 = every covered ticker is live-stream-present). -1 (SOURCE_NA) for raw
  // tape layers and absent cells. Makes the live-vs-backfill provenance of a cell legible on hover.
  coverage_source: number[][];
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

// A ticker's source provenance within a cell: `both` (live + backfill), `stream_only` (live, not yet
// backfilled), `backfill_only` (in history but NOT captured live = the FP_TICK_SYMBOLS live-coverage gap).
export type TickerSource = "both" | "stream_only" | "backfill_only";

// The per-cell stream/backfill rollup counts.
export interface SourceCounts {
  stream: number;
  backfill: number;
  both: number;
  stream_only: number;
  backfill_only: number;
}

// One (date x group) cell's per-ticker breakdown (the secondary drill, still available on a group cell click).
// `ticker_sources` is parallel to `tickers`; `source_counts` is the stream-vs-backfill rollup for the cell.
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
  ticker_sources: TickerSource[];
  source_counts: SourceCounts;
}

export interface BootingResponse {
  booting: true;
  detail?: string;
}

// Shapes returned by /api/latency-expectations (docs/feature_latency_expectations.json, #321). One per-group
// `compute_latest` latency profile, served slowest-first, with the e2e bar->vector context header.
export interface LatencyGroup {
  group: string;
  feat_count: number;
  kind: string;
  mechanism: string;
  incremental_ready: string;
  p50_ms: number;
  // p95_ms is being added to the living artifact by the measurement loop; optional so the UI degrades
  // gracefully (bar = p50, hover = p99 always + p95 only when present) until the field lands in the JSON.
  p95_ms?: number;
  p99_ms: number;
}

export interface LatencyE2EContext {
  metric: string;
  single_bet_isolated_p50_ms: number;
  typical_bet_under_load_p50_ms: number;
  target_p99_ms: number;
  note: string;
}

export interface LatencyExpectations {
  schema_version: number;
  generated_at: string;
  units: string;
  sorted_by: string;
  e2e_context: LatencyE2EContext;
  group_count: number;
  feature_count: number;
  groups: LatencyGroup[];
}

// Shapes returned by /api/news-edgar/* — the News & Filings tab (services/dashboard/news_edgar.py). The
// `stream` route is the live rate (uncached); `composition` is the slowly-changing store snapshot (TTL-cached).

// One source's freshness, graded business-hours-aware exactly as the data_freshness cron alert does.
// status: OK | WARN | STALE (during SEC business hours) | INACTIVE (expected weekend/overnight lull) | ERROR.
export interface FreshnessStatus {
  status: string;
  age_minutes: number | null;
  newest_iso: string | null;
  in_business_hours: boolean;
  detail: string;
}

// A per-minute arrival bucket for the recent timeline.
export interface TimelinePoint {
  minute: string;
  count: number;
}

// One source's live-stream block (articles or filings). `error` replaces the body if the source was briefly
// unreachable. `per_min` is over the trailing `window_minutes`.
export interface StreamRate {
  per_min: number;
  window_count: number;
  window_minutes: number;
  timeline: TimelinePoint[];
  freshness: FreshnessStatus;
  error?: string;
}

export interface NewsEdgarStream {
  generated_at: string;
  edgar: StreamRate | { error: string };
  news: StreamRate | { error: string };
}

export interface SymbolCount {
  symbol: string;
  count: number;
}

export interface FormTypeCount {
  form_type: string;
  count: number;
}

export interface NewsComposition {
  total_articles: number;
  n_symbols: number;
  earliest_date: string | null;
  latest_date: string | null;
  top_symbols: SymbolCount[];
}

export interface FilingsComposition {
  total_filings: number;
  stream_filings: number;
  earliest_available_at: string | null;
  latest_available_at: string | null;
  form_types: FormTypeCount[];
  error?: string;
}

// One feature computed (or coming) off the news/edgar tapes. status: LIVE | COMING.
export interface FeatureStatus {
  label: string;
  source: string;
  status: string;
  detail: string;
}

export interface NewsEdgarComposition {
  generated_at: string;
  news: NewsComposition;
  filings: FilingsComposition;
  features: FeatureStatus[];
  cached: boolean;
  cache_age_seconds: number;
}
