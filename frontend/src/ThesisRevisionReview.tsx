import { useEffect, useState } from "react";
import { commitThesisRevision, getThesisRevision, scanThesisRevision } from "./api";
import { BatchReview } from "./BatchReview";
import { confidenceLabel, thesisPair } from "./thesis";
import type { ThesisChange, ThesisRevision, ThesisRevisionDecision } from "./types";
import { isHttpUrl } from "./urls";
import { useScanJob } from "./useScanJob";

// Thesis evidence loop (#196, epic #192), folded into the Review inbox as a scan
// action alongside Resolve/Classify. The scan reads the observed ACQUIRED deals +
// the current rules and proposes revisions via ONE Gemini call; NOTHING is written
// until the reviewer approves changes and commits. Shares the scan→poll lifecycle
// and commit chrome (useScanJob + BatchReview); this owns only the per-change
// revision-diff body and the per-change approve/skip control.
//
// Every proposed change carries the deals it rests on (the backend drops any change
// with no cited http(s) source), so a confidence move is never uncited. Deal thesis
// text + names are untrusted crawled data — rendered escaped, links only when http(s).

const CHANGE_LABEL: Record<ThesisChange["change_kind"], string> = {
  support: "Strengthen",
  weaken: "Weaken",
  new: "New rule",
  refine: "Refine",
};

function ConfidenceDelta({ change }: { change: ThesisChange }) {
  if (change.old_confidence === null) {
    return <span className="small">confidence {confidenceLabel(change.new_confidence)}</span>;
  }
  return (
    <span className="small">
      {confidenceLabel(change.old_confidence)} → {confidenceLabel(change.new_confidence)}
    </span>
  );
}

function ChangeRow({
  change,
  approved,
  onToggle,
}: {
  change: ThesisChange;
  approved: boolean;
  onToggle: (change_id: string, approve: boolean) => void;
}) {
  const sources = change.evidence.filter((e) => isHttpUrl(e.source));
  return (
    <div className={approved ? "proposal" : "proposal muted"}>
      <div className="proposal-head">
        <span className={`activity-badge ${change.change_kind === "weaken" ? "err" : "run"}`}>
          {CHANGE_LABEL[change.change_kind]}
        </span>
        <strong>{change.statement}</strong>
        <label className="small" style={{ marginLeft: "auto" }}>
          <input
            type="checkbox"
            checked={approved}
            onChange={(e) => onToggle(change.change_id, e.target.checked)}
            aria-label={`Approve change: ${change.statement}`}
          />{" "}
          approve
        </label>
      </div>
      <div className="thesis-meta">
        <span className="thesis-pair muted small">{thesisPair(change)}</span>
        {change.qualifier && (
          <span className="thesis-qualifier muted small">· {change.qualifier}</span>
        )}
        <ConfidenceDelta change={change} />
      </div>
      {change.rationale && <div className="muted small">{change.rationale}</div>}
      <div className="thesis-evidence muted small">
        <span>
          {change.evidence.length} supporting deal{change.evidence.length === 1 ? "" : "s"}:
        </span>{" "}
        {change.evidence.map((e, i) => (
          <span key={`${change.change_id}-ev${i}`}>
            {i > 0 && "; "}
            {e.acquirer ?? "?"} → {e.target ?? "?"}{" "}
            {isHttpUrl(e.source) && (
              <a href={e.source} target="_blank" rel="noreferrer">
                source ↗
              </a>
            )}
          </span>
        ))}
        {sources.length === 0 && <span> (no citable source)</span>}
      </div>
    </div>
  );
}

export function ReviseThesisBatch() {
  const { data: res, jobId } = useScanJob<ThesisRevision>(scanThesisRevision, getThesisRevision);
  // change_id → approved. Un-checked by default: the reviewer opts each change in.
  const [approved, setApproved] = useState<Record<string, boolean>>({});

  // Seed every proposed change as approved once the batch lands (the common case is
  // "accept the evidence-backed revisions"); the reviewer un-checks any to skip.
  useEffect(() => {
    if (!res || res.status !== "ready") return;
    setApproved(Object.fromEntries(res.changes.map((c) => [c.change_id, true])));
  }, [res]);

  function toggle(change_id: string, approve: boolean) {
    setApproved((a) => ({ ...a, [change_id]: approve }));
  }

  function decisions(): ThesisRevisionDecision[] {
    if (!res || res.status !== "ready") return [];
    return res.changes.map((c) => ({
      change_id: c.change_id,
      action: approved[c.change_id] ? "approve" : "skip",
    }));
  }

  const ready = res !== null && res.status === "ready";
  const approvedCount = res?.changes.filter((c) => approved[c.change_id]).length ?? 0;

  const body =
    ready && res ? (
      <>
        <p className="muted" style={{ margin: "0 0 0.75rem" }}>
          {res.changes.length} proposed change{res.changes.length === 1 ? "" : "s"} from{" "}
          {res.deal_count} observed deal{res.deal_count === 1 ? "" : "s"} across {res.rule_count}{" "}
          rule{res.rule_count === 1 ? "" : "s"}. Approve the evidence-backed revisions to apply; each
          writes with the supporting deals attached.
        </p>
        {res.changes.length > 0 ? (
          res.changes.map((c) => (
            <ChangeRow
              key={c.change_id}
              change={c}
              approved={!!approved[c.change_id]}
              onToggle={toggle}
            />
          ))
        ) : (
          <p className="muted">No thesis revisions proposed from the current evidence. 🎉</p>
        )}
      </>
    ) : null;

  return (
    <BatchReview
      state={res}
      scanningLabel="reading observed deals and weighing them against the thesis…"
      canCommit={approvedCount > 0 && jobId !== null}
      commitLabel={`Apply ${approvedCount} revision${approvedCount === 1 ? "" : "s"}`}
      onCommit={() => commitThesisRevision(jobId!, decisions())}
      doneMessage={(r) =>
        `✓ applied ${r.applied ?? 0} thesis revision${(r.applied ?? 0) === 1 ? "" : "s"}. Open the M&A tab to see the updated thesis.`
      }
    >
      {body}
    </BatchReview>
  );
}
