import { commitClassification, getClassification, scanClassification } from "./api";
import { BatchReview } from "./BatchReview";
import type { Classification } from "./types";
import { useScanJob } from "./useScanJob";
import { useEffect, useState } from "react";

// Human-in-the-loop review for bulk client-kind classification, folded into the
// Review inbox as a scan action (#153). A heuristic only *proposes* end-customer
// stubs (only-inbound-HAS_CLIENT, no other signal); nothing is written until the
// reviewer approves a subset and commits. Shares the scan→poll lifecycle and the
// commit chrome (useScanJob + BatchReview) with the Resolve-stubs flow; this owns
// only the candidate list + per-candidate approval.
//
// Per-candidate approval is a checkbox today; the shared BatchReview body slot is
// where #158's kind-select / remove control will live once that story lands.
export function ClassifyBatch() {
  const { data: res, jobId } = useScanJob<Classification>(scanClassification, getClassification);
  const [approved, setApproved] = useState<Set<string>>(new Set());

  // Default to approving every candidate once the scan lands — the heuristic is
  // conservative.
  useEffect(() => {
    if (!res || res.status !== "ready") return;
    setApproved(new Set(res.candidates.map((c) => c.name)));
  }, [res]);

  function toggle(name: string) {
    setApproved((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  const ready = res !== null && res.status === "ready";

  const body =
    ready && res ? (
      <>
        <p className="muted" style={{ margin: "0 0 0.75rem" }}>
          {res.candidates.length} stubs look like end-customers (only inbound HAS_CLIENT, no other
          signal). Uncheck any you want to leave alone, then commit to set their kind to "client".
        </p>

        {res.candidates.length > 0 ? (
          <div className="proposal">
            <div className="proposal-head">
              <strong>Client candidates</strong>
              <span className="muted">will be set to kind = client</span>
            </div>
            <div className="name-chips">
              {res.candidates.map((c) => (
                <label
                  key={c.name}
                  className={`chip ${approved.has(c.name) ? "" : "muted"}`}
                  title={`${c.inbound} inbound HAS_CLIENT edge${c.inbound === 1 ? "" : "s"}`}
                >
                  <input
                    type="checkbox"
                    checked={approved.has(c.name)}
                    onChange={() => toggle(c.name)}
                  />{" "}
                  {c.name} <span className="muted num">·{c.inbound}</span>
                </label>
              ))}
            </div>
          </div>
        ) : (
          <p className="muted">No client-kind candidates found. 🎉</p>
        )}
      </>
    ) : null;

  return (
    <BatchReview
      state={res}
      scanningLabel="scanning stubs for end-customer organisations…"
      canCommit={approved.size > 0 && jobId !== null}
      commitLabel={`Classify ${approved.size} as client`}
      onCommit={() => commitClassification(jobId!, [...approved])}
      doneMessage={(r) => `✓ classified ${r.classified ?? 0} as client. Refresh to see changes.`}
    >
      {body}
    </BatchReview>
  );
}
