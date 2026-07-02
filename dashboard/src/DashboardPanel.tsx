import { useCallback, useEffect, useRef, useState } from "react";
import { fetchDashboardSummary, retryAllJobs } from "./api";
import type {
  DashboardHourEntry,
  DashboardJobStatusCount,
  DashboardSessionActivity,
  DashboardSummary,
  JobStatus,
} from "./types";

type RefreshState = "idle" | "loading" | "ready" | "error";

const STATUS_COLORS: Record<JobStatus, string> = {
  PENDING: "#4ea1ff",
  PROCESSING: "#ffd05c",
  COMPLETED: "#7cf6a8",
  FAILED: "#ff8e9e",
  QUOTA_LOCKED: "#c89bff",
};

const STATUS_ORDER: JobStatus[] = [
  "PENDING",
  "PROCESSING",
  "COMPLETED",
  "FAILED",
  "QUOTA_LOCKED",
];

export default function DashboardPanel() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [refreshState, setRefreshState] = useState<RefreshState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [retryMessage, setRetryMessage] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    setRefreshState((current) => (current === "ready" ? "ready" : "loading"));
    try {
      const payload = await fetchDashboardSummary();
      setSummary(payload);
      setRefreshState("ready");
      setError(null);
      setLastRefreshedAt(new Date().toISOString());
    } catch (caught) {
      setRefreshState("error");
      setError(caught instanceof Error ? caught.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    timerRef.current = window.setInterval(() => {
      void refresh();
    }, 15_000);
    return () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [refresh]);

  async function handleRetryAll() {
    setRetrying(true);
    try {
      const result = await retryAllJobs();
      setRetryMessage(`Retry queued for ${result.reset_count} stopped jobs`);
      await refresh();
    } catch (caught) {
      setRetryMessage(
        caught instanceof Error ? caught.message : "Retry failed",
      );
    } finally {
      setRetrying(false);
    }
  }

  const jobStatusCounts: DashboardJobStatusCount[] = useCountMap(summary);

  const stoppedCount =
    summary?.job_status_counts
      .filter((item) => item.status === "FAILED" || item.status === "QUOTA_LOCKED")
      .reduce((sum, item) => sum + item.count, 0) ?? 0;

  return (
    <section className="panel dashboard-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Visualization</p>
          <h2>Dashboard</h2>
        </div>
        <div className="actions">
          <button
            disabled={retrying || stoppedCount === 0}
            type="button"
            onClick={handleRetryAll}
          >
            {retrying
              ? "Retrying..."
              : `Retry stopped (${stoppedCount})`}
          </button>
          <button
            disabled={refreshState === "loading"}
            type="button"
            className="secondary"
            onClick={() => void refresh()}
          >
            Refresh
          </button>
        </div>
      </div>

      <p className="muted">
        {refreshState === "loading" && summary === null
          ? "Loading dashboard…"
          : summary
            ? `Generated ${summary.generated_at} · ${summary.total_jobs} total jobs · ${summary.success_rate_percent.toFixed(1)}% success`
            : "Awaiting first load…"}
        {lastRefreshedAt ? ` · auto-refresh OK ${formatTime(lastRefreshedAt)}` : ""}
      </p>

      {error ? <p className="error-text">Dashboard failed: {error}</p> : null}
      {retryMessage ? <p className="error-text">{retryMessage}</p> : null}

      <div className="chart-grid">
        <article className="chart-card">
          <header>
            <p className="eyebrow">Status distribution</p>
            <h3>Job queue</h3>
          </header>
          <StatusDonut summary={summary} counts={jobStatusCounts} />
        </article>

        <article className="chart-card">
          <header>
            <p className="eyebrow">24h activity</p>
            <h3>Hourly job creations (UTC)</h3>
          </header>
          <HourlyBars hours={summary?.hourly_jobs_last_24h ?? []} />
        </article>

        <article className="chart-card chart-card-wide">
          <header>
            <p className="eyebrow">Session heatmap</p>
            <h3>Top chat sessions by message count</h3>
          </header>
          <SessionHeatmap sessions={summary?.top_sessions_by_message_count ?? []} />
        </article>
      </div>
    </section>
  );
}

function useCountMap(summary: DashboardSummary | null): DashboardJobStatusCount[] {
  if (!summary) {
    return STATUS_ORDER.map((status) => ({ status, count: 0 }));
  }
  const byStatus = new Map<JobStatus, number>();
  for (const entry of summary.job_status_counts) {
    byStatus.set(entry.status, entry.count);
  }
  return STATUS_ORDER.map((status) => ({
    status,
    count: byStatus.get(status) ?? 0,
  }));
}

