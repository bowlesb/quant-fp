import type { StoreGridMatrix } from "./types";
import type { DisplayColumn, HoverCell } from "./CanvasHeatmap";

interface Props {
  hover: HoverCell | null;
  matrix: StoreGridMatrix;
  displayCols: DisplayColumn[];
}

// Floating tooltip near the cursor naming the hovered cell's column / kind / date / coverage% / trust. Only
// shown over a covered cell (coverage byte > 0).
export function Tooltip({ hover, matrix, displayCols }: Props) {
  if (!hover) return null;
  const dc = displayCols[hover.displayCol];
  if (!dc) return null;
  const byte = matrix.coverage[hover.rowIndex]?.[dc.coverageCol] ?? 0;
  if (byte <= 0) return null;
  const date = matrix.dates[hover.rowIndex];
  const universe = matrix.universe_size;
  const coveragePct = Math.round((byte / 255) * 100);
  const tickers = Math.round((byte / 255) * universe);

  const kindLabel =
    dc.kind === "raw" ? "raw tape layer" : dc.kind === "feature" ? "feature" : "feature group";
  const trustLabel = dc.kind === "raw" ? null : dc.trusted ? "trusted" : "untrusted";

  const margin = 14;
  const flipLeft = hover.clientX > window.innerWidth - 250;
  const style: React.CSSProperties = {
    top: hover.clientY + margin,
    left: flipLeft ? undefined : hover.clientX + margin,
    right: flipLeft ? window.innerWidth - hover.clientX + margin : undefined,
  };

  return (
    <div className="tooltip" style={style}>
      <div className="tooltip-title">
        {dc.label}
        <span className="tooltip-kind">{kindLabel}</span>
      </div>
      <div className="tooltip-row">
        <span className="tooltip-label">date</span>
        <span>{date}</span>
      </div>
      <div className="tooltip-row">
        <span className="tooltip-label">coverage</span>
        <span>
          {coveragePct}% &middot; {tickers.toLocaleString()}/{universe.toLocaleString()} tickers
        </span>
      </div>
      {trustLabel && (
        <div className="tooltip-row">
          <span className="tooltip-label">trust</span>
          <span className={dc.trusted ? "trust-yes" : "trust-no"}>{trustLabel}</span>
        </div>
      )}
      {dc.kind === "group" && dc.expandable && (
        <div className="tooltip-hint">click the column to expand its features</div>
      )}
    </div>
  );
}
