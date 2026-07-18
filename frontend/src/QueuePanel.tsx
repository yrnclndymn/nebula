import { useEffect, useMemo, useRef, useState } from "react";
import { dismissJob, fetchBacklog, getProposal, listJobs, researchBacklog } from "./api";
import type { BacklogRow, JobSummary, Proposal } from "./types";
import { Page } from "./Page";
import { ProposalCard } from "./ProposalCard";
import { dedupeProposalsByScope } from "./proposalDedupe";
import { usePollJob } from "./usePollJob";

// Server-side sanity cap: at most this many companies per "Research selected"
// request (mirrors MAX_BACKLOG_RESEARCH on the backend). The UI enforces the same
// ceiling so the user can't build a selection the server will reject.
const MAX_SELECT = 10;

// How many recent jobs to pull for the shared board/queue. One list feeds both the
// research proposals (top) and the agent-activity board (bottom); the poll cadence
// applies while anything is still in flight.
const ACTIVITY_LIMIT = 60;
const POLL_MS = 3000;

type Emphasis = "score" | "client" | "partner";

// Queue & activity (issue #154): a single Review tab that merges the old Backlog and
// Activity tabs into one pipeline view. Two stacked regions share ONE `/jobs` fetch
// and ONE poll (they previously hit listJobs separately):
//
//   • top — the ranked list of un-researched stubs (issue #30/#31): score
//     components, filters, multi-select and a "Research selected" trigger that runs
//     each stub through the durable propose→review→commit flow, followed by the
//     resulting research proposals as review cards (nothing is written to the graph
//     until the user commits — HITL). Proposal jobs live here, not in the board
//     below, so there's exactly one dismiss/retry surface per proposal.
//   • bottom — the live agent-activity board (issue #48/#49) for every OTHER job
//     type (back-fills, resolutions, scans, prunes…): active / recently completed /
//     failed, each dismissable, rehydrated from the graph so work survives a refresh.
export function QueuePage() {
  const [rows, setRows] = useState<BacklogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [minMentions, setMinMentions] = useState(1);
  const [emphasis, setEmphasis] = useState<Emphasis>("score");

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // The single durable-job list, newest-first: rehydrated from the graph on open,
  // then grown/pruned in-session as the user triggers/retries/dismisses. Everything
  // (proposal review cards, per-row status badge, and the activity board) derives
  // from this one snapshot — nothing durable relies on client-session state.
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState<string | null>(null);

  // Full-detail cache for ready proposals (keyed by proposal_id) — the list summary
  // lacks the diff the ProposalCard needs, so ready proposals are hydrated per-id
  // once and reused across polls.
  const [hydrated, setHydrated] = useState<Record<string, Proposal>>({});

  useEffect(() => {
    let stop = false;
    fetchBacklog()
      .then((r) => !stop && setRows(r))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, []);

  // Initial job-list load. Best-effort: the backlog table still works without it.
  useEffect(() => {
    let stop = false;
    listJobs({ limit: ACTIVITY_LIMIT })
      .then((j) => !stop && setJobs(j))
      .catch((e) => !stop && setJobsError(String(e)))
      .finally(() => !stop && setJobsLoading(false));
    return () => {
      stop = true;
    };
  }, []);

  // Research proposals awaiting review, derived from the shared list. Scope-aware
  // per-name newest-wins dedupe runs FIRST (issue #102) so a fresh success supersedes
  // a stale error before we drop already-committed jobs (which aren't awaiting
  // anything — their node status stays "ready" by design of the two-step commit).
  const proposals = useMemo(() => {
    const proposalJobs = jobs.filter((j) => j.type === "proposal");
    const deduped = dedupeProposalsByScope(proposalJobs);
    return deduped.filter((j) => !j.summary.committed);
  }, [jobs]);

  // The activity board covers every OTHER job type — proposals have their own
  // richer surface above, so they aren't double-listed (and double-dismissable) here.
  const board = useMemo(() => groupJobs(jobs.filter((j) => j.type !== "proposal")), [jobs]);

  // Hydrate ready proposals we haven't fetched yet (per-id detail for the diff).
  const hydratedRef = useRef(hydrated);
  hydratedRef.current = hydrated;
  useEffect(() => {
    let stop = false;
    proposals
      .filter((p) => p.status === "ready" && !hydratedRef.current[p.id])
      .forEach(async (p) => {
        try {
          const full = await getProposal(p.id);
          if (!stop) setHydrated((h) => ({ ...h, [p.id]: full }));
        } catch {
          /* transient — the review card falls back to the summary line */
        }
      });
    return () => {
      stop = true;
    };
  }, [proposals]);

  // One poll for both regions: refresh the whole list while anything is still in
  // flight (a running board job OR a pending proposal), then stop. A finished
  // pipeline is static, so we stop hitting the endpoint once nothing is running.
  const anyActive = board.active.length > 0 || proposals.some((p) => p.status === "pending");
  usePollJob(
    anyActive,
    async (cancelled) => {
      try {
        const next = await listJobs({ limit: ACTIVITY_LIMIT });
        if (cancelled()) return;
        // Retain optimistic entries the server snapshot hasn't caught up to yet
        // (a just-enqueued proposal may not be persisted by the time this returns),
        // so freshly-triggered research doesn't flicker out then back in.
        setJobs((prev) => {
          const seen = new Set(next.map((j) => j.id));
          const pendingLocal = prev.filter((j) => !seen.has(j.id) && j.status === "pending");
          return [...pendingLocal, ...next];
        });
      } catch {
        /* transient — keep the last snapshot and try again next tick */
      }
    },
    { intervalMs: POLL_MS },
  );

  // Latest proposal status per company name (proposals is newest-first, so the first
  // match wins) — drives the table's Status column.
  const statusByName = useMemo(() => {
    const m = new Map<string, Proposal["status"]>();
    for (const p of proposals) {
      const name = p.summary.name;
      if (name && !m.has(name)) m.set(name, p.status as Proposal["status"]);
    }
    return m;
  }, [proposals]);

  const view = useMemo(() => {
    const filtered = rows.filter((r) => r.mention_count >= minMentions);
    const cmp: Record<Emphasis, (a: BacklogRow, b: BacklogRow) => number> = {
      score: (a, b) => b.rank_score - a.rank_score || b.mention_count - a.mention_count,
      client: (a, b) => b.client_mentions - a.client_mentions || b.rank_score - a.rank_score,
      partner: (a, b) =>
        b.partner_mentions - a.partner_mentions ||
        b.cloud_isv_partner_mentions - a.cloud_isv_partner_mentions ||
        b.rank_score - a.rank_score,
    };
    return [...filtered].sort((a, b) => cmp[emphasis](a, b) || a.name.localeCompare(b.name));
  }, [rows, minMentions, emphasis]);

  function toggle(name: string) {
    setNotice(null);
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(name)) {
        next.delete(name);
      } else if (next.size >= MAX_SELECT) {
        setNotice(`You can research at most ${MAX_SELECT} companies at a time.`);
        return s;
      } else {
        next.add(name);
      }
      return next;
    });
  }

  // Prepend freshly-triggered proposals as optimistic pending jobs so they show (and
  // get polled) immediately. The list ids ARE proposal ids, so the next poll dedupes
  // these against the persisted rows.
  function addPending(fresh: { name: string; proposal_id: string }[]) {
    setJobs((js) => [
      ...fresh.map(
        (p): JobSummary => ({
          id: p.proposal_id,
          type: "proposal",
          status: "pending",
          createdAt: new Date().toISOString(),
          summary: { name: p.name },
        }),
      ),
      ...js,
    ]);
  }

  async function research() {
    if (!selected.size || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const res = await researchBacklog([...selected]);
      addPending(res.proposals);
      setSelected(new Set());
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  }

  // Re-trigger an errored proposal: re-uses the same endpoint, creating a FRESH
  // proposal for that name (issue #66). Drop the stale errored job for this name so
  // the old error card doesn't linger next to the fresh pending one.
  async function retry(name: string) {
    if (busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const res = await researchBacklog([name]);
      setJobs((js) => [
        ...res.proposals.map(
          (p): JobSummary => ({
            id: p.proposal_id,
            type: "proposal",
            status: "pending",
            createdAt: new Date().toISOString(),
            summary: { name: p.name },
          }),
        ),
        ...js.filter(
          (j) => !(j.type === "proposal" && j.summary.name === name && j.status === "error"),
        ),
      ]);
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  }

  // Dismiss a durable job (#73): removes the job node. Un-reviewed ready proposals
  // are unreviewed work, so those confirm first; committed proposals keep status
  // "ready" by design (two-step commit) and skip the prompt.
  async function dismiss(job: JobSummary) {
    const target = job.summary.name || job.id;
    const unreviewed = job.status === "ready" && !job.summary.committed;
    if (unreviewed && !window.confirm(`Dismiss the un-reviewed job for ${target}?`)) {
      return;
    }
    try {
      await dismissJob(job.id);
      setJobs((js) => js.filter((j) => j.id !== job.id));
      setHydrated((h) => {
        if (!(job.id in h)) return h;
        const next = { ...h };
        delete next[job.id];
        return next;
      });
    } catch (e) {
      setNotice(String(e));
    }
  }

  const proposalsLoading = jobsLoading && !jobsError;

  return (
    <Page
      title={
        <>
          Queue &amp; activity{" "}
          {board.active.length > 0 && (
            <span className="activity-live">● {board.active.length} running</span>
          )}
        </>
      }
    >
      <div className="backlog-filters">
        <label>
          Min mentions
          <input
            type="number"
            min={1}
            value={minMentions}
            onChange={(e) => setMinMentions(Math.max(1, Number(e.target.value) || 1))}
          />
        </label>
        <label>
          Emphasis
          <select value={emphasis} onChange={(e) => setEmphasis(e.target.value as Emphasis)}>
            <option value="score">Balanced (score)</option>
            <option value="client">Client-of</option>
            <option value="partner">Partner-of</option>
          </select>
        </label>
        <span className="muted small">
          Select up to {MAX_SELECT} un-researched stubs, then research them. Results come back as
          proposals to review and commit below — nothing is saved automatically.
        </span>
      </div>

      <div className="backfill-table-wrap">
        {error ? (
          <div className="proposal-err">⚠ couldn't load the backlog: {error}</div>
        ) : loading ? (
          <div className="muted" style={{ padding: "1rem" }}>
            loading backlog…
          </div>
        ) : view.length === 0 ? (
          <p className="muted" style={{ padding: "1rem" }}>
            No un-researched stubs match this filter. 🎉
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th></th>
                <th>Company</th>
                <th className="num" title="client_mentions + partner_mentions + boosted cloud/ISV partners">
                  Score
                </th>
                <th className="num">Mentions</th>
                <th className="num" title="Distinct researched companies that name it as a client">
                  Client-of
                </th>
                <th className="num" title="Distinct researched companies that name it as a partner">
                  Partner-of
                </th>
                <th className="num" title="Partners that are cloud providers / ISVs (score-boosted)">
                  Cloud/ISV
                </th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {view.map((r) => {
                const st = statusByName.get(r.name);
                return (
                  <tr key={r.name}>
                    <td>
                      {!st && (
                        <input
                          type="checkbox"
                          checked={selected.has(r.name)}
                          onChange={() => toggle(r.name)}
                          aria-label={`Select ${r.name}`}
                        />
                      )}
                    </td>
                    <td>{r.name}</td>
                    <td className="num">
                      <strong>{r.rank_score}</strong>
                    </td>
                    <td className="num">{r.mention_count}</td>
                    <td className="num">{r.client_mentions}</td>
                    <td className="num">{r.partner_mentions}</td>
                    <td className="num">{r.cloud_isv_partner_mentions || "—"}</td>
                    <td>
                      {st ? <BacklogStatusBadge status={st} /> : <span className="muted">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {(proposals.length > 0 || (!proposalsLoading && !loading)) && (
          <div className="backlog-review">
            <div className="diff-group-h">Research proposals</div>
            {proposalsLoading ? (
              <div className="muted small">loading recent research…</div>
            ) : proposals.length === 0 ? (
              <div className="muted small">No research proposals awaiting review.</div>
            ) : (
              proposals.map((p) =>
                p.status === "pending" ? (
                  <div key={p.id} className="proposal pending">
                    🔎 researching <strong>{p.summary.name || p.id}</strong>…{" "}
                    <span className="muted">this can take a minute</span>
                  </div>
                ) : p.status === "error" ? (
                  <div key={p.id} className="proposal">
                    <div>
                      ⚠ couldn't research <strong>{p.summary.name || p.id}</strong>
                      {p.summary.error ? `: ${p.summary.error}` : ""}
                    </div>
                    <div className="proposal-foot">
                      <button
                        className="commit"
                        disabled={busy || !p.summary.name}
                        onClick={() => p.summary.name && retry(p.summary.name)}
                      >
                        Retry
                      </button>{" "}
                      <button className="discard" onClick={() => dismiss(p)}>
                        Dismiss
                      </button>
                    </div>
                  </div>
                ) : hydrated[p.id] ? (
                  // The card's own Discard is wired to real dismissal here — one
                  // control, one behaviour (review finding on #74).
                  <ProposalCard key={p.id} p={hydrated[p.id]} onDiscard={() => dismiss(p)} />
                ) : (
                  <div key={p.id} className="muted small">
                    loading proposal for {p.summary.name || p.id}…
                  </div>
                ),
              )
            )}
          </div>
        )}
      </div>

      {notice && <div className="proposal-err">{notice}</div>}

      <div className="backfill-foot">
        <button className="commit" onClick={research} disabled={busy || !selected.size}>
          {busy ? "starting…" : `Research selected (${selected.size})`}
        </button>
      </div>

      <div className="backfill-table-wrap">
        <div className="diff-group-h">Agent activity</div>
        {jobsError ? (
          <div className="proposal-err">⚠ couldn't load activity: {jobsError}</div>
        ) : jobsLoading ? (
          <div className="muted" style={{ padding: "1rem" }}>
            loading activity…
          </div>
        ) : board.active.length + board.completed.length + board.failed.length === 0 ? (
          <p className="muted" style={{ padding: "1rem" }}>
            No agent jobs yet. Run a scheduled task or a back-fill to see activity here.
          </p>
        ) : (
          <>
            <Section title="Active" jobs={board.active} empty="Nothing running right now." />
            <Section
              title="Recently completed"
              jobs={board.completed}
              empty="No completed jobs yet."
              onDismiss={dismiss}
            />
            <Section title="Failed" jobs={board.failed} empty="No failures. 🎉" onDismiss={dismiss} />
          </>
        )}
      </div>
    </Page>
  );
}

function BacklogStatusBadge({ status }: { status: Proposal["status"] }) {
  if (status === "ready") return <span className="proposal-done">ready</span>;
  if (status === "error") return <span className="proposal-err">error</span>;
  return <span className="muted">researching…</span>;
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
        <JobStatusBadge status={job.status} />
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

function JobStatusBadge({ status }: { status: string }) {
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
