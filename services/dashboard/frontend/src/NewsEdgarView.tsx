import { useEffect, useMemo, useState } from "react";
import { BootingError, fetchNewsEdgarComposition, fetchNewsEdgarStream } from "./api";
import type {
  FeatureStatus,
  FilingsComposition,
  FreshnessStatus,
  NewsComposition,
  NewsEdgarComposition,
  NewsEdgarStream,
  StreamRate,
  TimelinePoint,
} from "./types";

// The News & Filings tab (the 3rd top-level view alongside the coverage grid + latency). Two panels:
//   1. LIVE STREAMING — current articles/min + filings/min, a recent per-minute sparkline, and the
//      ACTIVE/WARN/STALE freshness status per source (business-hours-aware, matching the data_freshness alert).
//      The stream rate is polled (it moves); the composition below is a slow store snapshot fetched once.
//   2. STORE COMPOSITION — total articles + span + top symbols (news), total filings + span + per-form-type
//      breakdown (EDGAR), and the FEATURE status (edgar_filing_frequency LIVE; news sentiment/hotness COMING,
//      with a left-open slot for the sentiment summary once NewsSentiment lands it).

const STREAM_POLL_MS = 30_000;

const FRESHNESS_LABEL: Record<string, string> = {
  OK: "ACTIVE",
  WARN: "LAGGING",
  STALE: "STALE",
  INACTIVE: "QUIET",
  ERROR: "ERROR",
};

function freshnessClass(status: string): string {
  // OK→active green, WARN→amber, STALE/ERROR→red, INACTIVE→neutral (expected lull, not a failure).
  return `ne-fresh ne-fresh-${status.toLowerCase()}`;
}

function isError(block: StreamRate | { error: string }): block is { error: string } {
  return "error" in block && block.error !== undefined;
}

function formatCount(value: number): string {
  return value.toLocaleString();
}

function formatRate(perMin: number): string {
  // Live feeds are bursty and often sub-1/min; show two decimals so a slow trickle is still legible.
  return perMin >= 10 ? perMin.toFixed(0) : perMin.toFixed(2);
}

