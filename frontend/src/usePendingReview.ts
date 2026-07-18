import { useCallback, useEffect, useState } from "react";
import { fetchAcquisitionProposals, listJobs } from "./api";
import { dedupeProposalsByScope } from "./proposalDedupe";
import { usePollJob } from "./usePollJob";

// How often the badge re-counts on its own. Commits inside the inbox fire a
// `nebula:review-changed` event for an immediate decrement; this poll is the
// backstop that also catches work started/committed elsewhere (chat, the
// acquisition panel, another tab).
const POLL_MS = 15000;

// Pending-review count for the Review sidebar badge (#153) — the number of items
// awaiting a commit decision across the surfaces the inbox composes: ready
// enrichment proposals, ready back-fills, and ready acquisition proposals. Scan
// batches (resolve/classify) are on-demand, so they don't contribute. Composed
// from the same read endpoints the inbox uses — no new backend endpoint.
export function usePendingReview(): number {
  const [count, setCount] = useState(0);

  const load = useCallback(async () => {
    try {
      const [proposals, backfills, acqs] = await Promise.all([
        listJobs({ type: "proposal", limit: 50 }),
        listJobs({ type: "backfill", status: "ready", limit: 50 }),
        fetchAcquisitionProposals(),
      ]);
      const readyProposals = dedupeProposalsByScope(proposals).filter(
        (j) => j.status === "ready" && !j.summary.committed,
      );
      const readyAcqs = acqs.filter((a) => a.status === "ready");
      setCount(readyProposals.length + backfills.length + readyAcqs.length);
    } catch {
      /* transient — keep the last count */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Light backstop poll (always on — the sidebar is always mounted).
  usePollJob(true, load, { intervalMs: POLL_MS });

  // Immediate re-count when the inbox commits/dismisses something.
  useEffect(() => {
    const handler = () => void load();
    window.addEventListener("nebula:review-changed", handler);
    return () => window.removeEventListener("nebula:review-changed", handler);
  }, [load]);

  return count;
}
