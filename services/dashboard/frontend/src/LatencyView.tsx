import { useEffect, useState } from "react";
import { BootingError, fetchLatencyExpectations } from "./api";
import type { LatencyExpectations, LatencyGroup } from "./types";

// The per-group feature-latency expectations view (docs/feature_latency_expectations.json, #321). The data is a
// static offline-generated artifact (not the always-warm worker cache), so a single fetch on mount is enough —
// no polling. The grid stays the dashboard's default view; this is an additional, clearly-linked page.

function formatMs(value: number): string {
  // Sub-ms groups read better with two decimals; everything else to one.
  return value < 10 ? value.toFixed(2) : value.toFixed(1);
}

// Tint the p99 cell from white (fast) toward a warm red (slowest) so the slowest rows pop. The scale is anchored
// at the table's own max p99 so it stays meaningful as the numbers drift across regenerations.
function p99Background(p99: number, maxP99: number): string {
  if (maxP99 <= 0) return "transparent";
  const t = Math.min(1, Math.max(0, p99 / maxP99));
  // White -> the dashboard's untrusted-red (#a01722), gentle gamma so mid values stay legible.
  const eased = Math.pow(t, 0.85);
  const red = Math.round(255 + (160 - 255) * eased);
  const green = Math.round(255 + (23 - 255) * eased);
  const blue = Math.round(255 + (34 - 255) * eased);
  return `rgb(${red},${green},${blue})`;
}

function p99TextColor(p99: number, maxP99: number): string {
  // Flip to white text once the cell background is dark enough to swallow dark text.
  return maxP99 > 0 && p99 / maxP99 > 0.6 ? "#ffffff" : "var(--text)";
}

interface LatencyRowProps {
  group: LatencyGroup;
  maxP99: number;
}

function LatencyRow({ group, maxP99 }: LatencyRowProps) {
  return (
    <tr>
      <td className="lat-group">{group.group}</td>
      <td
        className="lat-num lat-p99"
        style={{ background: p99Background(group.p99_ms, maxP99), color: p99TextColor(group.p99_ms, maxP99) }}
      >
        {formatMs(group.p99_ms)}
      </td>
      <td className="lat-num">{formatMs(group.p50_ms)}</td>
      <td>{group.kind}</td>
      <td className="lat-mech">{group.mechanism}</td>
      <td className={`lat-incr lat-incr-${group.incremental_ready}`}>{group.incremental_ready}</td>
      <td className="lat-num">{group.feat_count}</td>
    </tr>
  );
}

export function LatencyView() {
  const [data, setData] = useState<LatencyExpectations | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchLatencyExpectations()
      .then((payload) => {
        if (!cancelled) setData(payload);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof BootingError) setBooting(true);
        else setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="banner-error">{error}</div>;
  if (booting && !data) {
    return (
      <div className="lat-note">
        Latency expectations artifact not present yet (the dashboard is still booting).
      </div>
    );
  }
  if (!data) return <div className="lat-note">Loading latency expectations…</div>;

  const ctx = data.e2e_context;
  // The JSON is already sorted slowest-first; recompute the max defensively for the colour scale.
  const maxP99 = data.groups.reduce((acc, group) => Math.max(acc, group.p99_ms), 0);

  return (
    <div className="lat-view">
      <div className="lat-header">
        <div className="lat-headline">
          <span className="lat-metric">bar &rarr; vector (per-bet)</span>
          <span className="lat-stat">
            <span className="lat-stat-val">{ctx.single_bet_isolated_p50_ms}</span> ms
            <span className="lat-stat-lbl">isolated p50</span>
          </span>
          <span className="lat-stat">
            <span className="lat-stat-val">{ctx.typical_bet_under_load_p50_ms}</span> ms
            <span className="lat-stat-lbl">under load p50</span>
          </span>
          <span className="lat-stat">
            <span className="lat-stat-val lat-target">&lt;{ctx.target_p99_ms}</span> ms
            <span className="lat-stat-lbl">target p99</span>
          </span>
        </div>
        <div className="lat-submeta">
          {data.group_count} groups · {data.feature_count} features · units: {data.units} ·{" "}
          {data.sorted_by} · generated {data.generated_at}
        </div>
        <div className="lat-note">{ctx.note}</div>
      </div>

      <div className="lat-table-wrap">
        <table className="lat-table">
          <thead>
            <tr>
              <th>group</th>
              <th className="lat-num">p99 (ms)</th>
              <th className="lat-num">p50 (ms)</th>
              <th>kind</th>
              <th>mechanism</th>
              <th>incremental</th>
              <th className="lat-num">feats</th>
            </tr>
          </thead>
          <tbody>
            {data.groups.map((group) => (
              <LatencyRow key={group.group} group={group} maxP99={maxP99} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
