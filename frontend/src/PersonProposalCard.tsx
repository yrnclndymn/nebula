import { useCallback, useEffect, useRef, useState } from "react";
import { commitPersonProposal, dismissJob, enrichPerson, getPersonProposal } from "./api";
import { shapePersonDiff } from "./personDiff";
import type { PersonPriorRole, PersonProposal } from "./types";
import { isHttpUrl } from "./urls";
import { usePollJob } from "./usePollJob";

// The person-enrichment review card (#178): the SPA half of #40's propose→review→commit
// flow. A durable `person_proposal` job researches a person's bio/links/roles — every
// fact cited — and this card renders the ready proposal's diff + provenance, then
// commits (the ONLY write path) or discards. Poll-while-pending via `usePollJob`, exactly
// like ProposalCard. Every proposed string is graph/crawl-derived, so it renders as escaped
// text and only links out when the source URL is http(s).

type CommitState = "idle" | "committing" | "committed" | "discarded";

function roleLine(r: PersonPriorRole): string {
  const base = r.title ? `${r.title} at ${r.company}` : r.company;
  const span =
    r.from_year || r.to_year ? ` (${r.from_year ?? "?"}–${r.to_year ?? "?"})` : "";
  return base + span;
}

function ScalarRow({ label, old, value }: { label: string; old: string | null; value: string }) {
  return (
    <div className="diff-row">
      <span className="diff-k">{label}</span>{" "}
      {old ? (
        <span>
          <span className="diff-old">{old}</span>
          <span className="diff-arrow"> → </span>
          <span className="diff-new">{value}</span>
        </span>
      ) : (
        <span className="diff-new">{value}</span>
      )}
    </div>
  );
}

