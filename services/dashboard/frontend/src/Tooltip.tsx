import type { StoreGridMatrix } from "./types";
import type { HoverCell } from "./CanvasHeatmap";

interface Props {
  hover: HoverCell | null;
  matrix: StoreGridMatrix;
  trustOverlay: boolean;
}

// Floating tooltip pinned near the cursor, naming the hovered cell's group / date / coverage% / trust. Only
// shown over a covered cell (coverage byte > 0); empty cells get nothing.
export function Tooltip({ hover, matrix, trustOverlay }: Props) {
  if (!hover) return null;
  const coverageByte = matrix.coverage[hover.rowIndex]?.[hover.colIndex] ?? 0;
  if (coverageByte <= 0) return null;
  const trustedBit = matrix.group_trusted[hover.colIndex] ?? 0;
  const group = matrix.groups[hover.colIndex];
  const date = matrix.dates[hover.rowIndex];
  const universe = matrix.universe[hover.rowIndex] ?? 0;
  const coveragePct = Math.round((coverageByte / 255) * 100);
  const tickersCovered = Math.round((coverageByte / 255) * universe);

  // Keep the tooltip on-screen: flip to the left of the cursor near the right edge.
  const margin = 14;
  const flipLeft = hover.clientX > window.innerWidth - 240;
  const style: React.CSSProperties = {
    top: hover.clientY + margin,
    left: flipLeft ? undefined : hover.clientX + margin,
    right: flipLeft ? window.innerWidth - hover.clientX + margin : undefined,
  };

  return (
    <div className="tooltip" style={style}>
      <div className="tooltip-ticker">{group}</div>
      <div className="tooltip-row">
        <span className="tooltip-label">date</span>
        <span>{date}</span>
      </div>
      <div className="tooltip-row">
        <span className="tooltip-label">coverage</span>
        <span>
          {coveragePct}% &middot; {tickersCovered.toLocaleString()}/{universe.toLocaleString()} tickers
        </span>
      </div>
      <div className="tooltip-row">
        <span className="tooltip-label">trust</span>
        <span className={trustedBit ? "trust-yes" : "trust-no"}>
          {trustedBit ? "trusted group" : "untrusted group"}
        </span>
      </div>
      <div className="tooltip-hint">click for the per-ticker breakdown</div>
      {!trustOverlay && <div className="tooltip-hint">toggle trust overlay to colour by trust</div>}
    </div>
  );
}
