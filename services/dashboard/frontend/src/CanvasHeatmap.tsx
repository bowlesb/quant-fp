import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { StoreGridMatrix } from "./types";
import { CELL, COLORS, cellColor } from "./theme";

// A display column is either a base column (raw layer or group) or a feature sub-column of an expanded group.
// `coverageCol` indexes matrix.coverage[date] for this column's byte — for a feature sub-column that's its
// parent GROUP's coverage index (features share the group's coverage by construction).
export interface DisplayColumn {
  kind: "raw" | "group" | "feature";
  key: string;
  label: string;
  trusted: boolean;
  coverageCol: number;
  groupKey: string | null;
  expandable: boolean;
  expanded: boolean;
}

export interface HoverCell {
  rowIndex: number;
  displayCol: number;
  clientX: number;
  clientY: number;
}

interface Props {
  matrix: StoreGridMatrix;
  expandedGroups: Set<string>;
  highlightCol: string | null;
  onHoverChange: (cell: HoverCell | null) => void;
  onToggleExpand: (groupKey: string) => void;
  onOpenDetail: (columnKey: string) => void;
  // The group key currently under the pointer (header OR a body cell), so a "press K for detail" shortcut can
  // target it without the mouse chasing a hover menu. null when the pointer is off any group column.
  onHoverGroupChange: (groupKey: string | null) => void;
}

// The contiguous display-column span [start, end) covered by each EXPANDED group (its own column + its
// feature sub-columns) — used to outline the expansion as one block.
function expandedSpans(displayCols: DisplayColumn[]): { groupKey: string; start: number; end: number }[] {
  const spans: { groupKey: string; start: number; end: number }[] = [];
  let current: { groupKey: string; start: number; end: number } | null = null;
  displayCols.forEach((dc, c) => {
    if (dc.kind === "group" && dc.expanded && dc.groupKey) {
      current = { groupKey: dc.groupKey, start: c, end: c + 1 };
      spans.push(current);
    } else if (dc.kind === "feature" && current && dc.groupKey === current.groupKey) {
      current.end = c + 1;
    } else {
      current = null;
    }
  });
  return spans;
}

