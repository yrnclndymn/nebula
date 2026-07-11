import { useEffect, useMemo, useState } from "react";
import { dismissJob, listJobs } from "./api";
import type { JobSummary } from "./types";

// How many recent jobs to show, and how often to poll while anything is active.
const ACTIVITY_LIMIT = 60;
const POLL_MS = 3000;

// Agent activity page (issue #48): a live view of durable :Job nodes across every
// job type — proposals, back-fills, scheduled prunes, etc. — grouped into active
// (still running), recently completed (with their outcome line, issue #49), and
// failed (with the human-readable error and a collapsible raw detail). Reads the
// existing GET /jobs listing endpoint and polls lightly while work is in flight.
export function ActivityModal({ onClose }: { onClose: () => void }) {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Initial load.
  useEffect(() => {
    let stop = false;
    listJobs({ limit: ACTIVITY_LIMIT })
      .then((j) => !stop && setJobs(j))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, []);

  const { active, completed, failed } = useMemo(() => groupJobs(jobs), [jobs]);

  // Light polling ONLY while something is still active — a finished board is
  // static, so we stop hitting the endpoint once nothing is running.
  useEffect(() => {
    if (active.length === 0) return;
    const iv = setInterval(async () => {
      try {
        setJobs(await listJobs({ limit: ACTIVITY_LIMIT }));
      } catch {
        /* transient — keep the last snapshot and try again next tick */
      }
    }, POLL_MS);
    return () => clearInterval(iv);
  }, [active.length]);

  // Dismiss a finished/errored job from history (#73). Ready jobs may hold
  // un-reviewed work, so those confirm first; pending jobs never get the button.
  async function dismiss(job: JobSummary) {
    const target = job.summary.name || job.id;
    if (job.status === "ready" && !window.confirm(`Dismiss the un-reviewed job for ${target}?`)) {
      return;
    }
    try {
      await dismissJob(job.id);
      setJobs((js) => js.filter((j) => j.id !== job.id));
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="backfill-overlay" onClick={onClose}>
      <div className="backfill-modal activity-modal" onClick={(e) => e.stopPropagation()}>
        <div className="backfill-head">
          <strong>
            Agent activity{" "}
            {active.length > 0 && <span className="activity-live">● {active.length} running</span>}
          </strong>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="backfill-table-wrap">
          {error ? (
            <div className="proposal-err">⚠ couldn't load activity: {error}</div>
          ) : loading ? (
            <div className="muted" style={{ padding: "1rem" }}>
              loading activity…
            </div>
          ) : jobs.length === 0 ? (
            <p className="muted" style={{ padding: "1rem" }}>
              No agent jobs yet. Research a company or run a scheduled task to see activity here.
            </p>
          ) : (
            <>
              <Section title="Active" jobs={active} empty="Nothing running right now." />
              <Section
                title="Recently completed"
                jobs={completed}
                empty="No completed jobs yet."
                onDismiss={dismiss}
              />
              <Section title="Failed" jobs={failed} empty="No failures. 🎉" onDismiss={dismiss} />
            </>
          )}
        </div>

        <div className="backfill-foot">
          <button className="discard" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function Section({
  title,
  jobs,
  empty,
  onDismiss,
}: {
  title: string;
  jobs: JobSummary[];
  empty: string;
  onDismiss?: (job: JobSummary) => void;
}) {
  return (
    <div className="activity-section">
      <div className="diff-group-h">
        {title} <span className="muted">({jobs.length})</span>
      </div>
      {jobs.length === 0 ? (
        <div className="muted small">{empty}</div>
      ) : (
        jobs.map((j) => <JobRow key={j.id} job={j} onDismiss={onDismiss} />)
      )}
    </div>
  );
}

function JobRow({ job, onDismiss }: { job: JobSummary; onDismiss?: (job: JobSummary) => void }) {
  const { name, outcome, done, total, error, error_detail } = job.summary;
  const target = name || job.id;
  const showProgress = typeof done === "number" && typeof total === "number" && total > 0;
  return (
    <div className="activity-row">
      <div className="activity-row-head">
        <span className="tag">{job.type}</span>
        <span className="activity-target">{target}</span>
        <StatusBadge status={job.status} />
        <span className="muted small activity-when">{relativeTime(job.createdAt)}</span>
        {onDismiss && (
          <button
            className="discard small activity-dismiss"
            onClick={() => onDismiss(job)}
            title="Remove this job from history"
          >
            ✕
          </button>
        )}
      </div>
      {showProgress && (
        <div className="activity-progress">
          <div className="activity-bar">
            <div
              className="activity-bar-fill"
              style={{ width: `${Math.min(100, Math.round((done! / total!) * 100))}%` }}
            />
          </div>
          <span className="muted small">
            {done} / {total}
          </span>
        </div>
      )}
      {outcome && <div className="activity-outcome">{outcome}</div>}
      {error && (
        <div className="activity-error">
          <span className="proposal-err">⚠ {error}</span>
          {error_detail && (
            <details className="activity-detail">
              <summary>raw detail</summary>
              <pre>{error_detail}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "error"
      ? "activity-badge err"
      : status === "pending" || status === "running"
        ? "activity-badge run"
        : "activity-badge done";
  const label = status === "pending" ? "running" : status;
  return <span className={cls}>{label}</span>;
}

// pending/running = active; error = failed; everything else (ready/done) = done.
function groupJobs(jobs: JobSummary[]) {
  const active: JobSummary[] = [];
  const completed: JobSummary[] = [];
  const failed: JobSummary[] = [];
  for (const j of jobs) {
    if (j.status === "pending" || j.status === "running") active.push(j);
    else if (j.status === "error") failed.push(j);
    else completed.push(j);
  }
  return { active, completed, failed };
}

// Compact "3m ago" style relative time; falls back to the raw string if unparseable.
function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
