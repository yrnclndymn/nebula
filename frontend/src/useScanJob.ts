import { useEffect, useState } from "react";

// Shared scan→poll lifecycle (#153). The Resolve-stubs and Classify-clients
// flows each kicked off a durable scan job and hand-rolled the SAME recursive
// poll loop (start the scan, poll every 2s until the job turns ready/error, and
// bail on unmount). This extracts that loop once so both flows — folded into the
// Review inbox as batch-review actions — share one implementation.
//
// `scan` and `poll` are the flow's start + read endpoints (e.g. scanResolution /
// getResolution). Returns the settled batch (`data`, null while still scanning)
// and the durable job id (needed to commit the reviewer's decisions).
export function useScanJob<T extends { status: string }>(
  scan: () => Promise<{ job_id: string }>,
  poll: (jobId: string) => Promise<T>,
): { data: T | null; jobId: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  // scan/poll are module-level endpoint fns (stable) — the scan runs once on
  // mount, exactly as the original per-flow effects did.
  useEffect(() => {
    let stop = false;
    let id: string | null = null;
    const tick = async () => {
      try {
        if (!id) {
          id = (await scan()).job_id;
          if (!stop) setJobId(id);
        }
        const r = await poll(id);
        if (stop) return;
        if (r.status === "ready" || r.status === "error") {
          setData(r);
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (!stop) setTimeout(tick, 2000);
    };
    void tick();
    return () => {
      stop = true;
    };
    // scan/poll are the flow's stable module-level endpoint fns, so this runs once.
  }, [scan, poll]);

  return { data, jobId };
}
