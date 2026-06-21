import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchStatusGrid, postReaction } from "./api";
import type { StatusGrid, StatusRow } from "./types";

// The HOURLY STATUS table (docs/OPERATING_MODEL.md §"The hourly status dashboard"). One ROW per hour (newest
// first) × the eight workstream COLUMNS; each cell a concise Progress + Blockers the Lead synthesizes from
// that workstream's ledger each cycle. Each row carries a Ben-REACTION input box: Ben types a reaction, it
// POSTs to the append-only store, and the Lead reviews it next cycle. The table is persisted server-side
// (~/.quant-ops/status_dashboard.json), so it survives a dashboard restart.
//
// Polled (the Lead writes a new row ~hourly + may re-synthesize the current hour); a 60s poll is plenty. A
// reaction the user is actively editing is never clobbered by a poll (the dirty row's input keeps its local
// text until saved).

const STATUS_POLL_MS = 60_000;

function formatHour(hour: string): string {
  // hour is "YYYY-MM-DDTHH:00Z". Render a compact local "Mon HH:00" plus the UTC label on hover.
  const when = new Date(hour);
  if (Number.isNaN(when.getTime())) return hour;
  const day = when.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${day} · ${time}`;
}

interface ReactionBoxProps {
  row: StatusRow;
  onSaved: (updated: StatusRow) => void;
}

// The per-row reaction input. Local `text` is the source of truth while editing (so a background poll can't
// stomp what Ben is typing); Save POSTs and lifts the server's authoritative row back up. "Saved" is shown
// briefly after a successful write.
function ReactionBox({ row, onSaved }: ReactionBoxProps) {
  const [text, setText] = useState(row.reaction);
  const [saving, setSaving] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Re-sync the box to the server's value ONLY when the row's stored reaction changes AND the user has not
  // started editing away from it (dirty). This lets a Lead-side edit or another tab's save propagate in,
  // without ever discarding the user's in-progress text.
  const lastServerReaction = useRef(row.reaction);
  useEffect(() => {
    if (row.reaction !== lastServerReaction.current) {
      lastServerReaction.current = row.reaction;
      setText((current) => (current === "" || current === row.reaction ? row.reaction : current));
    }
  }, [row.reaction]);

  const dirty = text.trim() !== row.reaction.trim();

  const save = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await postReaction(row.hour, text);
      lastServerReaction.current = updated.reaction;
      onSaved(updated);
      setSavedFlash(true);
      window.setTimeout(() => setSavedFlash(false), 1800);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [row.hour, text, onSaved]);

  return (
    <div className="status-reaction">
      <textarea
        className="status-reaction-input"
        placeholder="Ben: type a reaction the Lead reviews next cycle…"
        value={text}
        rows={2}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          // Cmd/Ctrl+Enter saves (a textarea swallows plain Enter for newlines).
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            if (dirty && !saving) save();
          }
        }}
      />
      <div className="status-reaction-foot">
        <button
          className="status-reaction-save"
          disabled={!dirty || saving}
          onClick={save}
          title="Save reaction (Cmd/Ctrl+Enter)"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {savedFlash && <span className="status-reaction-saved">saved</span>}
        {row.reaction_at && !dirty && (
          <span className="status-reaction-at" title={row.reaction_at}>
            reacted {row.reaction_at.slice(11, 16)}Z
          </span>
        )}
        {error && <span className="status-reaction-err">{error}</span>}
      </div>
    </div>
  );
}

export function StatusView() {
  const [grid, setGrid] = useState<StatusGrid | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchStatusGrid();
      setGrid(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, STATUS_POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  // When a reaction saves, splice the server's authoritative row into local state so the "reacted HH:MMZ"
  // stamp updates immediately without waiting for the next poll.
  const onReactionSaved = useCallback((updated: StatusRow) => {
    setGrid((current) => {
      if (!current) return current;
      const rows = current.rows.map((row) => (row.hour === updated.hour ? updated : row));
      return { ...current, rows };
    });
  }, []);

  const workstreams = useMemo(() => grid?.workstreams ?? [], [grid]);

  if (error) return <div className="banner-error">{error}</div>;
  if (!grid) return <div className="status-note">Loading hourly status…</div>;

  if (grid.rows.length === 0) {
    return (
      <div className="status-note">
        No status rows yet. The Lead's conductor loop writes one row per hour (synthesized from each
        workstream's ledger); the first will appear on the next cycle.
      </div>
    );
  }

  return (
    <div className="status-view">
      <div className="status-submeta">
        {grid.rows.length} hour{grid.rows.length === 1 ? "" : "s"} · {workstreams.length} workstreams ·
        each cell = Progress / Blockers the Lead synthesizes from that workstream's ledger · type a reaction
        per row — the Lead reviews it next cycle
      </div>
      <div className="status-table-scroll">
        <table className="status-table">
          <thead>
            <tr>
              <th className="status-hour-h">Hour</th>
              {workstreams.map((workstream) => (
                <th key={workstream} className="status-ws-h">
                  {workstream}
                </th>
              ))}
              <th className="status-reaction-h">Ben reaction</th>
            </tr>
          </thead>
          <tbody>
            {grid.rows.map((row) => (
              <tr key={row.hour}>
                <td className="status-hour" title={`${row.hour} (updated ${row.updated_at})`}>
                  {formatHour(row.hour)}
                </td>
                {workstreams.map((workstream) => {
                  const cell = row.cells[workstream] ?? { progress: "", blockers: "" };
                  const hasBlocker = cell.blockers.trim().length > 0;
                  return (
                    <td key={workstream} className={`status-cell${hasBlocker ? " has-blocker" : ""}`}>
                      {cell.progress ? (
                        <div className="status-progress">{cell.progress}</div>
                      ) : (
                        <div className="status-progress status-empty">—</div>
                      )}
                      {hasBlocker && (
                        <div className="status-blockers">
                          <span className="status-blocker-tag">blocker</span> {cell.blockers}
                        </div>
                      )}
                    </td>
                  );
                })}
                <td className="status-reaction-cell">
                  <ReactionBox row={row} onSaved={onReactionSaved} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
