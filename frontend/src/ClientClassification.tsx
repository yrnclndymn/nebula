import { useEffect, useState } from "react";
import { commitClassification, getClassification, scanClassification } from "./api";
import type { Classification } from "./types";

// Human-in-the-loop review for bulk client-kind classification. A heuristic only
// *proposes* end-customer stubs (only-inbound-HAS_CLIENT, no other signal);
// nothing is written until the reviewer approves a subset and commits. Mirrors
// EntityResolutionModal's scan→poll→commit conventions.
export function ClientClassificationModal({ onClose }: { onClose: () => void }) {
  const [res, setRes] = useState<Classification | null>(null);
  const [approved, setApproved] = useState<Set<string>>(new Set());
  const [committing, setCommitting] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Kick off a scan, then poll it to completion.
  useEffect(() => {
    let stop = false;
    let jobId: string | null = null;
    const poll = async () => {
      try {
        if (!jobId) jobId = (await scanClassification()).job_id;
        const r = await getClassification(jobId);
        if (stop) return;
        if (r.status === "ready" || r.status === "error") {
          setRes(r);
          // Default to approving every candidate — the heuristic is conservative.
          setApproved(new Set(r.candidates.map((c) => c.name)));
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (!stop) setTimeout(poll, 2000);
    };
    poll();
    return () => {
      stop = true;
    };
  }, []);

  function toggle(name: string) {
    setApproved((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  async function commit() {
    if (!res || !approved.size) return;
    setCommitting(true);
    setError(null);
    try {
      const r = await commitClassification(res.job_id, [...approved]);
      if (r.error) throw new Error(r.error);
      setDone(`✓ classified ${r.classified ?? 0} as client. Refresh to see changes.`);
    } catch (e) {
      setError(String(e));
      setCommitting(false);
    }
  }

  return (
    <div className="backfill-overlay" onClick={onClose}>
      <div className="backfill-modal" onClick={(e) => e.stopPropagation()}>
        <div className="backfill-head">
          <strong>Classify client companies</strong>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {!res ? (
          <div className="muted" style={{ padding: "1rem" }}>
            🔎 scanning stubs for end-customer organisations…
          </div>
        ) : res.status === "error" ? (
          <div className="proposal-err">⚠ scan failed{res.error ? `: ${res.error}` : ""}</div>
        ) : (
          <div className="backfill-table-wrap">
            <p className="muted" style={{ margin: "0 0 0.75rem" }}>
              {res.candidates.length} stubs look like end-customers (only inbound
              HAS_CLIENT, no other signal). Uncheck any you want to leave alone,
              then commit to set their kind to "client".
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
          </div>
        )}

        {error && <div className="proposal-err">{error}</div>}

        <div className="backfill-foot">
          {done ? (
            <span className="proposal-done">{done}</span>
          ) : (
            <>
              <button className="commit" onClick={commit} disabled={committing || !approved.size}>
                {committing
                  ? "committing…"
                  : `Classify ${approved.size} as client`}
              </button>
              <button className="discard" onClick={onClose}>
                Cancel
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