function buildDisplayColumns(matrix: StoreGridMatrix, expanded: Set<string>): DisplayColumn[] {
  const out: DisplayColumn[] = [];
  matrix.columns.forEach((col, idx) => {
    if (col.kind === "group") {
      const isExpanded = expanded.has(col.key);
      out.push({
        kind: "group",
        key: col.key,
        label: col.label,
        trusted: col.trusted,
        coverageCol: idx,
        groupKey: col.key,
        expandable: col.features.length > 0,
        expanded: isExpanded,
      });
      if (isExpanded) {
        col.features.forEach((feature) => {
          out.push({
            kind: "feature",
            key: `${col.key}::${feature}`,
            label: feature,
            trusted: col.trusted,
            coverageCol: idx,
            groupKey: col.key,
            expandable: false,
            expanded: false,
          });
        });
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
}

export function CanvasHeatmap({
  matrix,
  expandedGroups,
  highlightCol,
  onHoverChange,
  onToggleExpand,
  onOpenDetail,
  onHoverGroupChange,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [viewport, setViewport] = useState({ scrollTop: 0, width: 0, height: 0 });

  const displayCols = useMemo(() => buildDisplayColumns(matrix, expandedGroups), [matrix, expandedGroups]);
  const spans = useMemo(() => expandedSpans(displayCols), [displayCols]);
  const nDates = matrix.dates.length;
  const nCols = displayCols.length;
  const contentWidth = nCols * CELL.w;
  const contentHeight = nDates * CELL.h;

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const measure = () =>
      setViewport({ scrollTop: node.scrollTop, width: node.clientWidth, height: node.clientHeight });
    measure();
    const onScroll = () => measure();
    node.addEventListener("scroll", onScroll, { passive: true });
    const resizeObserver = new ResizeObserver(measure);
    resizeObserver.observe(node);
    return () => {
      node.removeEventListener("scroll", onScroll);
      resizeObserver.disconnect();
    };
  }, []);

  const visibleRows = useMemo(() => {
    const overscan = 4;
    const firstRow = Math.max(0, Math.floor(viewport.scrollTop / CELL.h) - overscan);
    const lastRow = Math.min(nDates, Math.ceil((viewport.scrollTop + viewport.height) / CELL.h) + overscan);
    return { firstRow, lastRow };
  }, [viewport, nDates]);

  // Paint visible rows × all display columns onto a viewport-tall canvas overlay. White background (cleared)
  // == zero coverage; a present cell darkens toward its column colour.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = globalThis.devicePixelRatio ?? 1;
    const cssW = contentWidth;
    const cssH = viewport.height;
    if (cssW === 0 || cssH === 0) return;
    const pxW = Math.round(cssW * dpr);
    const pxH = Math.round(cssH * dpr);
    if (canvas.width !== pxW || canvas.height !== pxH) {
      canvas.width = pxW;
      canvas.height = pxH;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const { firstRow, lastRow } = visibleRows;
    const offY = viewport.scrollTop;

    // Distinct background band behind each EXPANDED group (its column + its feature sub-columns), so the
    // expansion reads as one block against the white field.
    spans.forEach((span) => {
      ctx.fillStyle = "rgba(31,111,235,0.07)";
      ctx.fillRect(span.start * CELL.w, 0, (span.end - span.start) * CELL.w, cssH);
    });

    for (let row = firstRow; row < lastRow; row++) {
      const coverageRow = matrix.coverage[row];
      const y = row * CELL.h - offY;
      for (let c = 0; c < nCols; c++) {
        const dc = displayCols[c];
        const byte = coverageRow[dc.coverageCol] ?? 0;
        const fill = cellColor(byte, dc.kind === "raw" ? "raw" : "group", dc.trusted);
        if (fill == null) continue;
        ctx.fillStyle = fill;
        ctx.fillRect(c * CELL.w, y, CELL.w - CELL.gap, CELL.h - CELL.gap);
      }
    }

    // Crisp accent OUTLINE around each expanded group's block, so it reads unmistakably as belonging to that
    // one group, set apart from the neighbouring groups.
    ctx.strokeStyle = COLORS.accent;
    ctx.lineWidth = 1.5;
    spans.forEach((span) => {
      ctx.strokeRect(span.start * CELL.w - 0.5, 0.5, (span.end - span.start) * CELL.w, cssH - 1);
    });

    if (highlightCol != null) {
      const c = displayCols.findIndex((dc) => dc.groupKey === highlightCol && dc.kind === "group");
      if (c >= 0) {
        ctx.strokeStyle = COLORS.accent;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(c * CELL.w - 0.5, 0, CELL.w + 1, cssH);
      }
    }
  }, [matrix, displayCols, spans, nCols, highlightCol, viewport, visibleRows, contentWidth]);

  const colAt = useCallback(
    (clientX: number, clientY: number): { row: number; col: number } | null => {
      const node = scrollRef.current;
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      const localX = clientX - rect.left + node.scrollLeft;
      const localY = clientY - rect.top + node.scrollTop;
      const col = Math.floor(localX / CELL.w);
      const row = Math.floor(localY / CELL.h);
      if (col < 0 || col >= nCols || row < 0 || row >= nDates) return null;
      return { row, col };
    },
    [nCols, nDates],
  );

  const onMouseMove = useCallback(
    (event: React.MouseEvent) => {
      const hit = colAt(event.clientX, event.clientY);
      if (!hit) {
        onHoverChange(null);
        onHoverGroupChange(null);
        return;
      }
      onHoverChange({
        rowIndex: hit.row,
        displayCol: hit.col,
        clientX: event.clientX,
        clientY: event.clientY,
      });
      onHoverGroupChange(displayCols[hit.col]?.groupKey ?? null);
    },
    [colAt, displayCols, onHoverChange, onHoverGroupChange],
  );

  const onBodyLeave = useCallback(() => {
    onHoverChange(null);
    onHoverGroupChange(null);
  }, [onHoverChange, onHoverGroupChange]);

  const dateLabels = useMemo(() => {
    const { firstRow, lastRow } = visibleRows;
    const labels: { y: number; text: string; major: boolean }[] = [];
    let lastMonth = "";
    for (let row = firstRow; row < lastRow; row++) {
      const date = matrix.dates[row];
      if (!date) continue;
      const month = date.slice(0, 7);
      const isMonthStart = month !== lastMonth;
      lastMonth = month;
      if (!isMonthStart && row % 5 !== 0) continue;
      labels.push({
        y: row * CELL.h - viewport.scrollTop,
        text: isMonthStart ? date.slice(0, 7) : date.slice(8),
        major: isMonthStart,
      });
    }
    return labels;
  }, [visibleRows, matrix.dates, viewport.scrollTop]);

  return (
    <div className="heatmap-frame">
      <div className="col-header" style={{ paddingLeft: 58 }}>
        <div className="col-header-inner" style={{ width: contentWidth }}>
          {/* A bracket + group-name chip spanning each expanded group's columns, so the feature sub-columns
              read unmistakably as belonging to that one group. */}
          {spans.map((span) => (
            <div
              key={`span-${span.groupKey}`}
              className="col-span-bracket"
              style={{ left: span.start * CELL.w, width: (span.end - span.start) * CELL.w }}
            >
              <span className="col-span-name">{span.groupKey}</span>
            </div>
          ))}
          {displayCols.map((dc, c) => {
            const isGroup = dc.kind === "group";
            const isExpandable = isGroup && dc.expandable;
            // Clicking a group header TOGGLES its feature expand (the primary interaction); a feature sub-column
            // header opens its parent group's detail. K (handled in App) opens detail for the hovered group, so
            // detail is never gated on clicking the header.
            const onLabelClick = () => {
              if (isExpandable && dc.groupKey) onToggleExpand(dc.groupKey);
              else if (dc.kind === "feature" && dc.groupKey) onOpenDetail(dc.groupKey);
              else if (isGroup) onOpenDetail(dc.key);
            };
            const title = isExpandable
              ? `${dc.label} — click to ${dc.expanded ? "collapse" : "expand"} features · press K for detail`
              : dc.kind === "feature"
                ? `${dc.label} — click for ${dc.groupKey} detail`
                : dc.label;
            return (
              <div
                key={dc.key}
                className={
                  "col-label" +
                  ` k-${dc.kind}` +
                  (dc.trusted ? " trusted" : isGroup ? " untrusted" : "") +
                  (isExpandable ? " expandable" : "") +
                  (dc.expanded ? " expanded" : "") +
                  (highlightCol === dc.groupKey && isGroup ? " active" : "")
                }
                style={{ left: c * CELL.w, width: CELL.w }}
                title={title}
                onClick={onLabelClick}
                onMouseEnter={() => onHoverGroupChange(dc.groupKey)}
                onMouseLeave={() => onHoverGroupChange(null)}
              >
                <span>
                  {isExpandable ? (
                    <span className={"col-chevron" + (dc.expanded ? " open" : "")}>▸</span>
                  ) : null}
                  {dc.label}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      <div className="heatmap-row">
        <div className="date-gutter" aria-hidden>
          {dateLabels.map((label, idx) => (
            <div key={idx} className={label.major ? "date-tick major" : "date-tick"} style={{ top: label.y }}>
              {label.text}
            </div>
          ))}
        </div>
        <div className="heatmap-body">
          <div
            ref={scrollRef}
            className="heatmap-scroll"
            onMouseMove={onMouseMove}
            onMouseLeave={onBodyLeave}
          >
            <div style={{ width: contentWidth, height: contentHeight }} />
          </div>
          <canvas
            ref={canvasRef}
            className="heatmap-canvas"
            style={{ width: contentWidth, height: viewport.height }}
          />
        </div>
      </div>
    </div>
  );
}
