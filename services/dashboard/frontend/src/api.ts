import type { CellDrill, GridMeta, StoreGridMatrix } from "./types";

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
