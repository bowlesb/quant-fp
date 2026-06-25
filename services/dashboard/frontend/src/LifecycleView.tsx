import { useEffect, useMemo, useState } from "react";
import { BootingError, fetchLifecycleState, fetchLifecycleTrend } from "./api";
import type {
  LifecycleGroup,
  LifecycleStage,
  LifecycleState,
  LifecycleTrend,
  LifecycleTrendDay,
} from "./types";

// The CERTIFICATION-LIFECYCLE tab (docs/WITHIN_DAY_PARITY_CERTIFICATION.md) — makes the now-running within-day
// parity lifecycle legible. Each feature-group's FURTHEST stage on the staged progression Ben described:
//
//   UNVERIFIED → MONITORING [who owns the lock] → CERTIFIED [stable_cycles / value_rate / cert_day] → TRUSTED
//
// read off within_day_assignment (the monitoring lock), within_day_parity_cert (the latest within-day intraday
// verdict), and feature_trust (the permanent binary grant). Polled (the monitor advances groups on a minutes
// cadence + the server caches the snapshot for ~20s), so a 30s poll tracks it cheaply.

const LIFECYCLE_POLL_MS = 30_000;

const STAGE_LABEL: Record<LifecycleStage, string> = {
  divergent: "Divergent",
  unverified: "Unverified",
  monitoring: "Monitoring",
  certified: "Certified (intraday)",
  trusted: "Trusted",
};

// A one-line "what this stage means" the header chips carry as a title (the staged story in plain words).
const STAGE_BLURB: Record<LifecycleStage, string> = {
  divergent: "a feature failed a clean-day parity check and nothing has lifted it since — needs a fix-it owner",
  unverified: "no owner, no within-day cert yet",
  monitoring: "a subagent owns the lock and is comparing live==backfill on the settled window",
  certified: "the within-day intraday window matched, held stable — pending a full clean day",
  trusted: "all the group's features earned the permanent binary trust grant",
};

