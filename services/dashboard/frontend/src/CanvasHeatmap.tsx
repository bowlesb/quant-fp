import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { StoreGridMatrix } from "./types";
import { CELL, COLORS, cellColor } from "./theme";

// Hovered cell identity passed up for the tooltip.
export interface HoverCell {
  rowIndex: number; // date index
  colIndex: number; // group index
  clientX: number;
  clientY: number;
}

interface Props {
  matrix: StoreGridMatrix;
  trustOverlay: boolean;
  // The group column to highlight (search jump / active drill), or null.
  highlightCol: number | null;
  onHoverChange: (cell: HoverCell | null) => void;
  // A click picks a (date row, group col) cell -> opens the per-ticker drill for that group+date.
  onPickCell: (rowIndex: number, colIndex: number) => void;
}

export function CanvasHeatmap({ matrix, trustOverlay, highlightCol, onHoverChange, onPickCell }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [viewport, setViewport] = useState({ scrollTop: 0, width: 0, height: 0 });

  const nDates = matrix.dates.length;
  const nGroups = matrix.groups.length;
  const contentWidth = nGroups * CELL.w;
  const contentHeight = nDates * CELL.h;

  // Track the scroll container's size + vertical scroll offset; only the visible rows are painted (the ~63
  // group columns all fit, so only the date axis needs windowing).
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const measure = () =>
      setViewport({
        scrollTop: node.scrollTop,
        width: node.clientWidth,
        height: node.clientHeight,
      });
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

  // Paint the visible rows × all group columns onto a viewport-sized canvas overlay (no full-content bitmap).
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = globalThis.devicePixelRatio ?? 1;
    const cssW = contentWidth; // the canvas is exactly the (few) columns wide; the body scrolls vertically
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

    for (let row = firstRow; row < lastRow; row++) {
      const coverageRow = matrix.coverage[row];
      const y = row * CELL.h - offY;
      for (let col = 0; col < nGroups; col++) {
        const fill = cellColor(coverageRow[col], matrix.group_trusted[col], trustOverlay);
        if (fill == null) continue;
        ctx.fillStyle = fill;
        ctx.fillRect(col * CELL.w, y, CELL.w - CELL.gap, CELL.h - CELL.gap);
      }
    }

    if (highlightCol != null) {
      const x = highlightCol * CELL.w;
      ctx.fillStyle = "rgba(108,182,255,0.10)";
      ctx.fillRect(x - 1, 0, CELL.w + 2, cssH);
      ctx.strokeStyle = COLORS.accent;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x - 0.5, 0, CELL.w + 1, cssH);
    }
  }, [matrix, trustOverlay, highlightCol, viewport, visibleRows, contentWidth, nGroups]);

  // Map a mouse position to a (row, col).
  const cellAt = useCallback(
    (clientX: number, clientY: number): { row: number; col: number } | null => {
      const node = scrollRef.current;
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      const localX = clientX - rect.left + node.scrollLeft;
      const localY = clientY - rect.top + node.scrollTop;
      const col = Math.floor(localX / CELL.w);
      const row = Math.floor(localY / CELL.h);
      if (col < 0 || col >= nGroups || row < 0 || row >= nDates) return null;
      return { row, col };
    },
    [nGroups, nDates],
  );

  const onMouseMove = useCallback(
    (event: React.MouseEvent) => {
      const hit = cellAt(event.clientX, event.clientY);
      if (!hit) {
        onHoverChange(null);
        return;
      }
      onHoverChange({ rowIndex: hit.row, colIndex: hit.col, clientX: event.clientX, clientY: event.clientY });
    },
    [cellAt, onHoverChange],
  );

  const onClick = useCallback(
    (event: React.MouseEvent) => {
      const hit = cellAt(event.clientX, event.clientY);
      if (hit) onPickCell(hit.row, hit.col);
    },
    [cellAt, onPickCell],
  );

  // Date labels for the visible rows only, positioned at their row's screen Y (content Y − scrollTop). Month
  // starts are bold; a sampled cadence fills between — a real time axis without a label per row.
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
      {/* Group-name column headers — angled so all ~63 fit. A fixed header row above the scrolling body. */}
      <div className="group-header" style={{ paddingLeft: 58 }}>
        <div className="group-header-inner" style={{ width: contentWidth }}>
          {matrix.groups.map((group, col) => (
            <div
              key={group}
              className={
                "group-label" +
                (matrix.group_trusted[col] ? " trusted" : "") +
                (highlightCol === col ? " active" : "")
              }
              style={{ left: col * CELL.w, width: CELL.w }}
              title={`${group}${matrix.group_trusted[col] ? " · trusted" : ""}`}
            >
              <span>{group}</span>
            </div>
          ))}
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
            onMouseLeave={() => onHoverChange(null)}
            onClick={onClick}
          >
            {/* Spacer establishes the full scrollable content height so the native scrollbar is correct. */}
            <div style={{ width: contentWidth, height: contentHeight }} />
          </div>
          {/* The canvas overlays the columns; it repaints the visible rows on vertical scroll. */}
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
