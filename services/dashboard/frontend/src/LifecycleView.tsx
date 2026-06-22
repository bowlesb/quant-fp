import { useEffect, useMemo, useState } from "react";
import { BootingError, fetchLifecycleState } from "./api";
import type { LifecycleGroup, LifecycleStage, LifecycleState } from "./types";

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
  unverified: "Unverified",
  monitoring: "Monitoring",
  certified: "Certified (intraday)",
  trusted: "Trusted",
};

// A one-line "what this stage means" the header chips carry as a title (the staged story in plain words).
const STAGE_BLURB: Record<LifecycleStage, string> = {
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
  const reached = STAGE_SEQUENCE.indexOf(stage);
  return (
    <div className="lc-pipeline" title={`${STAGE_LABEL[stage]} — ${STAGE_BLURB[stage]}`}>
      {STAGE_SEQUENCE.map((step, idx) => (
        <span
          key={step}
          className={`lc-dot lc-dot-${step}${idx <= reached ? " on" : ""}`}
          title={STAGE_LABEL[step]}
        />
      ))}
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
      </td>
    </tr>
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
          {data.active_owners.length} active owner{data.active_owners.length === 1 ? "" : "s"} · as of{" "}
          {formatAgo(data.generated_at)}
        </div>
      </div>

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
