import { useCallback, useEffect, useRef, useState } from "react";
import {
  commitAcquisitionProposal,
  dismissJob,
  fetchAcquisitionProposal,
  fetchAcquisitionProposals,
} from "./api";
import type {
  Acquisition,
  AcquisitionDiffEntry,
  AcquisitionProposalDetail,
  AcquisitionProposalRow,
} from "./types";

// #133 SPA review card for acquisition proposals — the review surface the #43
// propose→review→commit loop was missing. A proposal (an `acquisition_proposal`
// job) is discovered via GET /ma/proposals, its full detail + citations read from
// GET /companies/acquisitions/{job_id}, then committed (writes :ACQUIRED edges —
// confirmed first) or discarded (removes the job). Commit is the ONLY write path.
//
// Every string here is job/graph data (deal facts are derived from crawled,
// untrusted evidence) so it renders as escaped text; a `source`/`amount_source`
// URL becomes a link only when it is http(s), and an amount is shown ONLY next to
// its own citation — an uncited figure is never surfaced (the provenance rule).

const POLL_MS = 3000;

function isHttpUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function whenLabel(deal: Acquisition): string | null {
  const raw = deal.announced_at || deal.closed_at;
  if (!raw) return null;
  const t = Date.parse(raw);
  return Number.isNaN(t) ? raw : new Date(t).toLocaleDateString();
}

// The diff (new/changed deals vs. what's stored) keys on the acquirer→target pair,
// so a proposed deal can be badged "new" / "updated" for the reviewer.
function diffFor(
  deal: Acquisition,
  diff: AcquisitionDiffEntry[] | null | undefined,
): AcquisitionDiffEntry | undefined {
  return (diff || []).find(
    (d) => d.deal.acquirer === deal.acquirer && d.deal.target === deal.target,
  );
}

function ProposalDealRow({
  deal,
  change,
}: {
  deal: Acquisition;
  change?: AcquisitionDiffEntry;
}) {
  const when = whenLabel(deal);
  return (
    <li className="deal-item">
      <div className="deal-head">
        <strong>{deal.acquirer}</strong>
        <span className="muted small">→</span>
        <strong>{deal.target}</strong>
        {when && <span className="muted small"> · {when}</span>}
        {change?.status === "new" && <span className="activity-badge run">new</span>}
        {change?.status === "update" && <span className="activity-badge run">updated</span>}
      </div>
      {deal.amount && isHttpUrl(deal.amount_source) && (
        <div className="deal-amount">
          {deal.currency ? `${deal.currency} ` : ""}
          {deal.amount}{" "}
          <a href={deal.amount_source} target="_blank" rel="noreferrer">
            source ↗
          </a>
          {change?.status === "update" && change.old_amount && (
            <span className="muted small"> (was {change.old_amount})</span>
          )}
        </div>
      )}
      {deal.thesis && <div className="deal-thesis muted small">{deal.thesis}</div>}
      {isHttpUrl(deal.source) && (
        <a className="deal-source small" href={deal.source} target="_blank" rel="noreferrer">
          deal source ↗
        </a>
      )}
    </li>
  );
}

