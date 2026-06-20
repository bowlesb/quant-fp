import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BootingError, fetchMatrix, fetchMeta } from "./api";
import type { GridMeta, StoreGridMatrix } from "./types";
import { CanvasHeatmap, type HoverCell, type DisplayColumn } from "./CanvasHeatmap";
import { Tooltip } from "./Tooltip";

// The worker rebuilds every 10 min; a 60s meta poll is plenty. The matrix blob is only re-fetched when its
// generated_at advances.
const META_POLL_MS = 60_000;

function formatAsOf(generatedAt: string): string {
  const then = new Date(generatedAt);
  const secs = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
  const hhmm = then.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (secs < 90) return `as of ${hhmm}`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `as of ${hhmm} · ${mins}m ago`;
  return `as of ${hhmm}`;
}

export function App() {
  const [matrix, setMatrix] = useState<StoreGridMatrix | null>(null);
  const [meta, setMeta] = useState<GridMeta | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<HoverCell | null>(null);
  const [query, setQuery] = useState("");
  const [highlightCol, setHighlightCol] = useState<string | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const matrixGeneratedAt = useRef<string | null>(null);

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

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const m = await fetchMeta();
        if (cancelled) return;
        setMeta(m);
        if (m.generated_at !== matrixGeneratedAt.current) await loadMatrix();
      } catch (err) {
        if (cancelled) return;
        if (err instanceof BootingError) setBooting(true);
      }
    };
    poll();
    const id = window.setInterval(poll, META_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [loadMatrix]);

  const [, forceTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => forceTick((n) => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const runSearch = useCallback(() => {
    if (!matrix) return;
    const needle = query.trim().toLowerCase();
    if (!needle) {
      setHighlightCol(null);
      return;
    }
    const exact = matrix.columns.find((c) => c.kind === "group" && c.key.toLowerCase() === needle);
    const partial = matrix.columns.find((c) => c.kind === "group" && c.key.toLowerCase().includes(needle));
    const hit = exact ?? partial;
    if (!hit) {
      setError(`no feature-group matches "${query.trim()}"`);
      return;
    }
    setError(null);
    setHighlightCol(hit.key);
  }, [matrix, query]);

  const toggleExpand = useCallback((groupKey: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupKey)) next.delete(groupKey);
      else next.add(groupKey);
      return next;
    });
  }, []);

  // The display columns the tooltip needs (mirrors the heatmap's expansion); cheap to recompute.
  const displayCols = useMemo<DisplayColumn[]>(() => {
    if (!matrix) return [];
    const out: DisplayColumn[] = [];
    matrix.columns.forEach((col, idx) => {
      if (col.kind === "group") {
        const expanded = expandedGroups.has(col.key);
        out.push({
          kind: "group",
          key: col.key,
          label: col.label,
          trusted: col.trusted,
          coverageCol: idx,
          groupKey: col.key,
          expandable: col.features.length > 0,
          expanded,
        });
        if (expanded) {
          col.features.forEach((feature) =>
            out.push({
              kind: "feature",
              key: `${col.key}::${feature}`,
              label: feature,
              trusted: col.trusted,
              coverageCol: idx,
              groupKey: col.key,
              expandable: false,
              expanded: false,
            }),
          );
        }
      } else {
        out.push({
          kind: "raw",
          key: col.key,
          label: col.label,
          trusted: false,
          coverageCol: idx,
          groupKey: null,
          expandable: false,
          expanded: false,
        });
      }
    });
    return out;
  }, [matrix, expandedGroups]);

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
          <span className="brand-mark" />
          <h1>Feature-store coverage</h1>
        </div>
        <div className="topbar-right">
          {meta && (
            <span className="asof" title={`generated ${meta.generated_at}`}>
              {formatAsOf(meta.generated_at)}
            </span>
          )}
        </div>
      </header>

      <div className="controls">
        <div className="search">
          <input
            type="text"
            placeholder="search a feature group (e.g. candlestick)"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") runSearch();
            }}
            spellCheck={false}
          />
          <button onClick={runSearch}>Find</button>
        </div>

        {matrix && (
          <div className="legend">
            <span className="legend-item">
              <span className="sw sw-empty" /> none
            </span>
            <span className="legend-item">
              <span className="ramp ramp-trusted" /> trusted feature
            </span>
            <span className="legend-item">
              <span className="ramp ramp-untrusted" /> untrusted feature
            </span>
            <span className="legend-item">
              <span className="ramp ramp-raw" /> raw tape layer
            </span>
            <span className="legend-uni">
              darkness = % of the {matrix.universe_size.toLocaleString()}-ticker universe covered
            </span>
          </div>
        )}
      </div>

      {error && <div className="banner-error">{error}</div>}

      <div className="grid-region">
        {matrix && (
          <CanvasHeatmap
            matrix={matrix}
            expandedGroups={expandedGroups}
            highlightCol={highlightCol}
            onHoverChange={setHover}
            onToggleExpand={toggleExpand}
          />
        )}
      </div>

      {matrix && <Tooltip hover={hover} matrix={matrix} displayCols={displayCols} />}
    </div>
  );
}
