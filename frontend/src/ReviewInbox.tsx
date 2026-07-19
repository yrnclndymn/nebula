import { useCallback, useEffect, useState, type ReactNode } from "react";
import { dismissJob, fetchTopics, getBackfill, getProposal, listJobs } from "./api";
import { AcquisitionProposalsPanel } from "./AcquisitionProposals";
import { BackfillCard, BackfillModal } from "./BackfillReview";
import { ClassifyBatch } from "./ClientClassification";
import { ResolveBatch } from "./EntityResolution";
import { Page } from "./Page";
import { PersonProposalCard } from "./PersonProposalCard";
import { ProposalCard } from "./ProposalCard";
import { ReviseThesisBatch } from "./ThesisRevisionReview";
import { dedupeProposalsByScope } from "./proposalDedupe";
import type { Backfill, JobSummary, Proposal } from "./types";
import { usePollJob } from "./usePollJob";

// How many recent proposal jobs to rehydrate into the inbox.
const PROPOSAL_LIMIT = 40;

// A back-fill awaiting review, hydrated enough to render its card (the compact
// job summary doesn't carry the field label / total).
interface BackfillRef {
  job_id: string;
  field: string;
  total: number;
}

type ScanKind = "resolve" | "classify" | "revise-thesis";

// Per-scan chrome: the panel heading + which batch component renders. Keeps the
// button row + panel body declarative as scan actions are added (#196).
const SCAN_META: Record<ScanKind, { heading: string; render: () => ReactNode }> = {
  resolve: { heading: "Resolve stub companies", render: () => <ResolveBatch key="resolve" /> },
  classify: {
    heading: "Classify client companies",
    render: () => <ClassifyBatch key="classify" />,
  },
  "revise-thesis": {
    heading: "Revise thesis from evidence",
    render: () => <ReviseThesisBatch key="revise-thesis" />,
  },
};

