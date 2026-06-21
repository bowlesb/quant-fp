import { useEffect, useMemo, useState } from "react";
import { BootingError, fetchLatencyExpectations } from "./api";
import type { LatencyExpectations, LatencyGroup } from "./types";

// The per-group feature-latency expectations view (docs/feature_latency_expectations.json, #321). The data is a
// living offline-generated artifact (a measurement loop refreshes it), not the always-warm worker cache, so a
// single fetch on mount is enough — no polling. The grid stays the dashboard's default view; this is a tab.
//
// The view is a HORIZONTAL BAR CHART, one row per group, SORTED SLOWEST-FIRST (slowest at top). The bar LENGTH
// encodes the typical per-minute cost (p50_ms); hovering a bar surfaces the TAIL (p95 + p99) plus the few extra
// facts that explain the cost — kind, mechanism, incremental_ready, feat_count. p95_ms is being added to the
// artifact; until it lands the hover shows p99 (always present) and omits p95 — the chart degrades gracefully.

function formatMs(value: number): string {
  // Sub-10ms groups read better with two decimals; everything else to one.
  return value < 10 ? value.toFixed(2) : value.toFixed(1);
}

// The bar fill darkens with slowness (anchored at the slowest p50 so it stays meaningful as numbers drift). A
// faster group is a pale blue bar; the slowest is the dashboard's deep "trusted" blue, matching the grid theme.
function barColor(p50: number, maxP50: number): string {
  const t = maxP50 > 0 ? Math.min(1, Math.max(0, p50 / maxP50)) : 0;
  const eased = Math.pow(t, 0.6); // lift thin bars so they stay visible
  // pale blue (#cfe0fb) -> deep trusted blue (#0b3d91)
  const red = Math.round(207 + (11 - 207) * eased);
  const green = Math.round(224 + (61 - 224) * eased);
  const blue = Math.round(251 + (145 - 251) * eased);
  return `rgb(${red},${green},${blue})`;
}

interface HoverState {
  group: LatencyGroup;
  x: number;
  y: number;
}

interface LatencyBarProps {
  group: LatencyGroup;
  maxP50: number;
  onHover: (state: HoverState | null) => void;
}

function LatencyBar({ group, maxP50, onHover }: LatencyBarProps) {
  const widthPct = maxP50 > 0 ? Math.max(0.5, (group.p50_ms / maxP50) * 100) : 0;
  const move = (event: React.MouseEvent) => onHover({ group, x: event.clientX, y: event.clientY });
  return (
    <div
      className="lat-bar-row"
      onMouseMove={move}
      onMouseEnter={move}
      onMouseLeave={() => onHover(null)}
    >
      <div className="lat-bar-label">{group.group}</div>
      <div className="lat-bar-track">
        <div
          className="lat-bar-fill"
          style={{ width: `${widthPct}%`, background: barColor(group.p50_ms, maxP50) }}
        />
        <span className="lat-bar-value">{formatMs(group.p50_ms)} ms</span>
      </div>
    </div>
  );
}

function LatencyTooltip({ hover }: { hover: HoverState }) {
  const { group } = hover;
  // Keep the tooltip on-screen: flip to the cursor's left near the right edge.
  const flipLeft = hover.x > window.innerWidth - 280;
  const style: React.CSSProperties = {
    top: hover.y + 14,
    left: flipLeft ? undefined : hover.x + 14,
    right: flipLeft ? window.innerWidth - hover.x + 14 : undefined,
  };
  return (
    <div className="lat-tooltip" style={style}>
      <div className="lat-tip-title">{group.group}</div>
      <div className="lat-tip-tail">
        <span className="lat-tip-stat">
          <span className="lat-tip-k">p50</span>
          <span className="lat-tip-v">{formatMs(group.p50_ms)} ms</span>
        </span>
        {group.p95_ms !== undefined && (
          <span className="lat-tip-stat">
            <span className="lat-tip-k">p95</span>
            <span className="lat-tip-v">{formatMs(group.p95_ms)} ms</span>
          </span>
        )}
        <span className="lat-tip-stat">
          <span className="lat-tip-k">p99</span>
          <span className="lat-tip-v lat-tip-tailv">{formatMs(group.p99_ms)} ms</span>
        </span>
      </div>
      <dl className="lat-tip-meta">
        <dt>kind</dt>
        <dd>{group.kind}</dd>
        <dt>mechanism</dt>
        <dd>{group.mechanism}</dd>
        <dt>incremental</dt>
        <dd className={`lat-incr lat-incr-${group.incremental_ready}`}>{group.incremental_ready}</dd>
        {group.path && (
          <>
            <dt>path</dt>
            <dd>{group.path}</dd>
          </>
        )}
        <dt>features</dt>
        <dd>{group.feat_count}</dd>
      </dl>
    </div>
  );
}

export function LatencyView() {
  const [data, setData] = useState<LatencyExpectations | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<HoverState | null>(null);

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

  // Sort slowest-first by the tail (p99) so "slowest at top" holds even if the artifact's order ever drifts;
  // the bar length still encodes p50. maxP50 anchors the bar-length + colour scale.
  const sorted = useMemo<LatencyGroup[]>(
    () => (data ? [...data.groups].sort((a, b) => b.p99_ms - a.p99_ms) : []),
    [data],
  );
  const maxP50 = useMemo(() => sorted.reduce((acc, group) => Math.max(acc, group.p50_ms), 0), [sorted]);

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

  return (
    <div className="lat-view" onMouseLeave={() => setHover(null)}>
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
      </div>

      <div className="lat-chart">
        {sorted.map((group) => (
          <LatencyBar key={group.group} group={group} maxP50={maxP50} onHover={setHover} />
        ))}
      </div>

      {hover && <LatencyTooltip hover={hover} />}
    </div>
  );
}
