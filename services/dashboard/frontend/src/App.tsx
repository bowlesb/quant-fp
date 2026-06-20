import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BootingError, fetchMatrix, fetchMeta } from "./api";
import type { GridMeta, StoreGridMatrix } from "./types";
import { CanvasHeatmap, type HoverCell } from "./CanvasHeatmap";
import { Tooltip } from "./Tooltip";
import { DrillPanel } from "./DrillPanel";

// How often to re-poll the meta header for the "as of" staleness, and to pick up a freshly-built matrix. The
// worker rebuilds every few minutes; a 60s meta poll is plenty and cheap. The matrix itself is only re-fetched
// when its generated_at advances (avoids re-downloading the multi-hundred-KB blob every minute).
const META_POLL_MS = 60_000;

function formatAsOf(generatedAt: string): string {
  const then = new Date(generatedAt);
  const secs = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
  const hhmmss = then.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (secs < 90) return `as of ${hhmmss} (${secs}s ago)`;
  const mins = Math.round(secs / 60);
  return `as of ${hhmmss} (${mins}m ago)`;
}

export function App() {
  const [matrix, setMatrix] = useState<StoreGridMatrix | null>(null);
  const [meta, setMeta] = useState<GridMeta | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trustOverlay, setTrustOverlay] = useState(false);
  const [hover, setHover] = useState<HoverCell | null>(null);
  const [query, setQuery] = useState("");
  const [activeCol, setActiveCol] = useState<number | null>(null);
  const [scrollToCol, setScrollToCol] = useState<number | null>(null);
  const matrixGeneratedAt = useRef<string | null>(null);

  // Initial + conditional matrix load. We refetch the blob only when meta says a newer build exists.
  const loadMatrix = useCallback(async () => {
    try {
      const data = await fetchMatrix();
      matrixGeneratedAt.current = data.generated_at;
      setMatrix(data);
      setBooting(false);
      setError(null);
    } catch (err) {
      if (err instanceof BootingError) setBooting(true);
      else setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    loadMatrix();
  }, [loadMatrix]);

  // Poll meta for staleness; if the worker produced a newer matrix, pull it. If we were booting, the first
  // successful meta means the worker is live — load the matrix.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const m = await fetchMeta();
        if (cancelled) return;
        setMeta(m);
        if (m.generated_at !== matrixGeneratedAt.current) {
          await loadMatrix();
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof BootingError) setBooting(true);
        // a transient meta error is non-fatal: keep showing the last good matrix.
      }
    };
    poll();
    const id = window.setInterval(poll, META_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [loadMatrix]);

  // Re-render the "as of" line every second without re-fetching.
  const [, forceTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Ticker search: resolve the query to a column index (exact match first, then prefix), jump + open drill.
  const tickerIndex = useMemo(() => {
    const map = new Map<string, number>();
    matrix?.tickers.forEach((ticker, idx) => map.set(ticker, idx));
    return map;
  }, [matrix]);

  const runSearch = useCallback(() => {
    if (!matrix) return;
    const upper = query.trim().toUpperCase();
    if (!upper) return;
    let col = tickerIndex.get(upper);
    if (col == null) {
      col = matrix.tickers.findIndex((ticker) => ticker.startsWith(upper));
      if (col < 0) col = undefined as unknown as number;
    }
    if (col == null || col < 0) {
      setError(`ticker "${upper}" not in the grid`);
      return;
    }
    setError(null);
    setActiveCol(col);
    setScrollToCol(col);
  }, [matrix, query, tickerIndex]);

  const pickColumn = useCallback((colIndex: number) => {
    setActiveCol(colIndex);
  }, []);

  const activeSymbol = activeCol != null && matrix ? matrix.tickers[activeCol] : null;
  const activeCoveragePct = activeCol != null && matrix ? matrix.coverage_pct[activeCol] ?? null : null;

  if (booting && !matrix) {
    return (
      <div className="boot-screen">
        <div className="boot-card">
          <div className="boot-spinner" />
          <div className="boot-title">Building the coverage grid…</div>
          <div className="boot-sub">
            First-ever build in progress. This one-time wait happens only on a fresh worker boot — the grid is
            kept always-warm afterwards.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-left">
          <div className="brand">
            <span className="brand-mark" />
            <h1>Feature-store coverage</h1>
          </div>
          {matrix && (
            <div className="chips">
              <span className="chip">
                <strong>{matrix.summary.n_tickers.toLocaleString()}</strong> tickers
              </span>
              <span className="chip">
                <strong>{matrix.summary.n_dates}</strong> dates
              </span>
              <span className="chip">
                <strong>{matrix.summary.n_groups}</strong> groups
              </span>
              <span className="chip trust">
                <strong>{matrix.summary.n_trusted_groups}</strong>/{matrix.summary.n_groups} trusted
              </span>
            </div>
          )}
        </div>
        <div className="topbar-right">
          {meta && (
            <span className="asof" title={`generated ${meta.generated_at}`}>
              <span className="pulse" />
              {formatAsOf(meta.generated_at)}
            </span>
          )}
        </div>
      </header>

      <div className="controls">
        <div className="search">
          <input
            type="text"
            placeholder="search ticker (e.g. AAPL) — Enter to jump"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") runSearch();
            }}
            spellCheck={false}
          />
          <button onClick={runSearch}>Jump</button>
        </div>
        <button
          className={trustOverlay ? "toggle on" : "toggle"}
          onClick={() => setTrustOverlay((value) => !value)}
          title="Colour cells by binary trust instead of plain coverage"
        >
          <span className="toggle-dot" />
          Trust overlay
        </button>
        {matrix && (
          <div className="legend">
            {!trustOverlay ? (
              <span className="legend-item">
                <em className="ramp-end">sparse</em>
                <span className="ramp coverage-ramp" />
                <em className="ramp-end">full</em>
              </span>
            ) : (
              <>
                <span className="legend-item">
                  <span className="ramp trusted-ramp" /> all trusted
                </span>
                <span className="legend-item">
                  <span className="ramp untrusted-ramp" /> some untrusted
                </span>
              </>
            )}
          </div>
        )}
      </div>

      {matrix && (
        <div className="legend-notes">
          <span>{matrix.legend.coverage_scale}</span>
          <span className="depth-note">{matrix.legend.depth_note}</span>
        </div>
      )}

      {error && <div className="banner-error">{error}</div>}

      <div className="grid-region">
        {matrix && (
          <CanvasHeatmap
            matrix={matrix}
            trustOverlay={trustOverlay}
            highlightCol={activeCol}
            onHoverChange={setHover}
            onPickTicker={pickColumn}
            scrollToCol={scrollToCol}
          />
        )}
      </div>

      {matrix && <Tooltip hover={hover} matrix={matrix} trustOverlay={trustOverlay} />}

      {activeSymbol && (
        <DrillPanel
          symbol={activeSymbol}
          coveragePct={activeCoveragePct}
          onClose={() => setActiveCol(null)}
        />
      )}
    </div>
  );
}
