import { useEffect, useState } from "react";
import { BootingError, fetchCellDrill } from "./api";
import type { CellDrill } from "./types";

interface Props {
  group: string;
  date: string;
  trusted: boolean;
  onClose: () => void;
}

// The DRILL panel — a first-class NESTED view (Ben's explicit ask) for one (date × group) CELL: which tickers
// have that group's features on that date. It is unmistakably owned by the clicked cell: a labeled header CHIP
// naming the parent group + date + trust, a contained/shaded card, and the ticker list sits visibly INSIDE a
// further-indented nested card with its own header chip — the same treatment one level down.
export function DrillPanel({ group, date, trusted, onClose }: Props) {
  const [drill, setDrill] = useState<CellDrill | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [booting, setBooting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDrill(null);
    setError(null);
    setBooting(false);
    fetchCellDrill(group, date)
      .then((data) => {
        if (!cancelled) setDrill(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof BootingError) setBooting(true);
        else setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [group, date]);

  return (
    <aside className="drill-panel" aria-label={`drill-down for ${group} on ${date}`}>
      {/* Header chip: names the PARENT cell (group + date) this panel belongs to, so the owner is unmistakable. */}
      <div className="drill-header">
        <span className="drill-chip">
          <span className="drill-chip-kind">group</span>
          <span className="drill-chip-name">{group}</span>
        </span>
        <span className={trusted ? "trust-pill yes" : "trust-pill no"}>
          {trusted ? "trusted" : "untrusted"}
        </span>
        <span className="drill-header-meta">{date}</span>
        <button className="drill-close" onClick={onClose} aria-label="close drill">
          ×
        </button>
      </div>

      <div className="drill-body">
        {booting && <div className="drill-status">first build in progress — try again in a moment…</div>}
        {error && <div className="drill-status drill-error">failed to load: {error}</div>}
        {!booting && !error && !drill && <div className="drill-status">loading…</div>}
        {drill && (
          <>
            <div className="drill-summary">
              <span>
                <strong>{drill.n_tickers.toLocaleString()}</strong> of{" "}
                {drill.universe.toLocaleString()} captured tickers
              </span>
              <span className="drill-summary-pct">{drill.coverage_pct}% coverage</span>
            </div>

            {/* Nested card: the ticker list, contained + shaded + further-indented, with its own header chip. */}
            <div className="drill-nested">
              <div className="drill-nested-header">
                <span className="drill-chip drill-chip-sm">
                  <span className="drill-chip-kind">tickers</span>
                  <span className="drill-chip-name">{group}</span>
                </span>
                <span className="drill-header-meta">
                  {drill.n_tickers > drill.tickers.length
                    ? `showing first ${drill.tickers.length.toLocaleString()}`
                    : `${drill.tickers.length.toLocaleString()} names`}
                </span>
              </div>
              {drill.tickers.length === 0 ? (
                <div className="drill-status">no tickers have this group on this date.</div>
              ) : (
                <div className="drill-ticker-grid">
                  {drill.tickers.map((ticker) => (
                    <span key={ticker} className="drill-ticker-chip">
                      {ticker}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </aside>
  );
}