function formatAge(minutes: number | null): string {
  if (minutes === null) return "—";
  if (minutes < 60) return `${minutes.toFixed(0)}m ago`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)}h ago`;
  return `${(hours / 24).toFixed(1)}d ago`;
}

function FreshnessBadge({ freshness }: { freshness: FreshnessStatus }) {
  const label = FRESHNESS_LABEL[freshness.status] ?? freshness.status;
  return (
    <span className={freshnessClass(freshness.status)} title={freshness.detail}>
      <span className="ne-fresh-dot" />
      {label}
    </span>
  );
}

// A compact per-minute sparkline of recent arrivals (bar heights normalized to the window max). A flat empty
// window (overnight/weekend lull) shows the baseline row, not an error.
function Sparkline({ timeline }: { timeline: TimelinePoint[] }) {
  const max = useMemo(() => timeline.reduce((acc, point) => Math.max(acc, point.count), 0), [timeline]);
  if (timeline.length === 0) {
    return <div className="ne-spark ne-spark-empty">no arrivals in the recent window</div>;
  }
  return (
    <div className="ne-spark" title={`${timeline.length} active minutes in the recent window`}>
      {timeline.map((point) => {
        const heightPct = max > 0 ? Math.max(6, (point.count / max) * 100) : 6;
        return (
          <span
            key={point.minute}
            className="ne-spark-bar"
            style={{ height: `${heightPct}%` }}
            title={`${new Date(point.minute).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${point.count}`}
          />
        );
      })}
    </div>
  );
}

function StreamCard({ title, unit, block }: { title: string; unit: string; block: StreamRate | { error: string } }) {
  if (isError(block)) {
    return (
      <div className="ne-stream-card">
        <div className="ne-stream-head">
          <span className="ne-stream-title">{title}</span>
        </div>
        <div className="ne-stream-err">{block.error}</div>
      </div>
    );
  }
  return (
    <div className="ne-stream-card">
      <div className="ne-stream-head">
        <span className="ne-stream-title">{title}</span>
        <FreshnessBadge freshness={block.freshness} />
      </div>
      <div className="ne-stream-rate">
        <span className="ne-rate-val">{formatRate(block.per_min)}</span>
        <span className="ne-rate-unit">{unit}/min</span>
      </div>
      <div className="ne-stream-sub">
        {formatCount(block.window_count)} in the last {block.window_minutes}m · newest{" "}
        {formatAge(block.freshness.age_minutes)}
      </div>
      <Sparkline timeline={block.timeline} />
    </div>
  );
}

function dateSpan(earliest: string | null, latest: string | null): string {
  if (!earliest || !latest) return "no data";
  const short = (iso: string) => iso.slice(0, 10);
  return `${short(earliest)} → ${short(latest)}`;
}

function NewsPanel({ news }: { news: NewsComposition }) {
  const max = useMemo(
    () => news.top_symbols.reduce((acc, item) => Math.max(acc, item.count), 0),
    [news.top_symbols],
  );
  return (
    <div className="ne-comp-card">
      <div className="ne-comp-title">News store</div>
      <div className="ne-stat-row">
        <span className="ne-stat">
          <span className="ne-stat-val">{formatCount(news.total_articles)}</span>
          <span className="ne-stat-lbl">articles</span>
        </span>
        <span className="ne-stat">
          <span className="ne-stat-val">{formatCount(news.n_symbols)}</span>
          <span className="ne-stat-lbl">symbols</span>
        </span>
        <span className="ne-stat">
          <span className="ne-stat-val ne-stat-span">{dateSpan(news.earliest_date, news.latest_date)}</span>
          <span className="ne-stat-lbl">span (UTC dates)</span>
        </span>
      </div>
      <div className="ne-bars-label">Top symbols by article mentions</div>
      <div className="ne-bars">
        {news.top_symbols.map((item) => (
          <div className="ne-bar-row" key={item.symbol}>
            <span className="ne-bar-name">{item.symbol}</span>
            <span className="ne-bar-track">
              <span
                className="ne-bar-fill ne-bar-news"
                style={{ width: `${max > 0 ? Math.max(1, (item.count / max) * 100) : 0}%` }}
              />
            </span>
            <span className="ne-bar-val">{formatCount(item.count)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function FilingsPanel({ filings }: { filings: FilingsComposition }) {
  const max = useMemo(
    () => filings.form_types?.reduce((acc, item) => Math.max(acc, item.count), 0) ?? 0,
    [filings.form_types],
  );
  if (filings.error) {
    return (
      <div className="ne-comp-card">
        <div className="ne-comp-title">EDGAR filings store</div>
        <div className="ne-stream-err">{filings.error}</div>
      </div>
    );
  }
  return (
    <div className="ne-comp-card">
      <div className="ne-comp-title">EDGAR filings store</div>
      <div className="ne-stat-row">
        <span className="ne-stat">
          <span className="ne-stat-val">{formatCount(filings.total_filings)}</span>
          <span className="ne-stat-lbl">filings</span>
        </span>
        <span className="ne-stat">
          <span className="ne-stat-val">{formatCount(filings.stream_filings)}</span>
          <span className="ne-stat-lbl">live-stream</span>
        </span>
        <span className="ne-stat">
          <span className="ne-stat-val ne-stat-span">
            {dateSpan(filings.earliest_available_at, filings.latest_available_at)}
          </span>
          <span className="ne-stat-lbl">span (available_at)</span>
        </span>
      </div>
      <div className="ne-bars-label">Filings by form type</div>
      <div className="ne-bars">
        {filings.form_types.map((item) => (
          <div className="ne-bar-row" key={item.form_type}>
            <span className="ne-bar-name">{item.form_type}</span>
            <span className="ne-bar-track">
              <span
                className="ne-bar-fill ne-bar-filing"
                style={{ width: `${max > 0 ? Math.max(1, (item.count / max) * 100) : 0}%` }}
              />
            </span>
            <span className="ne-bar-val">{formatCount(item.count)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function FeaturePanel({ features }: { features: FeatureStatus[] }) {
  return (
    <div className="ne-comp-card ne-feat-card">
      <div className="ne-comp-title">Features off these tapes</div>
      <div className="ne-feat-list">
        {features.map((feature) => (
          <div className="ne-feat-row" key={feature.label}>
            <span className={`ne-feat-status ne-feat-${feature.status.toLowerCase()}`}>{feature.status}</span>
            <span className="ne-feat-body">
              <span className="ne-feat-name">{feature.label}</span>
              <span className="ne-feat-detail">{feature.detail}</span>
            </span>
            <span className="ne-feat-src">{feature.source}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function NewsEdgarView() {
  const [stream, setStream] = useState<NewsEdgarStream | null>(null);
  const [composition, setComposition] = useState<NewsEdgarComposition | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Poll the live rate; refresh the slow composition snapshot on mount (it is TTL-cached server-side).
  useEffect(() => {
    let cancelled = false;
    const pollStream = async () => {
      try {
        const payload = await fetchNewsEdgarStream();
        if (!cancelled) {
          setStream(payload);
          setError(null);
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof BootingError) setBooting(true);
        else setError(err instanceof Error ? err.message : String(err));
      }
    };
    pollStream();
    const id = window.setInterval(pollStream, STREAM_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchNewsEdgarComposition()
      .then((payload) => {
        if (!cancelled) setComposition(payload);
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
  if (booting && !stream && !composition) {
    return <div className="lat-note">News &amp; Filings data not available yet (the dashboard is booting).</div>;
  }

  return (
    <div className="ne-view">
      <section className="ne-section">
        <div className="ne-section-head">
          <h2 className="ne-section-title">Streaming right now</h2>
          {stream && (
            <span className="ne-section-asof">
              as of{" "}
              {new Date(stream.generated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
        </div>
        {stream ? (
          <div className="ne-stream-grid">
            <StreamCard title="Alpaca news" unit="articles" block={stream.news} />
            <StreamCard title="SEC EDGAR filings" unit="filings" block={stream.edgar} />
          </div>
        ) : (
          <div className="lat-note">Loading live rate…</div>
        )}
      </section>

      <section className="ne-section">
        <div className="ne-section-head">
          <h2 className="ne-section-title">Store composition</h2>
          {composition && (
            <span className="ne-section-asof">
              {composition.cached
                ? `cached ${composition.cache_age_seconds.toFixed(0)}s ago`
                : "fresh"}
            </span>
          )}
        </div>
        {composition ? (
          <>
            <div className="ne-comp-grid">
              <NewsPanel news={composition.news} />
              <FilingsPanel filings={composition.filings} />
            </div>
            <FeaturePanel features={composition.features} />
          </>
        ) : (
          <div className="lat-note">Loading store composition…</div>
        )}
      </section>
    </div>
  );
}
