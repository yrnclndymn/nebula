import { commitClassification, getClassification, scanClassification } from "./api";
import { BatchReview } from "./BatchReview";
import type { Classification, ClassificationAction, ClassificationDecision } from "./types";
import { KINDS, kindLabel } from "./types";
import { useScanJob } from "./useScanJob";
import { useEffect, useState } from "react";

// Human-in-the-loop review for classifying end-customer stubs, folded into the
// Review inbox as a scan action (#153). A heuristic only *proposes* stubs
// (only-inbound-HAS_CLIENT, no other signal); nothing is written until the
// reviewer picks an action per row and commits. Shares the scan→poll lifecycle
// and the commit chrome (useScanJob + BatchReview) with the Resolve-stubs flow;
// this owns only the candidate list + per-candidate decision control.
//
// Broadened from client-only labelling (#158): each row is a per-item decision —
// relabel the stub to any company KIND, 'remove' it (a hard delete of a true
// stub, e.g. extraction junk), or leave it alone. The scan pre-selects a
// suggestion; 'remove' is irreversible, so a batch with removals confirms first.

// The row's working choice: a company KIND, a hard 'remove', or 'skip' (leave the
// stub untouched — the way to drop a candidate from the batch).
type Choice = ClassificationAction | "skip";

export function ClassifyBatch() {
  const { data: res, jobId } = useScanJob<Classification>(scanClassification, getClassification);
  const [choices, setChoices] = useState<Record<string, Choice>>({});

  // Seed each row from the scan's suggestion once the batch lands.
  useEffect(() => {
    if (!res || res.status !== "ready") return;
    setChoices(Object.fromEntries(res.candidates.map((c) => [c.name, c.suggested])));
  }, [res]);

  function set(name: string, choice: Choice) {
    setChoices((c) => ({ ...c, [name]: choice }));
  }

  // Build the commit batch from the current selections — 'skip' rows drop out.
  function decisions(): ClassificationDecision[] {
    if (!res || res.status !== "ready") return [];
    return res.candidates
      .map((c) => ({ name: c.name, action: choices[c.name] }))
      .filter((d): d is ClassificationDecision => d.action !== undefined && d.action !== "skip");
  }

  const ready = res !== null && res.status === "ready";
  const batch = decisions();
  const removeCount = batch.filter((d) => d.action === "remove").length;
  const kindCount = batch.length - removeCount;

  const body =
    ready && res ? (
      <>
        <p className="muted" style={{ margin: "0 0 0.75rem" }}>
          {res.candidates.length} stubs look like end-customers (only inbound HAS_CLIENT, no other
          signal). Pick an action per row — relabel to a company kind, remove the stub (irreversible),
          or leave it alone.
        </p>

        {res.candidates.length > 0 ? (
          <div className="proposal">
            <div className="proposal-head">
              <strong>Stub candidates</strong>
              <span className="muted">
                {kindCount} to relabel{removeCount ? ` · ${removeCount} to remove` : ""}
              </span>
            </div>
            <table>
              <tbody>
                {res.candidates.map((c) => {
                  const choice = choices[c.name] ?? "skip";
                  return (
                    <tr key={c.name} className={choice === "skip" ? "muted" : ""}>
                      <td className="name">
                        {c.name}{" "}
                        <span className="muted num" title={`${c.inbound} inbound HAS_CLIENT`}>
                          ·{c.inbound}
                        </span>
                      </td>
                      <td>
                        <select
                          value={choice}
                          className={choice === "remove" ? "classify-remove" : ""}
                          onChange={(e) => set(c.name, e.target.value as Choice)}
                          aria-label={`Action for ${c.name}`}
                        >
                          {KINDS.map((k) => (
                            <option key={k} value={k}>
                              {kindLabel(k)}
                            </option>
                          ))}
                          <option value="remove">Remove</option>
                          <option value="skip">Leave alone</option>
                        </select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No classification candidates found. 🎉</p>
        )}
      </>
    ) : null;

  const commitLabel =
    removeCount > 0
      ? `Apply ${kindCount} relabel${kindCount === 1 ? "" : "s"} + ${removeCount} remove${removeCount === 1 ? "" : "s"}`
      : `Relabel ${kindCount} stub${kindCount === 1 ? "" : "s"}`;

  return (
    <BatchReview
      state={res}
      scanningLabel="scanning stubs for end-customer organisations…"
      canCommit={batch.length > 0 && jobId !== null}
      commitLabel={commitLabel}
      confirmCommit={() =>
        removeCount === 0 ||
        window.confirm(
          `Permanently delete ${removeCount} stub${removeCount === 1 ? "" : "s"}? This can't be undone.`,
        )
      }
      onCommit={() => commitClassification(jobId!, batch)}
      doneMessage={(r) => {
        const refused = r.refused?.length ? `, ${r.refused.length} refused (researched)` : "";
        return `✓ relabelled ${r.classified ?? 0}, removed ${r.removed ?? 0}${refused}. Refresh to see changes.`;
      }}
    >
      {body}
    </BatchReview>
  );
}