function StatusDonut({
  summary,
  counts,
}: {
  summary: DashboardSummary | null;
  counts: DashboardJobStatusCount[];
}) {
  const total = counts.reduce((sum, item) => sum + item.count, 0);
  const completed = counts.find((item) => item.status === "COMPLETED")?.count ?? 0;
  const success = total === 0 ? 0 : (completed / total) * 100;
  const radius = 60;
  const stroke = 22;
  const circumference = 2 * Math.PI * radius;

  let cumulativeOffset = 0;
  const segments = counts.map((entry) => {
    if (total === 0) {
      return { ...entry, dashArray: "0 999", dashOffset: 0 };
    }
    const fraction = entry.count / total;
    const dashLength = fraction * circumference;
    const segment = {
      ...entry,
      dashArray: `${dashLength} ${circumference - dashLength}`,
      dashOffset: -cumulativeOffset,
    };
    cumulativeOffset += dashLength;
    return segment;
  });

  return (
    <div className="donut-wrap">
      <svg
        className="donut"
        viewBox="-100 -100 200 200"
        role="img"
        aria-label="Job status distribution donut chart"
      >
        <circle
          r={radius}
          fill="none"
          stroke="#1b2440"
          strokeWidth={stroke}
        />
        {segments.map((segment) => (
          <circle
            key={segment.status}
            r={radius}
            fill="none"
            stroke={STATUS_COLORS[segment.status]}
            strokeWidth={stroke}
            strokeDasharray={segment.dashArray}
            strokeDashoffset={segment.dashOffset}
            transform="rotate(-90)"
          />
        ))}
        <text className="donut-total" textAnchor="middle" y="-4">
          {total}
        </text>
        <text className="donut-label" textAnchor="middle" y="18">
          total jobs
        </text>
      </svg>
      <ul className="donut-legend">
        {counts.map((entry) => (
          <li key={entry.status}>
            <span
              className="legend-swatch"
              style={{ backgroundColor: STATUS_COLORS[entry.status] }}
            />
            <span className="legend-label">{entry.status}</span>
            <span className="legend-count">{entry.count}</span>
          </li>
        ))}
      </ul>
      <p className="muted">
        Success rate:{" "}
        <strong>{summary ? summary.success_rate_percent.toFixed(1) : success.toFixed(1)}%</strong>
      </p>
    </div>
  );
}

function HourlyBars({ hours }: { hours: DashboardHourEntry[] }) {
  const width = 720;
  const height = 220;
  const padX = 28;
  const padTop = 18;
  const padBottom = 36;
  const plotWidth = width - padX * 2;
  const plotHeight = height - padTop - padBottom;
  const buckets = ensure24Hours(hours);
  const peak = Math.max(1, ...buckets.map((bucket) => bucket.count));
  const barWidth = plotWidth / buckets.length;
  return (
    <svg
      className="bars"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Hourly CGI parse job creations over the last 24 hours"
    >
      <line
        x1={padX}
        y1={height - padBottom}
        x2={width - padX}
        y2={height - padBottom}
        stroke="#2a3458"
        strokeWidth={1}
      />
      {buckets.map((bucket, index) => {
        const barHeight = (bucket.count / peak) * plotHeight;
        const x = padX + index * barWidth + 1;
        const y = height - padBottom - barHeight;
        const label = bucket.hour_utc;
        return (
          <g key={`${bucket.hour_utc}-${index}`}>
            <rect
              x={x}
              y={y}
              width={Math.max(2, barWidth - 2)}
              height={Math.max(2, barHeight)}
              rx={3}
              fill="#7c5cff"
              opacity={bucket.count === 0 ? 0.25 : 0.95}
            >
              <title>
                Hour {label.toString().padStart(2, "0")}:00 UTC · {bucket.count}{" "}
                jobs
              </title>
            </rect>
            {label % 4 === 0 ? (
              <text
                x={x + (barWidth - 2) / 2}
                y={height - padBottom + 14}
                textAnchor="middle"
                className="bar-axis-label"
              >
                {label.toString().padStart(2, "0")}
              </text>
            ) : null}
          </g>
        );
      })}
      <text
        x={padX}
        y={padTop}
        className="bar-peak-label"
      >
        Peak {peak} jobs/hr
      </text>
      <text
        x={padX}
        y={height - 6}
        className="bar-axis-label"
      >
        UTC hour of day (most recent on the right)
      </text>
    </svg>
  );
}

function ensure24Hours(
  hours: DashboardHourEntry[],
): DashboardHourEntry[] {
  if (hours.length === 24) {
    return hours;
  }
  const out: DashboardHourEntry[] = [];
  for (let i = 0; i < 24; i += 1) {
    const match = hours.find((entry) => entry.hour_utc === i);
    out.push(match ?? { hour_utc: i, count: 0 });
  }
  return out;
}

function SessionHeatmap({
  sessions,
}: {
  sessions: DashboardSessionActivity[];
}) {
  if (sessions.length === 0) {
    return <p className="empty">No chat sessions recorded yet.</p>;
  }
  const peak = Math.max(1, ...sessions.map((item) => item.message_count));
  return (
    <div className="heatmap">
      {sessions.map((item, index) => {
        const intensity = item.message_count / peak;
        return (
          <div
            key={`${item.session_id}-${index}`}
            className="heat-cell"
            style={{
              backgroundColor: `rgba(124, 92, 255, ${0.18 + intensity * 0.7})`,
            }}
            title={`${item.session_id.slice(0, 8)} · ${item.message_count} messages`}
          >
            <span className="heat-count">{item.message_count}</span>
            <span className="heat-id">{item.session_id.slice(0, 6)}</span>
          </div>
        );
      })}
    </div>
  );
}

function formatTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return parsed.toLocaleTimeString();
}
