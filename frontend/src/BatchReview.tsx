import { useState, type ReactNode } from "react";

// Shared batch-review template (#153). The scan→poll→commit review surfaces
// (Resolve stubs, Classify clients) all have the same shape: kick off a scan,
// poll it, show a reviewable batch, then commit the reviewer's decisions. This
// owns that common chrome — the scanning spinner, a scan-error line, and the
// commit footer with its committing/done/error states — while each flow supplies
// the scanned `state`, its own per-item body, and how to commit.
//
// Built with #158's per-item decisions in mind: the body is a free-form slot, so
// a per-row kind-select / remove control drops into the flow's body without any
// change to this shell — the shell only knows "there is a batch to commit".
export function BatchReview<T extends { status: string; error?: string }, R extends { error?: string }>({
  state,
  scanningLabel,
  children,
  commitLabel,
  canCommit,
  onCommit,
  doneMessage,
}: {
  state: T | null; // null while the scan is still running
  scanningLabel: string;
  children: ReactNode; // the reviewable body — rendered only once the scan is ready
  commitLabel: ReactNode;
  canCommit: boolean;
  onCommit: () => Promise<R>;
  doneMessage: (res: R) => string;
}) {
  const [committing, setCommitting] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function commit() {
    setCommitting(true);
    setError(null);
    try {
      const res = await onCommit();
      if (res.error) throw new Error(res.error);
      setDone(doneMessage(res));
      // Let the sidebar badge / inbox re-count now the batch is committed.
      window.dispatchEvent(new CustomEvent("nebula:review-changed"));
    } catch (e) {
      setError(String(e));
      setCommitting(false);
    }
  }

  if (!state) {
    return <div className="muted batch-scanning">🔎 {scanningLabel}</div>;
  }
  if (state.status === "error") {
    return (
      <div className="proposal-err">⚠ scan failed{state.error ? `: ${state.error}` : ""}</div>
    );
  }

  return (
    <div className="batch-review">
      {children}
      {error && <div className="proposal-err">{error}</div>}
      <div className="backfill-foot">
        {done ? (
          <span className="proposal-done">{done}</span>
        ) : (
          <button className="commit" onClick={commit} disabled={committing || !canCommit}>
            {committing ? "committing…" : commitLabel}
          </button>
        )}
      </div>
    </div>
  );
}
