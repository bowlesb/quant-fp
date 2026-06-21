import type { GridColumn, GroupInfo } from "./types";

interface Props {
  column: GridColumn;
  info: GroupInfo | undefined;
  onClose: () => void;
  // Hide the sidebar entirely (persisted for the session), giving the grid the full width.
  onHide: () => void;
}

// The "more detail" panel for one column (feature group or raw layer). Shows, from the matrix's baked-in
// group_info: WHAT it is (the group docstring / curated purpose), HOW its features differ (the per-feature
// description list, authored for every feature), and WHY we compute it (the curated guide value — or an honest
// "not yet written" stub when the guide has no entry, rather than fabricating a rationale).
export function GroupDetailPanel({ column, info, onClose, onHide }: Props) {
  const isRaw = column.kind === "raw";
  const trustLabel = isRaw ? "raw tape layer" : column.trusted ? "trusted" : "untrusted";
  const guide = info?.guide ?? null;
  const purpose = guide?.purpose?.trim();
  const docstring = info?.docstring?.trim();

  return (
    <aside className={"detail-panel" + (isRaw ? " raw" : column.trusted ? " trusted" : " untrusted")}>
      <div className="detail-header">
        <span className="detail-chip">
          <span className="detail-chip-kind">{isRaw ? "raw layer" : "feature group"}</span>
          <span className="detail-chip-name">{column.label}</span>
        </span>
        <span className={"detail-trust " + (isRaw ? "raw" : column.trusted ? "yes" : "no")}>{trustLabel}</span>
        <button className="detail-hide" onClick={onHide} aria-label="hide sidebar" title="Hide sidebar (Esc)">
          Hide ‹
        </button>
        <button className="detail-close" onClick={onClose} aria-label="close detail" title="Close">
          ×
        </button>
      </div>

      <div className="detail-body">
        {info && !isRaw && (
          <div className="detail-meta">
            {info.n_features} feature{info.n_features === 1 ? "" : "s"} · type {info.type || "—"} · layer{" "}
            {info.layer || "—"}
          </div>
        )}

        {/* WHAT it is */}
        <section className="detail-section">
          <h4>What it is</h4>
          <p>{purpose || docstring || "No description available."}</p>
          {purpose && docstring && docstring !== purpose && <p className="detail-doc">{docstring}</p>}
        </section>

        {/* WHY we compute it — curated, honest stub when absent */}
        {!isRaw && (
          <section className="detail-section">
            <h4>Why we compute it</h4>
            {guide?.value ? (
              <p>{guide.value}</p>
            ) : (
              <p className="detail-stub">
                Guide entry not yet written — we record what this group measures, not a claim that it
                predicts returns. (Curated narrative is seeded for a few groups and grown over time.)
              </p>
            )}
            {guide?.example && <pre className="detail-example">{guide.example}</pre>}
          </section>
        )}

        {/* HOW the features differ — the per-feature description list, authored for every feature */}
        {!isRaw && info && info.features.length > 0 && (
          <section className="detail-section">
            <h4>
              How the {info.features.length} features differ
              {guide?.how_features_differ ? "" : " (per-feature)"}
            </h4>
            {guide?.how_features_differ && <p className="detail-howdiff">{guide.how_features_differ}</p>}
            <ul className="detail-feature-list">
              {info.features.map((feature) => (
                <li key={feature.name}>
                  <code>{feature.name}</code>
                  <span>{feature.description || "—"}</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {!info && <p className="detail-stub">No detail recorded for this column.</p>}
      </div>
    </aside>
  );
}
