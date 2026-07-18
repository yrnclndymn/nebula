import { useState } from "react";
import { commitResolution } from "./api";
import type { MergeProposal } from "./types";
import type { CommitStatus } from "./ProposalCard";

// A user-named merge the assistant proposed (issue #64). The named companies are
// shown with the survivor called out; nothing merges until the user commits — the
// commit reuses the resolution endpoint (the assistant can never merge directly).
export function MergeCard({ m }: { m: MergeProposal }) {
  const [status, setStatus] = useState<CommitStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const variants = m.members.filter((mem) => mem.name !== m.canonical);

  async function commit() {
    setStatus("committing");
    setError(null);
    try {
      const res = await commitResolution(m.job_id, [
        { action: "merge", canonical: m.canonical, variants: variants.map((v) => v.name) },
      ]);
      if (res.error) throw new Error(res.error);
      setStatus("committed");
    } catch (e) {
      setError(String(e));
      setStatus("idle");
    }
  }

  return (
    <div className={`proposal ${status}`}>
      <div className="proposal-head">
        <strong>Merge {m.members.length} records</strong>
        <span className="tag">duplicate</span>
      </div>

      <div className="diff-group">
        <div className="diff-group-h">Keep</div>
        <div className="diff-row">
          <strong>{m.canonical}</strong>
          {m.members.find((mem) => mem.name === m.canonical)?.researched && (
            <span className="muted small"> · researched</span>
          )}
        </div>
      </div>

      <div className="diff-group">
        <div className="diff-group-h">Merge in &amp; keep as aliases</div>
        {variants.map((v) => (
          <div key={v.name} className="diff-row">
            <span className="diff-old">{v.name}</span>
            {v.researched && <span className="muted small"> · researched</span>}
            <span className="muted num"> {v.edges} edges</span>
          </div>
        ))}
      </div>

      <div className="muted small">
        Edges and sources re-point onto <strong>{m.canonical}</strong>; its own values are kept and
        the others fill any gaps. Irreversible — review before committing.
      </div>
      {m.canonical_reason && <div className="muted small">ℹ {m.canonical_reason}</div>}

      {error && <div className="proposal-err">{error}</div>}

      {status === "discarded" ? (
        <div className="proposal-done muted">discarded</div>
      ) : (
        <div className="proposal-foot">
          <div className="proposal-actions">
            {status === "committed" ? (
              <span className="proposal-done">✓ merged into {m.canonical}</span>
            ) : (
              <>
                <button className="commit" disabled={status === "committing"} onClick={commit}>
                  {status === "committing" ? "merging…" : "Commit merge"}
                </button>
                <button className="discard" onClick={() => setStatus("discarded")}>
                  Discard
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