// The Review inbox (#153): one badged surface for every pending commit. It
// COMPOSES the existing review cards client-side from existing read endpoints —
// no new backend endpoint — so enrichment proposals, back-fills and acquisition
// proposals started anywhere (chat, backlog, the M&A page) all land here to
// review and commit. "Resolve stubs" and "Classify clients" fold in as scan
// actions that produce reviewable batches via the shared BatchReview template.
export function InboxPage() {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  // Person-enrichment proposals (#178): the card self-fetches its full detail + diff
  // and self-polls while pending, so the inbox only needs the compact job rows here.
  const [personProposals, setPersonProposals] = useState<JobSummary[]>([]);
  const [backfills, setBackfills] = useState<BackfillRef[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  const [reviewJob, setReviewJob] = useState<Backfill | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [activeScan, setActiveScan] = useState<ScanKind | null>(null);

  // Broadcast so the sidebar badge re-counts immediately after a local change.
  const reviewChanged = useCallback(() => {
    window.dispatchEvent(new CustomEvent("nebula:review-changed"));
  }, []);

  // Initial load: existing topics (for the new-topic flag), pending proposals,
  // pending back-fills. Acquisition proposals are handled by their own panel.
  useEffect(() => {
    let stop = false;
    async function load() {
      const [proposalJobs, personJobs, backfillJobs, topicList] = await Promise.all([
        listJobs({ type: "proposal", limit: PROPOSAL_LIMIT }).catch(() => [] as JobSummary[]),
        listJobs({ type: "person_proposal", limit: PROPOSAL_LIMIT }).catch(
          () => [] as JobSummary[],
        ),
        listJobs({ type: "backfill", status: "ready", limit: 50 }).catch(() => [] as JobSummary[]),
        fetchTopics().catch(() => [] as string[]),
      ]);
      // Newest-wins scope dedupe FIRST, then drop already-committed jobs — the
      // remaining proposals are the ones still awaiting a review decision.
      const openProposals = dedupeProposalsByScope(proposalJobs)
        .filter((j) => !j.summary.committed && j.status !== "error")
        .map(hydrateProposal);
      const hydratedProposals = await Promise.all(openProposals);
      const hydratedBackfills = await Promise.all(backfillJobs.map(hydrateBackfill));
      if (stop) return;
      setProposals(hydratedProposals);
      // Person proposals flip to status "committed" on commit (not "ready"), so
      // pending + ready are exactly the ones still awaiting a decision.
      setPersonProposals(
        personJobs.filter((j) => j.status === "pending" || j.status === "ready"),
      );
      setBackfills(hydratedBackfills.filter((b): b is BackfillRef => b !== null));
      setTopics(topicList);
      setLoading(false);
    }
    void load();
    return () => {
      stop = true;
    };
  }, []);

  // Poll the still-researching proposals until each settles.
  usePollJob(
    proposals.some((p) => p.status === "pending"),
    (cancelled) => {
      proposals
        .filter((p) => p.status === "pending")
        .forEach(async (p) => {
          try {
            const updated = await getProposal(p.proposal_id);
            if (cancelled() || updated.status === "pending") return;
            setProposals((ps) =>
              ps.map((x) =>
                x.proposal_id === p.proposal_id ? { ...updated, name: updated.name || p.name } : x,
              ),
            );
          } catch {
            /* transient — keep polling */
          }
        });
    },
  );

  async function dismissProposal(p: Proposal) {
    if (p.status === "ready" && !window.confirm(`Dismiss the un-reviewed proposal for ${p.name}?`)) {
      return;
    }
    try {
      await dismissJob(p.proposal_id);
      setProposals((ps) => ps.filter((x) => x.proposal_id !== p.proposal_id));
      reviewChanged();
    } catch {
      /* leave the card in place; the user can retry */
    }
  }

  async function dismissPersonProposal(jobId: string) {
    try {
      await dismissJob(jobId);
      setPersonProposals((ps) => ps.filter((j) => j.id !== jobId));
      reviewChanged();
    } catch {
      /* leave the card in place; the user can retry */
    }
  }

  const readyProposals = proposals.filter((p) => p.status === "ready");
  const pendingProposals = proposals.filter((p) => p.status === "pending");
  const nothingPending =
    !loading &&
    readyProposals.length === 0 &&
    pendingProposals.length === 0 &&
    personProposals.length === 0 &&
    backfills.length === 0;

  return (
    <Page title="Review inbox">
      <div className="inbox-actions">
        <span className="muted small">Scan for cleanup work:</span>
        <button
          className={activeScan === "resolve" ? "inbox-scan active" : "inbox-scan"}
          onClick={() => setActiveScan((s) => (s === "resolve" ? null : "resolve"))}
        >
          🧩 Resolve stubs
        </button>
        <button
          className={activeScan === "classify" ? "inbox-scan active" : "inbox-scan"}
          onClick={() => setActiveScan((s) => (s === "classify" ? null : "classify"))}
        >
          🏷 Classify clients
        </button>
        <button
          className={activeScan === "revise-thesis" ? "inbox-scan active" : "inbox-scan"}
          onClick={() => setActiveScan((s) => (s === "revise-thesis" ? null : "revise-thesis"))}
        >
          📐 Revise thesis from evidence
        </button>
      </div>

      {activeScan && (
        <div className="inbox-scan-panel">
          <div className="proposal-head">
            <strong>{SCAN_META[activeScan].heading}</strong>
            <button
              className="discard small"
              style={{ marginLeft: "auto" }}
              onClick={() => setActiveScan(null)}
            >
              Close
            </button>
          </div>
          {SCAN_META[activeScan].render()}
        </div>
      )}

      {flash && <div className="chat-flash">{flash}</div>}

      <div className="backfill-table-wrap">
        {loading ? (
          <div className="muted" style={{ padding: "1rem" }}>
            loading pending review items…
          </div>
        ) : (
          <>
            {pendingProposals.map((p) => (
              <div key={p.proposal_id} className="proposal pending">
                🔎 researching <strong>{p.name}</strong>…{" "}
                <span className="muted">this can take a minute</span>
              </div>
            ))}

            {readyProposals.map((p) => (
              <ProposalCard
                key={p.proposal_id}
                p={p}
                existingTopics={topics}
                onCommitted={reviewChanged}
                onDiscard={() => dismissProposal(p)}
              />
            ))}

            {personProposals.map((job) => (
              <PersonProposalCard
                key={job.id}
                jobId={job.id}
                name={job.summary.name || job.id}
                initialStatus={job.status as "pending" | "ready"}
                onCommitted={reviewChanged}
                onDiscard={() => dismissPersonProposal(job.id)}
              />
            ))}

            {backfills.map((b) => (
              <BackfillCard key={b.job_id} job={b} onReview={setReviewJob} />
            ))}

            <AcquisitionProposalsPanel heading="Pending acquisition proposals" />

            {nothingPending && (
              <p className="muted" style={{ padding: "1rem" }}>
                Nothing awaiting review. 🎉 Research a company from the backlog or chat, or run a
                scan above.
              </p>
            )}
          </>
        )}
      </div>

      {reviewJob && (
        <BackfillModal
          job={reviewJob}
          onClose={() => setReviewJob(null)}
          onCommitted={(n) => {
            const committed = reviewJob;
            setReviewJob(null);
            setBackfills((bs) => bs.filter((b) => b.job_id !== committed.job_id));
            setFlash(`✓ committed ${n} ${committed.field.label} values — refresh the table to see them.`);
            reviewChanged();
          }}
        />
      )}
    </Page>
  );
}

// Hydrate a compact proposal summary into a Proposal. Ready proposals need the
// full record/diff (per-id) so the card can render its review; pending rows
// render from the summary alone.
async function hydrateProposal(job: JobSummary): Promise<Proposal> {
  const base: Proposal = {
    proposal_id: job.id,
    name: job.summary.name || job.id,
    status: job.status as Proposal["status"],
    discovered_website: job.summary.discovered_website,
    error: job.summary.error,
  };
  if (job.status === "ready") {
    try {
      return await getProposal(job.id);
    } catch {
      return base;
    }
  }
  return base;
}

// The job listing doesn't carry a back-fill's field label / total, so read the
// (small) detail to build the card's ref. A read failure drops the card.
async function hydrateBackfill(job: JobSummary): Promise<BackfillRef | null> {
  try {
    const full = await getBackfill(job.id);
    return { job_id: job.id, field: full.field.label, total: full.total };
  } catch {
    return null;
  }
}
