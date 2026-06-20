import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { StoreGridMatrix } from "./types";
import { CELL, COLORS } from "./theme";

// Hovered cell identity passed up for the tooltip.
export interface HoverCell {
  rowIndex: number; // date index
  colIndex: number; // ticker index
  clientX: number;
  clientY: number;
}

interface Props {
  matrix: StoreGridMatrix;
  trustOverlay: boolean;
  // The ticker column to highlight (search jump / active drill), or null.
  highlightCol: number | null;
  onHoverChange: (cell: HoverCell | null) => void;
  onPickTicker: (colIndex: number) => void;
  // Imperative scroll target: when this changes the heatmap scrolls that column into view (search jump).
  scrollToCol: number | null;
}

// Parse "#rrggbb" once into [r,g,b] so the per-cell paint is integer math, not string work.
function hexToRgb(hex: string): [number, number, number] {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

const COVERAGE_RGB = hexToRgb(COLORS.coverageHue);
const TRUSTED_RGB = hexToRgb(COLORS.trusted);
const UNTRUSTED_RGB = hexToRgb(COLORS.untrusted);

// Cell fill for a coverage byte (0..255) + trust bit, as an rgba string. Coverage maps to ALPHA (brightness
// on the dark bg) so light = sparse, opaque = full. Hue is the trust colour when the overlay is on, else the
// neutral coverage blue. Byte 0 (absent) returns null = paint nothing (bg shows through).
function cellFill(coverageByte: number, trustedBit: number, overlay: boolean): string | null {
  if (coverageByte <= 0) return null;
  const alpha = 0.12 + 0.88 * (coverageByte / 255); // floor so even thin coverage is visible
  let rgb = COVERAGE_RGB;
  if (overlay) {
    rgb = trustedBit ? TRUSTED_RGB : UNTRUSTED_RGB;
  }
  return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha.toFixed(3)})`;
}

export function CanvasHeatmap({
  matrix,
  trustOverlay,
  highlightCol,
  onHoverChange,
  onPickTicker,
  scrollToCol,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [viewport, setViewport] = useState({ scrollLeft: 0, scrollTop: 0, width: 0, height: 0 });

  const nDates = matrix.dates.length;
  const nTickers = matrix.tickers.length;
  const contentWidth = nTickers * CELL.w;
  const contentHeight = nDates * CELL.h;

  // Track the scroll container's size + scroll offsets; only the visible window of columns/rows is painted.
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const measure = () =>
      setViewport({
        scrollLeft: node.scrollLeft,
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

  // Honor an imperative scroll-to-column request (search jump): centre the column horizontally.
  useEffect(() => {
    if (scrollToCol == null || !scrollRef.current) return;
    const node = scrollRef.current;
    const target = scrollToCol * CELL.w - node.clientWidth / 2;
    node.scrollTo({ left: Math.max(0, target), behavior: "smooth" });
  }, [scrollToCol]);

  // The visible column/row window (+ a small overscan) — virtualization keeps 11.4k columns smooth.
  const visibleWindow = useMemo(() => {
    const overscan = 4;
    const firstCol = Math.max(0, Math.floor(viewport.scrollLeft / CELL.w) - overscan);
    const lastCol = Math.min(
      nTickers,
      Math.ceil((viewport.scrollLeft + viewport.width) / CELL.w) + overscan,
    );
    const firstRow = Math.max(0, Math.floor(viewport.scrollTop / CELL.h) - overscan);
    const lastRow = Math.min(
      nDates,
      Math.ceil((viewport.scrollTop + viewport.height) / CELL.h) + overscan,
    );
    return { firstCol, lastCol, firstRow, lastRow };
  }, [viewport, nTickers, nDates]);

  // Paint the visible window onto a viewport-sized canvas (NOT the full 2.8M-cell content — that bitmap would
  // be enormous). The canvas overlays the viewport; each visible cell is drawn at its content position minus
  // the scroll offset.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = globalThis.devicePixelRatio ?? 1;
    const cssW = viewport.width;
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

    const { firstCol, lastCol, firstRow, lastRow } = visibleWindow;
    const offX = viewport.scrollLeft;
    const offY = viewport.scrollTop;

    for (let row = firstRow; row < lastRow; row++) {
      const coverageRow = matrix.coverage[row];
      const trustedRow = matrix.trusted[row];
      const y = row * CELL.h - offY;
      for (let col = firstCol; col < lastCol; col++) {
        const fill = cellFill(coverageRow[col], trustedRow[col], trustOverlay);
        if (fill == null) continue;
        ctx.fillStyle = fill;
        ctx.fillRect(col * CELL.w - offX, y, CELL.w - CELL.gap, CELL.h - CELL.gap);
      }
    }

    if (highlightCol != null) {
      const x = highlightCol * CELL.w - offX;
      if (x >= -CELL.w && x <= cssW) {
        ctx.strokeStyle = COLORS.link;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x - 0.5, 0, CELL.w + 1, cssH);
      }
    }
  }, [matrix, trustOverlay, highlightCol, viewport, visibleWindow]);

  // Map a mouse position to a (row, col); used for hover tooltip + click-to-drill.
  const cellAt = useCallback(
    (clientX: number, clientY: number): { row: number; col: number } | null => {
      const node = scrollRef.current;
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      const localX = clientX - rect.left + node.scrollLeft;
      const localY = clientY - rect.top + node.scrollTop;
      const col = Math.floor(localX / CELL.w);
      const row = Math.floor(localY / CELL.h);
      if (col < 0 || col >= nTickers || row < 0 || row >= nDates) return null;
      return { row, col };
    },
    [nTickers, nDates],
  );

  const onMouseMove = useCallback(
    (event: React.MouseEvent) => {
      const hit = cellAt(event.clientX, event.clientY);
      if (!hit) {
        onHoverChange(null);
        return;
      }
      onHoverChange({
        rowIndex: hit.row,
        colIndex: hit.col,
        clientX: event.clientX,
        clientY: event.clientY,
      });
    },
    [cellAt, onHoverChange],
  );

  const onClick = useCallback(
    (event: React.MouseEvent) => {
      const hit = cellAt(event.clientX, event.clientY);
      if (hit) onPickTicker(hit.col);
    },
    [cellAt, onPickTicker],
  );

  return (
    <div className="heatmap-frame">
      <div
        ref={scrollRef}
        className="heatmap-scroll"
        onMouseMove={onMouseMove}
        onMouseLeave={() => onHoverChange(null)}
        onClick={onClick}
      >
        {/* Spacer establishes the full scrollable content size so native scrollbars are correct. */}
        <div style={{ width: contentWidth, height: contentHeight }} />
      </div>
      {/* The canvas overlays the viewport (it does not scroll); it repaints the visible window on scroll. */}
      <canvas ref={canvasRef} className="heatmap-canvas" style={{ width: viewport.width, height: viewport.height }} />
    </div>
  );
}