function formatRate(rate: number | null): string {
  if (rate === null) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

function formatAgo(iso: string | null): string {
  if (!iso) return "—";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  const secs = Math.max(0, Math.round((Date.now() - when.getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  return `${hrs}h ago`;
}

// The four-step progress pipeline, rendered as a row of dots: each stage is filled up to the group's reached
// stage so the staged progression is visible at a glance. The certified/trusted distinction Ben cares about
// (intraday-ok vs pending-full-day vs trusted) is exactly this fill depth.
const STAGE_SEQUENCE: LifecycleStage[] = ["unverified", "monitoring", "certified", "trusted"];

function StagePipeline({ stage }: { stage: LifecycleStage }) {
  // DIVERGENT is not a progress step on the linear path — it is a broken state sitting at the unverified
  // depth. Render its first dot in a distinct "broken" style and light no further dots.
  const divergent = stage === "divergent";
  const reached = divergent ? 0 : STAGE_SEQUENCE.indexOf(stage);
  return (
    <div className="lc-pipeline" title={`${STAGE_LABEL[stage]} — ${STAGE_BLURB[stage]}`}>
      {STAGE_SEQUENCE.map((step, idx) => {
        const broken = divergent && idx === 0;
        const cls = broken ? "lc-dot lc-dot-divergent on" : `lc-dot lc-dot-${step}${idx <= reached ? " on" : ""}`;
        return <span key={step} className={cls} title={broken ? STAGE_LABEL.divergent : STAGE_LABEL[step]} />;
      })}
    </div>
  );
}

function GroupRow({ group }: { group: LifecycleGroup }) {
  const trustPct = group.n_features > 0 ? (100 * group.n_trusted) / group.n_features : 0;
  return (
    <tr className={`lc-row lc-row-${group.stage}`}>
      <td className="lc-cell-group">
        <span className={`lc-stage-badge lc-stage-${group.stage}`}>{STAGE_LABEL[group.stage]}</span>
        <span className="lc-group-name">{group.group}</span>
      </td>
      <td className="lc-cell-pipeline">
        <StagePipeline stage={group.stage} />
      </td>
      <td className="lc-cell-owner">
        {group.owner ? (
          <span className={`lc-owner lc-owner-${group.owner_status ?? "none"}`}>
            <span className="lc-owner-id">{group.owner}</span>
            <span className="lc-owner-status">
              {group.owner_stale ? "stale" : group.owner_status}
            </span>
          </span>
        ) : (
          <span className="lc-none">—</span>
        )}
      </td>
      <td className="lc-cell-cert">
        {group.cert_status ? (
          <span className={`lc-cert lc-cert-${group.cert_status}`}>
            <span className="lc-cert-status">{group.cert_status}</span>
            <span className="lc-cert-meta">
              {group.cert_stable_cycles ?? 0} cyc · {group.cert_window_minutes ?? 0}m ·{" "}
              {formatRate(group.cert_value_rate)}
              {group.cert_day ? ` · ${group.cert_day}` : ""}
            </span>
            {group.cert_reason && <span className="lc-cert-reason">{group.cert_reason}</span>}
          </span>
        ) : (
          <span className="lc-none">—</span>
        )}
      </td>
      <td className="lc-cell-trust">
        <div className="lc-trust-track" title={`${group.n_trusted}/${group.n_features} features trusted`}>
          <div className="lc-trust-fill" style={{ width: `${trustPct}%` }} />
          <span className="lc-trust-label">
            {group.n_trusted}/{group.n_features}
          </span>
        </div>
        {group.n_divergent > 0 && (
          <span
            className="lc-divergent-tag"
            title={`${group.n_divergent} of this group's features failed a clean-day parity check (broken, waiting on a fix)`}
          >
            {group.n_divergent} divergent
          </span>
        )}
      </td>
    </tr>
  );
}

const TREND_POLL_MS = 120_000;

function dayLabel(iso: string): string {
  // The day strings arrive as ISO dates (YYYY-MM-DD); show MM-DD compactly.
  return iso.length >= 10 ? iso.slice(5, 10) : iso;
}

// A compact per-day history strip: how trust ADVANCED over time (the snapshot above shows only where it
// stands today). Each day = a column with a cert-activity bar (certified within-day) and a trust-grant bar,
// each scaled to the busiest day in the window; the cumulative-trusted total rides above as a label so the
// advancing frontier reads at a glance. Pure presentation of the read-only /api/lifecycle-trend payload.
function LifecycleTrendStrip() {
  const [trend, setTrend] = useState<LifecycleTrend | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await fetchLifecycleTrend();
        if (!cancelled) {
          setTrend(payload);
          setError(null);
        }
      } catch (err) {
        if (cancelled) return;
        // A booting trust DB just hides the strip (the snapshot panel already shows the booting note).
        if (!(err instanceof BootingError)) setError(err instanceof Error ? err.message : String(err));
      }
    };
    load();
    const id = window.setInterval(load, TREND_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const days = useMemo<LifecycleTrendDay[]>(() => trend?.trend ?? [], [trend]);
  const maxCert = useMemo(() => Math.max(1, ...days.map((d) => d.certs_total)), [days]);
  const maxGrant = useMemo(() => Math.max(1, ...days.map((d) => d.trust_grants)), [days]);

  if (error) return null; // never break the page on the trend; the snapshot stands on its own
  if (!trend || days.length === 0) return null;

  return (
    <div className="lc-trend">
      <div className="lc-trend-head">
        <span className="lc-trend-title">Trust over time</span>
        <span className="lc-trend-legend">
          <span className="lc-trend-key lc-trend-key-cert" /> within-day certified
          <span className="lc-trend-key lc-trend-key-grant" /> trust grants
          <span className="lc-trend-cum">· {trend.trusted_now} trusted now</span>
        </span>
      </div>
      <div className="lc-trend-bars">
        {days.map((day) => {
          const certH = Math.round((100 * day.certs_certified) / maxCert);
          const grantH = Math.round((100 * day.trust_grants) / maxGrant);
          const title =
            `${day.day}: ${day.certs_certified}/${day.certs_total} certified across ${day.cert_groups} group(s); ` +
            `+${day.trust_grants} trust grants` +
            (day.untrust_events > 0 ? `, −${day.untrust_events} un-trusted` : "") +
            `; ${day.cumulative_trusted} cumulative`;
          return (
            <div key={day.day} className="lc-trend-col" title={title}>
              <span className="lc-trend-cum-label">{day.cumulative_trusted}</span>
              <div className="lc-trend-stack">
                <span className="lc-trend-bar lc-trend-bar-cert" style={{ height: `${certH}%` }} />
                <span className="lc-trend-bar lc-trend-bar-grant" style={{ height: `${grantH}%` }} />
              </div>
              {day.untrust_events > 0 && <span className="lc-trend-untrust">−{day.untrust_events}</span>}
              <span className="lc-trend-day">{dayLabel(day.day)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function LifecycleView() {
  const [data, setData] = useState<LifecycleState | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await fetchLifecycleState();
        if (cancelled) return;
        setData(payload);
        setBooting(false);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof BootingError) setBooting(true);
        else setError(err instanceof Error ? err.message : String(err));
      }
    };
    load();
    const id = window.setInterval(load, LIFECYCLE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  // Groups arrive stage-ordered (most work outstanding first) from the server; keep that order.
  const groups = useMemo<LifecycleGroup[]>(() => data?.groups ?? [], [data]);

  if (error) return <div className="banner-error">{error}</div>;
  if (booting && !data) {
    return (
      <div className="lc-note">
        The trust DB is unreachable (the dashboard is still booting). The lifecycle state will appear once it
        is available.
      </div>
    );
  }
  if (!data) return <div className="lc-note">Loading certification lifecycle…</div>;

  return (
    <div className="lc-view">
      <div className="lc-header">
        <div className="lc-stage-chips">
          {data.stage_order.map((stage) => (
            <div
              key={stage}
              className={`lc-chip lc-chip-${stage}`}
              title={STAGE_BLURB[stage]}
            >
              <span className="lc-chip-count">{data.summary[stage] ?? 0}</span>
              <span className="lc-chip-label">{STAGE_LABEL[stage]}</span>
            </div>
          ))}
        </div>
        <div className="lc-submeta">
          {data.n_groups} groups · {data.n_trusted_features}/{data.n_features} features trusted ·{" "}
          {data.n_divergent_features > 0 && (
            <span className="lc-submeta-divergent">{data.n_divergent_features} divergent · </span>
          )}
          {data.active_owners.length} active owner{data.active_owners.length === 1 ? "" : "s"} · as of{" "}
          {formatAgo(data.generated_at)}
        </div>
      </div>

      <LifecycleTrendStrip />

      {data.active_owners.length > 0 && (
        <div className="lc-owners-strip">
          <span className="lc-owners-title">Currently monitoring:</span>
          {data.active_owners.map((owner) => (
            <span key={owner.group_name} className="lc-owner-pill" title={`since ${owner.claimed_at}`}>
              <span className="lc-owner-pill-agent">{owner.agent_id}</span>
              <span className="lc-owner-pill-group">{owner.group_name}</span>
              <span className="lc-owner-pill-hb">♥ {formatAgo(owner.heartbeat_at)}</span>
            </span>
          ))}
        </div>
      )}

      <table className="lc-table">
        <thead>
          <tr>
            <th className="lc-th-group">Stage · group</th>
            <th className="lc-th-pipeline">Progression</th>
            <th className="lc-th-owner">Monitoring owner</th>
            <th className="lc-th-cert">Within-day cert (latest)</th>
            <th className="lc-th-trust">Features trusted</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <GroupRow key={group.group} group={group} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