// One proposal's review card. Fetches full detail; while the proposal is still
// researching (status pending) it polls until it turns ready/error. Commit confirms
// first (it writes edges); discard confirms then removes the job. `onResolved` lets
// the parent drop the card and refresh its list after either action.
export function AcquisitionProposalCard({
  row,
  onResolved,
}: {
  row: AcquisitionProposalRow;
  onResolved: (jobId: string) => void;
}) {
  const [detail, setDetail] = useState<AcquisitionProposalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const company = row.company || detail?.company || "this company";

  const load = useCallback(async () => {
    try {
      setDetail(await fetchAcquisitionProposal(row.job_id));
    } catch (e) {
      setError(String(e));
    }
  }, [row.job_id]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll only while the proposal is still researching — a ready/errored board is
  // static, so we stop hitting the endpoint once the work has settled.
  const researching = (detail?.status ?? row.status) === "pending";
  useEffect(() => {
    if (!researching) return;
    const iv = setInterval(load, POLL_MS);
    return () => clearInterval(iv);
  }, [researching, load]);

  async function commit() {
    if (!window.confirm(`Commit this acquisition proposal for ${company}? It writes ACQUIRED edges to the graph.`))
      return;
    setBusy(true);
    setError(null);
    try {
      const res = await commitAcquisitionProposal(row.job_id);
      if (res.error) {
        setError(res.error);
        setBusy(false);
        return;
      }
      onResolved(row.job_id);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  async function discard() {
    if (!window.confirm(`Discard the acquisition proposal for ${company}?`)) return;
    setBusy(true);
    setError(null);
    try {
      await dismissJob(row.job_id);
      onResolved(row.job_id);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  const status = detail?.status ?? row.status;
  const deals = detail?.record?.deals ?? [];
  const hasFacts = deals.length > 0;

  return (
    <div className="proposal acq-proposal">
      <div className="proposal-head">
        <span className="tag">🤝 acquisition proposal</span>
        <strong>{company}</strong>
        {status === "pending" && <span className="activity-badge run">researching…</span>}
        {status === "ready" && hasFacts && (
          <span className="activity-badge done">
            {deals.length} deal{deals.length === 1 ? "" : "s"}
            {row.new_count > 0 ? ` · ${row.new_count} new/changed` : ""}
          </span>
        )}
        {status === "error" && <span className="activity-badge err">error</span>}
      </div>

      {status === "pending" && (
        <div className="muted small">Researching this company&rsquo;s M&amp;A history…</div>
      )}

      {status === "error" && (
        <div className="proposal-err">⚠ {detail?.error || row.error || "research failed"}</div>
      )}

      {status === "ready" && !hasFacts && (
        <div className="muted small">
          {detail?.outcome || "No cited acquisitions found — nothing to commit."}
        </div>
      )}

      {status === "ready" && hasFacts && (
        <ul className="deal-list">
          {deals.map((d) => (
            <ProposalDealRow
              key={`${d.acquirer}→${d.target}`}
              deal={d}
              change={diffFor(d, detail?.diff)}
            />
          ))}
        </ul>
      )}

      {error && <div className="proposal-err">⚠ {error}</div>}

      <div className="proposal-actions">
        {status === "ready" && hasFacts && (
          <button className="commit" onClick={commit} disabled={busy}>
            {busy ? "committing…" : "Commit"}
          </button>
        )}
        {status !== "pending" && (
          <button className="discard" onClick={discard} disabled={busy}>
            Discard
          </button>
        )}
      </div>
    </div>
  );
}

// A list of proposals awaiting review. Scoped to one `company` for the drawer, or
// unscoped for the M&A page. Polls the (light) listing while anything is still
// researching so a freshly-started proposal fills in without a manual refresh.
// Renders nothing when there are no proposals (so the drawer section stays hidden).
export function AcquisitionProposalsPanel({
  company,
  heading,
}: {
  company?: string;
  heading?: string;
}) {
  const [rows, setRows] = useState<AcquisitionProposalRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolved = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const data = await fetchAcquisitionProposals(company);
      setRows(data.filter((r) => !resolved.current.has(r.job_id)));
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoaded(true);
    }
  }, [company]);

  useEffect(() => {
    load();
  }, [load]);

  const anyResearching = rows.some((r) => r.status === "pending");
  useEffect(() => {
    if (!anyResearching) return;
    const iv = setInterval(load, POLL_MS);
    return () => clearInterval(iv);
  }, [anyResearching, load]);

  const onResolved = useCallback((jobId: string) => {
    resolved.current.add(jobId);
    setRows((rs) => rs.filter((r) => r.job_id !== jobId));
  }, []);

  if (!loaded && rows.length === 0) return null;
  if (rows.length === 0 && !error) return null;

  return (
    <div className="acq-proposals">
      <span className="field-label">
        {heading || "Pending acquisition proposals"} <span className="muted">({rows.length})</span>
      </span>
      {error && <div className="proposal-err">⚠ couldn&rsquo;t load proposals: {error}</div>}
      {rows.map((r) => (
        <AcquisitionProposalCard key={r.job_id} row={r} onResolved={onResolved} />
      ))}
    </div>
  );
}
