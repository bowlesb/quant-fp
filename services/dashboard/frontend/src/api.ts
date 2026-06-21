import type {
  CellDrill,
  GridMeta,
  LatencyExpectations,
  NewsEdgarComposition,
  NewsEdgarStream,
  StatusGrid,
  StatusRow,
  StoreGridMatrix,
} from "./types";

// Thin client for the dashboard's /api/store-grid/* endpoints. The grid IS the dashboard (served at "/"), so
// the app is same-origin with the API — absolute /api/... paths hit the dashboard FastAPI directly (the Vite
// dev server proxies them to :8088). The matrix route returns gzip; the browser transparently decodes it via
// the Content-Encoding header, so `res.json()` works without any manual inflate.

// Sentinel thrown when the worker has not written its first matrix yet (the API replies 503 {booting:true}).
// The UI shows the one-and-only legitimate loading state for this; it is never a recurring "warming".
export class BootingError extends Error {
  constructor() {
    super("coverage matrix not built yet (first boot)");
    this.name = "BootingError";
  }
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (res.status === 503) {
    throw new BootingError();
  }
  if (!res.ok) {
    throw new Error(`${url} -> ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export function fetchMatrix(): Promise<StoreGridMatrix> {
  return getJson<StoreGridMatrix>("/api/store-grid/matrix");
}

export function fetchMeta(): Promise<GridMeta> {
  return getJson<GridMeta>("/api/store-grid/meta");
}

export function fetchCellDrill(group: string, date: string): Promise<CellDrill> {
  const params = new URLSearchParams({ group, date });
  return getJson<CellDrill>(`/api/store-grid/cell?${params.toString()}`);
}

export function fetchLatencyExpectations(): Promise<LatencyExpectations> {
  return getJson<LatencyExpectations>("/api/latency-expectations");
}

// The News & Filings tab. `stream` is the live rate (polled); `composition` is the slowly-changing store
// snapshot (TTL-cached server-side, so a single fetch per tab open is plenty).
export function fetchNewsEdgarStream(): Promise<NewsEdgarStream> {
  return getJson<NewsEdgarStream>("/api/news-edgar/stream");
}

export function fetchNewsEdgarComposition(): Promise<NewsEdgarComposition> {
  return getJson<NewsEdgarComposition>("/api/news-edgar/composition");
}

// The hourly Status tab. `fetchStatusGrid` reads the hour×workstream Progress/Blockers table (polled);
// `postReaction` records Ben's reaction to a row (the input box's write path), returning the updated row.
export function fetchStatusGrid(): Promise<StatusGrid> {
  return getJson<StatusGrid>("/api/status-grid");
}

export async function postReaction(hour: string, reaction: string): Promise<StatusRow> {
  const res = await fetch("/api/status-grid/reaction", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ hour, reaction }),
  });
  if (!res.ok) {
    throw new Error(`/api/status-grid/reaction -> ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as StatusRow;
}