// onDiscard: optional override for the Discard button. A host that persists the job
// (the inbox) passes a handler that dismisses the underlying job; otherwise Discard is
// a local hide (the card collapses). onCommitted: fires after a successful commit so a
// host can re-count its pending badge.
export function PersonProposalCard({
  jobId,
  name,
  initialStatus = "pending",
  onCommitted,
  onDiscard,
}: {
  jobId: string;
  name: string;
  initialStatus?: PersonProposal["status"];
  onCommitted?: () => void;
  onDiscard?: () => void;
}) {
  const [prop, setProp] = useState<PersonProposal | null>(null);
  const [status, setStatus] = useState<PersonProposal["status"]>(initialStatus);
  const [commitState, setCommitState] = useState<CommitState>("idle");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(
    async (cancelled?: () => boolean) => {
      try {
        const updated = await getPersonProposal(jobId);
        if (cancelled?.()) return;
        setProp(updated);
        setStatus(updated.status);
      } catch {
        /* transient — keep polling / leave the last state */
      }
    },
    [jobId],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll while the background research is still running.
  usePollJob(status === "pending", (cancelled) => refresh(cancelled));

  async function commit() {
    setCommitState("committing");
    setError(null);
    try {
      const res = await commitPersonProposal(jobId);
      if (res.error) throw new Error(res.error);
      setCommitState("committed");
      onCommitted?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setCommitState("idle");
    }
  }

  if (status === "pending") {
    return (
      <div className="proposal pending">
        🔎 researching <strong>{name}</strong>…{" "}
        <span className="muted">this can take a minute</span>
      </div>
    );
  }
  // A ready job mounted by a host (the inbox) hasn't fetched its detail yet — show a
  // loading line rather than flashing the "couldn't research" fallback.
  if (status !== "error" && prop === null) {
    return (
      <div className="proposal pending">
        <span className="muted">loading proposal for </span>
        <strong>{name}</strong>…
      </div>
    );
  }
  if (status === "error" || !prop?.record) {
    return (
      <div className="proposal">
        ⚠ couldn&rsquo;t research <strong>{name}</strong>
        {prop?.error ? `: ${prop.error}` : ""}
      </div>
    );
  }

  const r = prop.record;
  const shaped = shapePersonDiff(prop.diff);
  const nothingToCommit = shaped.changeCount === 0;

  if (commitState === "discarded") {
    return (
      <div className="proposal">
        <div className="proposal-head">
          <strong>{prop.name || name}</strong>
        </div>
        <div className="proposal-done muted">discarded</div>
      </div>
    );
  }

  return (
    <div className={`proposal ${commitState}`}>
      <div className="proposal-head">
        <strong>{prop.name || name}</strong>
        <span className={`origin ${prop.exists ? "" : "origin-agent"}`}>
          {prop.exists ? "updates person" : "new person details"}
        </span>
      </div>

      {nothingToCommit ? (
        <div className="muted small">No new cited facts found to add.</div>
      ) : (
        <>
          {shaped.updated.length > 0 && (
            <div className="diff-group">
              <div className="diff-group-h">Updated values</div>
              {shaped.updated.map((s) => (
                <ScalarRow key={s.field} label={s.label} old={s.old} value={s.value} />
              ))}
            </div>
          )}
          {shaped.added.length > 0 && (
            <div className="diff-group">
              <div className="diff-group-h">Newly sourced</div>
              {shaped.added.map((s) => (
                <ScalarRow key={s.field} label={s.label} old={s.old} value={s.value} />
              ))}
            </div>
          )}
          {shaped.talks.length > 0 && (
            <div className="diff-group">
              <div className="diff-group-h">Talks (+{shaped.talks.length})</div>
              <div className="name-chips">
                {shaped.talks.map((t, i) =>
                  isHttpUrl(t) ? (
                    <a key={t} href={t} target="_blank" rel="noreferrer" className="chip">
                      talk {i + 1} ↗
                    </a>
                  ) : (
                    <span key={t} className="chip">
                      talk {i + 1}
                    </span>
                  ),
                )}
              </div>
            </div>
          )}
          {shaped.priorRoles.length > 0 && (
            <div className="diff-group">
              <div className="diff-group-h">Prior roles (+{shaped.priorRoles.length})</div>
              <ul className="people">
                {shaped.priorRoles.map((role, i) => (
                  <li key={`${role.company}-${role.title ?? ""}-${i}`}>
                    {roleLine(role)}
                    {isHttpUrl(role.source) && (
                      <a href={role.source} target="_blank" rel="noreferrer" className="diff-src">
                        {" "}
                        ↗
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {r.citations.length > 0 && (
        <details className="proposal-sources">
          <summary>{r.citations.length} sources</summary>
          <ul>
            {r.citations.map((c, i) => (
              <li key={`${c.field}-${c.source}-${i}`}>
                <span className="src-field">{c.field}</span>: {c.value}
                {isHttpUrl(c.source) && (
                  <a href={c.source} target="_blank" rel="noreferrer">
                    {" "}
                    ↗
                  </a>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}

      {error && <div className="proposal-err">{error}</div>}

      <div className="proposal-foot">
        <div className="proposal-actions">
          {commitState === "committed" ? (
            <span className="proposal-done">✓ changes committed</span>
          ) : (
            <>
              <button
                className="commit"
                disabled={commitState === "committing" || nothingToCommit}
                onClick={commit}
              >
                {commitState === "committing" ? "committing…" : "Commit changes"}
              </button>
              <button
                className="discard"
                onClick={() => (onDiscard ? onDiscard() : setCommitState("discarded"))}
              >
                Discard
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// The trigger (#178): start a person-enrichment proposal, then render its review card
// inline. Reused by PersonDrawer and CompanyDrawer's leadership entries. `company`
// scopes which person (their LEADS edge) — the caller supplies a company the person
// actually leads, so the backend never 404s on an unknown pairing.
export function PersonResearchButton({
  name,
  company,
  label = "🔎 Research this person",
  className = "person-research-btn",
  autoStart = false,
}: {
  name: string;
  company: string;
  label?: string;
  className?: string;
  autoStart?: boolean; // begin research on mount (a host that already gestured "research")
}) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [phase, setPhase] = useState<"idle" | "starting" | "error">("idle");
  const [note, setNote] = useState("");

  const start = useCallback(async () => {
    setPhase("starting");
    setNote("");
    try {
      const res = await enrichPerson(name, company);
      if (!res.job_id) throw new Error("could not start research");
      setJobId(res.job_id);
      setPhase("idle");
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [name, company]);

  // Start once on mount when the host already gestured intent (a leadership entry's
  // 🔎 toggle) — never re-fire on re-render, so `start` stays out of the deps.
  const started = useRef(false);
  useEffect(() => {
    if (autoStart && !started.current) {
      started.current = true;
      void start();
    }
  }, [autoStart, start]);

  if (jobId) {
    return (
      <PersonProposalCard
        jobId={jobId}
        name={name}
        onDiscard={() => {
          void dismissJob(jobId).catch(() => {
            /* best-effort — the card unmounts regardless */
          });
          setJobId(null);
        }}
      />
    );
  }

  return (
    <div className="person-research-trigger">
      <button type="button" className={className} onClick={start} disabled={phase === "starting"}>
        {phase === "starting" ? "Starting…" : label}
      </button>
      {note && <span className="muted error"> {note}</span>}
    </div>
  );
}
