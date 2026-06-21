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

  // Stream-vs-backfill provenance: coverage_source carries, per feature cell, the STREAM fraction of its
  // covered tickers (-1 = SOURCE_NA: raw layers / absent cells). Split back to a stream / backfill-only
  // approximate ticker count against this cell's own ticker total — surfaces the FP_TICK_SYMBOLS live gap.
  const sourceByte = matrix.coverage_source[hover.rowIndex]?.[dc.coverageCol] ?? -1;
  const hasSource = sourceByte >= 0 && tickers > 0;
  const streamPct = hasSource ? Math.round((sourceByte / 255) * 100) : 0;
  const nStream = hasSource ? Math.round((sourceByte / 255) * tickers) : 0;
  const nBackfillOnly = hasSource ? Math.max(0, tickers - nStream) : 0;

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
      {hasSource && (
        <div className="tooltip-row">
          <span className="tooltip-label">source</span>
          <span>
            <span className="src-stream">{nStream.toLocaleString()} live</span>
            {nBackfillOnly > 0 && (
              <>
                {" "}
                &middot; <span className="src-backfill">{nBackfillOnly.toLocaleString()} backfill-only</span>
              </>
            )}{" "}
            <span className="src-pct">({streamPct}% live)</span>
          </span>
        </div>
      )}
      {trustLabel && (
        <div className="tooltip-row">
          <span className="tooltip-label">trust</span>
          <span className={dc.trusted ? "trust-yes" : "trust-no"}>{trustLabel}</span>
        </div>
      )}
      {dc.kind === "group" && dc.expandable && (
        <div className="tooltip-hint">click a cell to expand its features</div>
      )}
      {dc.kind !== "raw" && (
        <div className="tooltip-more">
          press <kbd>K</kbd> (or click the header) for <strong>more detail ▸</strong>
        </div>
      )}
    </div>
  );
}
