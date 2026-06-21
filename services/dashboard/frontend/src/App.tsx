import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BootingError, fetchMatrix, fetchMeta } from "./api";
import type { GridColumn, GridMeta, StoreGridMatrix } from "./types";
import { CanvasHeatmap, type HoverCell, type DisplayColumn } from "./CanvasHeatmap";
import { Tooltip } from "./Tooltip";
import { GroupDetailPanel } from "./GroupDetailPanel";

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
  const [detailKey, setDetailKey] = useState<string | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // The group column under the pointer right now (header or body cell); the "press K for detail" shortcut and
  // the toolbar detail button target it.
  const [hoveredGroup, setHoveredGroup] = useState<string | null>(null);
  // The detail sidebar is OPTIONAL. `sidebarHidden` force-hides it (toolbar toggle + Esc), and the preference
  // persists for the session so a user who dismisses it keeps the full-width grid across reloads.
  const [sidebarHidden, setSidebarHidden] = useState<boolean>(
    () => sessionStorage.getItem("sidebarHidden") === "1",
  );
  const matrixGeneratedAt = useRef<string | null>(null);
  // Latest hovered/expanded group, read by the keydown handler without re-binding it every hover.
  const hoveredGroupRef = useRef<string | null>(null);
  hoveredGroupRef.current = hoveredGroup;
  const expandedGroupsRef = useRef<Set<string>>(expandedGroups);
  expandedGroupsRef.current = expandedGroups;

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

  // Opening detail (via K or a header click) is an explicit request to SEE detail, so it reveals the sidebar
  // even if previously hidden — Hide/Esc/the toolbar toggle are the way to keep it away.
  const openDetail = useCallback((columnKey: string) => {
    setSidebarHidden(false);
    sessionStorage.setItem("sidebarHidden", "0");
    setDetailKey(columnKey);
  }, []);

  const closeDetail = useCallback(() => setDetailKey(null), []);

  const toggleSidebar = useCallback(() => {
    setSidebarHidden((prev) => {
      const next = !prev;
      sessionStorage.setItem("sidebarHidden", next ? "1" : "0");
      return next;
    });
  }, []);

  const toggleExpand = useCallback((groupKey: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupKey)) next.delete(groupKey);
      else next.add(groupKey);
      return next;
    });
  }, []);

  // Keyboard: "K" opens the detail sidebar for the group currently under the pointer (falling back to the sole
  // expanded group), so detail never requires chasing a hover menu; Esc closes/hides the sidebar. Ignored while
  // typing in an input/textarea/contenteditable.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      if (event.key === "k" || event.key === "K") {
        const expanded = expandedGroupsRef.current;
        const group = hoveredGroupRef.current ?? (expanded.size === 1 ? [...expanded][0] : null);
        if (group) {
          event.preventDefault();
          openDetail(group);
        }
      } else if (event.key === "Escape") {
        setDetailKey(null);
        setSidebarHidden(true);
        sessionStorage.setItem("sidebarHidden", "1");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [openDetail]);

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

  const detailColumn: GridColumn | null =
    matrix && detailKey ? matrix.columns.find((c) => c.key === detailKey) ?? null : null;

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
          <button
            className={"sidebar-toggle" + (sidebarHidden ? "" : " on")}
            onClick={toggleSidebar}
            title={sidebarHidden ? "Show the detail sidebar" : "Hide the detail sidebar"}
            aria-pressed={!sidebarHidden}
          >
            {sidebarHidden ? "Show detail ›" : "Hide detail ‹"}
          </button>
        </div>
      </header>

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
            highlightCol={sidebarHidden ? null : detailKey}
            onHoverChange={setHover}
            onToggleExpand={toggleExpand}
            onOpenDetail={openDetail}
            onHoverGroupChange={setHoveredGroup}
          />
        )}
      </div>

      {matrix && <Tooltip hover={hover} matrix={matrix} displayCols={displayCols} />}

      {detailColumn && !sidebarHidden && (
        <GroupDetailPanel
          column={detailColumn}
          info={matrix?.group_info[detailColumn.key]}
          onClose={closeDetail}
          onHide={toggleSidebar}
        />
      )}
    </div>
  );
}
