import { useEffect, useState } from "react";
import { BootingError, fetchTickerDrill } from "./api";
import type { TickerDrill } from "./types";

interface Props {
  symbol: string;
  coveragePct: number | null;
  onClose: () => void;
}

// The DRILL panel — a first-class NESTED view (Ben's explicit ask). It is unmistakably owned by the clicked
// ticker: a labeled header CHIP naming the parent + group count, the whole panel is an indented/contained
// card with a distinct background shade, and the child rows (per group) are tighter than the parent grid.
// The same nesting treatment is applied to the deeper per-group date strip so every level reads as "contained
// by the thing above it".
export function DrillPanel({ symbol, coveragePct, onClose }: Props) {
  const [drill, setDrill] = useState<TickerDrill | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [booting, setBooting] = useState(false);
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDrill(null);
    setError(null);
    setBooting(false);
    setExpandedGroup(null);
    fetchTickerDrill(symbol)
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
  }, [symbol]);

  const nGroups = drill?.groups.length ?? 0;
  const nTrusted = drill?.groups.filter((group) => group.trusted).length ?? 0;

  return (
    <aside className="drill-panel" aria-label={`drill-down for ${symbol}`}>
      {/* Header chip: names the PARENT this panel belongs to + a count, so the nesting owner is unmistakable. */}
      <div className="drill-header">
        <span className="drill-chip">
          <span className="drill-chip-kind">ticker</span>
          <span className="drill-chip-name">{symbol}</span>
        </span>
        <span className="drill-header-meta">
          {nGroups} groups present &middot; {nTrusted} trusted
          {coveragePct != null && <> &middot; {coveragePct}% mean coverage</>}
        </span>
        <button className="drill-close" onClick={onClose} aria-label="close drill">
          ×
        </button>
      </div>

      <div className="drill-body">
        {booting && <div className="drill-status">first build in progress — try again in a moment…</div>}
        {error && <div className="drill-status drill-error">failed to load: {error}</div>}
        {!booting && !error && !drill && <div className="drill-status">loading {symbol}…</div>}
        {drill && drill.groups.length === 0 && (
          <div className="drill-status">no feature groups cover {symbol} in the window.</div>
        )}
        {drill && drill.groups.length > 0 && (
          <ul className="drill-group-list">
            {drill.groups.map((group) => {
              const isOpen = expandedGroup === group.group;
              const presentDates = drill.cells
                ? drill.dates.filter((date) => drill.cells[date]?.[group.group])
                : [];
              return (
                <li key={group.group} className="drill-group-row">
                  <button
                    className="drill-group-head"
                    onClick={() => setExpandedGroup(isOpen ? null : group.group)}
                    aria-expanded={isOpen}
                  >
                    <span className="drill-caret">{isOpen ? "▾" : "▸"}</span>
                    <span className="drill-group-name">{group.group}</span>
                    <span className={group.trusted ? "trust-pill yes" : "trust-pill no"}>
                      {group.trusted ? "trusted" : "untrusted"}
                    </span>
                    <span className="drill-group-count">{presentDates.length} dates</span>
                  </button>
                  {isOpen && (
                    // Deeper NEST: the per-group date strip is itself a contained, further-indented card with
                    // a header chip naming ITS parent (the group) — the same treatment, one level down.
                    <div className="drill-nested">
                      <div className="drill-nested-header">
                        <span className="drill-chip drill-chip-sm">
                          <span className="drill-chip-kind">group</span>
                          <span className="drill-chip-name">{group.group}</span>
                        </span>
                        <span className="drill-header-meta">present on {presentDates.length} dates</span>
                      </div>
                      <div className="drill-date-strip">
                        {presentDates.length === 0 ? (
                          <span className="drill-status">not present in the window</span>
                        ) : (
                          presentDates.map((date) => (
                            <span key={date} className="drill-date-chip">
                              {date}
                            </span>
                          ))
                        )}
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
}
