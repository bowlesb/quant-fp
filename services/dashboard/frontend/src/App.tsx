import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BootingError, fetchMatrix, fetchMeta } from "./api";
import type { GridColumn, GridMeta, StoreGridMatrix } from "./types";
import { CanvasHeatmap, type HoverCell, type DisplayColumn } from "./CanvasHeatmap";
import { Tooltip } from "./Tooltip";
import { GroupDetailPanel } from "./GroupDetailPanel";
import { LatencyView } from "./LatencyView";

// The worker rebuilds every 10 min; a 60s meta poll is plenty. The matrix blob is only re-fetched when its
// generated_at advances.
const META_POLL_MS = 60_000;

// The two top-level views. The coverage grid stays the default; "latency" is the additive #321 read-side page.
type View = "grid" | "latency";

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
  const [detailKey, setDetailKey] = useState<string | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [view, setView] = useState<View>("grid");
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

  const openDetail = useCallback((columnKey: string) => setDetailKey(columnKey), []);

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

  // The group key under the cursor right now, or null over a raw layer / off-grid. Pressing "K" opens that
  // group's detail panel without a mouse-chase to the hover tooltip's link (the affordance Ben lost when the
  // cursor moved off the column). Mirrors the click path's feature→parent-group resolution.
  const hoveredGroupKey = useMemo<string | null>(() => {
    if (!hover) return null;
    const dc = displayCols[hover.displayCol];
    return dc ? dc.groupKey : null;
  }, [hover, displayCols]);

  const hoveredGroupRef = useRef<string | null>(null);
  hoveredGroupRef.current = hoveredGroupKey;

  // Keyboard: "K" opens the hovered group's detail panel; Esc closes it. The ref keeps the handler stable
  // (registered once) while always reading the latest hovered group. Ignored while typing in a field.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      if (event.key === "Escape") {
        setDetailKey(null);
        return;
      }
      if (event.key === "k" || event.key === "K") {
        const groupKey = hoveredGroupRef.current;
        if (groupKey) {
          event.preventDefault();
          setDetailKey(groupKey);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const detailColumn: GridColumn | null =
    matrix && detailKey ? matrix.columns.find((c) => c.key === detailKey) ?? null : null;

  // The first-boot screen is grid-only: it must never block the latency view, which serves a static artifact
  // independent of the always-warm grid cache.
  if (view === "grid" && booting && !matrix) {
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
          <h1>{view === "grid" ? "Feature-store coverage" : "Feature latency expectations"}</h1>
        </div>
        <div className="topbar-center">
          {view === "grid" && meta && (
            <span className="asof" title={`generated ${meta.generated_at}`}>
              {formatAsOf(meta.generated_at)}
            </span>
          )}
        </div>
        <nav className="view-tabs">
          <button
            className={`view-tab${view === "grid" ? " active" : ""}`}
            onClick={() => setView("grid")}
          >
            Coverage grid
          </button>
          <button
            className={`view-tab${view === "latency" ? " active" : ""}`}
            onClick={() => setView("latency")}
          >
            Latency
          </button>
        </nav>
      </header>

      {view === "latency" ? (
        <LatencyView />
      ) : (
        <>
          <div className="controls">
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
                <span className="legend-item">
                  hover a cell for <span className="src-stream">live</span>/
                  <span className="src-backfill">backfill-only</span> source split
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
                highlightCol={detailKey}
                onHoverChange={setHover}
                onToggleExpand={toggleExpand}
                onOpenDetail={openDetail}
              />
            )}
          </div>

          {matrix && <Tooltip hover={hover} matrix={matrix} displayCols={displayCols} />}

          {detailColumn && (
            <GroupDetailPanel
              column={detailColumn}
              info={matrix?.group_info[detailColumn.key]}
              onClose={() => setDetailKey(null)}
            />
          )}
        </>
      )}
    </div>
  );
}
