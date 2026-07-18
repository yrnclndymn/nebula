import { useEffect, useState } from "react";
import { commitProposal, fetchTopics, getProposal } from "./api";
import type { Proposal, ScalarDiff } from "./types";
import { usePollJob } from "./usePollJob";

export type CommitStatus = "idle" | "committing" | "committed" | "discarded";

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

function ScalarValue({ s }: { s: ScalarDiff }) {
  if (s.status === "changed" && s.old !== null && s.old !== undefined && s.old !== "") {
    return (
      <span>
        <span className="diff-old">{fmt(s.old)}</span>
        <span className="diff-arrow"> → </span>
        <span className="diff-new">{fmt(s.new)}</span>
      </span>
    );
  }
  return <span className="diff-new">{fmt(s.new)}</span>;
}

function ChipGroup({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="diff-group">
      <div className="diff-group-h">{title}</div>
      <div className="name-chips">
        {items.map((name) => (
          <span key={name} className="chip">
            {name}
          </span>
        ))}
      </div>
    </div>
  );
}

// onDiscard: optional override for the Discard button. In chat, discard is a
// local hide (the card just collapses); host pages that persist activity (the
// backlog) pass a handler that actually dismisses the underlying job (#73).
// onCommitted: fires after a successful commit so a host (the Review inbox) can
// re-count its pending badge.
// existingTopics: the graph's current topics — when passed, the card flags a
// proposal whose topic isn't among them ("⚠ creates new topic", the UI half of
// #148). When omitted it fetches them itself, so the flag also shows in chat.
export function ProposalCard({
  p: initial,
  onDiscard,
  onCommitted,
  existingTopics,
}: {
  p: Proposal;
  onDiscard?: () => void;
  onCommitted?: () => void;
  existingTopics?: string[];
}) {
  const [prop, setProp] = useState<Proposal>(initial);
  const [primary, setPrimary] = useState<CommitStatus>("idle");
  const [others, setOthers] = useState<CommitStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [knownTopics, setKnownTopics] = useState<string[] | null>(existingTopics ?? null);

  // Fall back to fetching the topic list ourselves when a host didn't supply it,
  // so the new-topic flag shows anywhere a ready proposal is reviewed.
  useEffect(() => {
    if (existingTopics) {
      setKnownTopics(existingTopics);
      return;
    }
    if (!prop.topic) return;
    let stop = false;
    fetchTopics()
      .then((t) => !stop && setKnownTopics(t))
      .catch(() => {
        /* best-effort — no flag if topics can't be read */
      });
    return () => {
      stop = true;
    };
  }, [existingTopics, prop.topic]);

  // Poll while the background research is still running.
  usePollJob(prop.status === "pending", async (cancelled) => {
    try {
      const updated = await getProposal(prop.proposal_id);
      if (cancelled()) return;
      if (updated.status !== "pending") setProp(updated);
    } catch {
      /* transient — keep polling */
    }
  });

  async function commit(scope: "focus" | "all", setState: (s: CommitStatus) => void) {
    setState("committing");
    setError(null);
    try {
      const res = await commitProposal(prop.proposal_id, scope);
      if (res.error) throw new Error(res.error);
      setState("committed");
      onCommitted?.();
    } catch (e) {
      setError(String(e));
      setState("idle");
    }
  }

  if (prop.status === "pending") {
    return (
      <div className="proposal pending">
        🔎 researching <strong>{prop.name}</strong>…{" "}
        <span className="muted">this can take a minute</span>
      </div>
    );
  }
  if (prop.status === "error" || !prop.record || !prop.diff) {
    return (
      <div className="proposal">
        ⚠ couldn't research <strong>{prop.name}</strong>
        {prop.error ? `: ${prop.error}` : ""}
      </div>
    );
  }

  const r = prop.record;
  const diff = prop.diff;
  // #148 UI half: the commit MERGEs the proposal's topic, silently minting a new
  // Topic node for any novel string. Flag it when it isn't an existing topic so
  // the reviewer catches an invented one (e.g. request phrasing) before committing.
  // Guard on a non-empty topic list: an empty one usually means the read failed,
  // and flagging every proposal as a new topic would be worse than staying quiet.
  const newTopic =
    prop.topic && knownTopics && knownTopics.length > 0 && !knownTopics.includes(prop.topic)
      ? prop.topic
      : null;
  const focusKey = prop.focus_key ?? null;
  const focusLabel = prop.focus_label || "";
  const focusMode = !!focusKey;

  const focusScalar = focusKey ? diff.scalars.find((s) => s.key === focusKey) : undefined;
  const rest = diff.scalars.filter((s) => s !== focusScalar);
  const changed = rest.filter((s) => s.status === "changed");
  const created = rest.filter((s) => s.status === "new");
  const unchanged = rest.filter((s) => s.status === "same");
  const lead = diff.leadership;

  // Committable "other" work beyond the focus field (variants are flagged, not written).
  const otherUpdates =
    changed.length +
    created.length +
    diff.clients.added.length +
    diff.partners.added.length +
    lead.added.length +
    lead.merged.length;

  const focusSrc = focusKey
    ? r.citations.find(
        (c) => c.field.toLowerCase().replace(/[ _-]/g, "") === focusKey.replace(/[ _-]/g, ""),
      )
    : undefined;

  // The changed/new/relationship groups — shown inline for a general update, or
  // tucked into a collapsible for a focused one.
  const groups = (
    <>
      {changed.length > 0 && (
        <div className="diff-group">
          <div className="diff-group-h">Updated values</div>
          {changed.map((s) => (
            <div key={s.key} className="diff-row">
              <span className="diff-k">{s.label}</span> <ScalarValue s={s} />
            </div>
          ))}
        </div>
      )}
      {created.length > 0 && (
        <div className="diff-group">
          <div className="diff-group-h">Newly sourced</div>
          {created.map((s) => (
            <div key={s.key} className="diff-row">
              <span className="diff-k">{s.label}</span> <ScalarValue s={s} />
            </div>
          ))}
        </div>
      )}
      {diff.clients.added.length > 0 && (
        <ChipGroup title={`Clients (+${diff.clients.added.length})`} items={diff.clients.added} />
      )}
      {diff.partners.added.length > 0 && (
        <ChipGroup title={`Partners (+${diff.partners.added.length})`} items={diff.partners.added} />
      )}
      {lead.added.length > 0 && (
        <ChipGroup
          title={`Leadership (+${lead.added.length})`}
          items={lead.added.map((l) => (l.title ? `${l.name} — ${l.title}` : l.name))}
        />
      )}
      {lead.merged.length > 0 && (
        <div className="diff-group">
          <div className="diff-group-h">Merged duplicates</div>
          {lead.merged.map((m, i) => (
            <div key={i} className="diff-merge">
              <span className="diff-old">{m.proposed}</span>
              <span className="diff-arrow"> → </span>
              {m.canonical}
            </div>
          ))}
        </div>
      )}
    </>
  );

  return (
    <div className={`proposal ${primary}`}>
      <div className="proposal-head">
        <strong>{prop.name}</strong>
        <span className={`origin ${prop.exists ? "" : "origin-agent"}`}>
          {prop.exists ? "updates existing" : "new company"}
        </span>
      </div>

      {newTopic && (
        <div className="proposal-newtopic">
          ⚠ creates new topic: <strong>{newTopic}</strong>
          <span className="muted small">
            {" "}
            — not an existing research domain; check it isn&rsquo;t stray request phrasing.
          </span>
        </div>
      )}

      {/* Backlog stubs have no website: the job discovered one to research from. */}
      {prop.discovered_website && (
        <div className="muted small">
          🌐 discovered site:{" "}
          <a href={`https://${prop.discovered_website}`} target="_blank" rel="noreferrer">
            {prop.discovered_website}
          </a>
        </div>
      )}

      {focusMode ? (
        focusScalar ? (
          <div className="diff-focus">
            <div className="diff-focus-label">{focusScalar.label}</div>
            <div className="diff-focus-val">
              <ScalarValue s={focusScalar} />
              {focusSrc && (
                <a href={focusSrc.source} target="_blank" rel="noreferrer" className="diff-src">
                  ↗
                </a>
              )}
            </div>
            {focusScalar.status === "same" && (
              <div className="muted small">matches the value already on record</div>
            )}
          </div>
        ) : (
          <div className="diff-focus empty">No {focusLabel || "value"} found in the sources.</div>
        )
      ) : (
        groups
      )}

      {/* Uncertain name variants always surface — they are NOT written automatically. */}
      {lead.variants.length > 0 && (
        <div className="diff-variants">
          <div className="diff-group-h warn">⚠ Possible duplicate people — review</div>
          {lead.variants.map((v, i) => (
            <div key={i} className="diff-variant">
              <strong>{v.name}</strong>
              {v.title ? ` — ${v.title}` : ""} · possibly the same as{" "}
              <strong>{v.possibly}</strong>
            </div>
          ))}
          <div className="muted small">
            Not written automatically. Add manually if they're different people.
          </div>
        </div>
      )}

      {focusMode && otherUpdates > 0 && (
        <details className="proposal-sources">
          <summary>{otherUpdates} other change{otherUpdates > 1 ? "s" : ""} found</summary>
          <div className="diff-other">{groups}</div>
        </details>
      )}

      {unchanged.length > 0 && (
        <details className="proposal-sources">
          <summary>{unchanged.length} unchanged</summary>
          <ul>
            {unchanged.map((s) => (
              <li key={s.key}>
                {s.label}: {fmt(s.new)}
              </li>
            ))}
          </ul>
        </details>
      )}

      {r.citations.length > 0 && (
        <details className="proposal-sources">
          <summary>{r.citations.length} sources</summary>
          <ul>
            {r.citations.map((c, i) => (
              <li key={i}>
                <span className="src-field">{c.field}</span>: {c.value}{" "}
                <a href={c.source} target="_blank" rel="noreferrer">
                  ↗
                </a>
              </li>
            ))}
          </ul>
        </details>
      )}

      {error && <div className="proposal-err">{error}</div>}

      {primary === "discarded" ? (
        <div className="proposal-done muted">discarded</div>
      ) : (
        <div className="proposal-foot">
          <div className="proposal-actions">
            {primary === "committed" ? (
              <span className="proposal-done">
                ✓ {focusMode ? `${focusLabel} committed` : "changes committed"}
              </span>
            ) : (
              <>
                <button
                  className="commit"
                  disabled={primary === "committing" || (focusMode && !focusScalar)}
                  onClick={() => commit(focusMode ? "focus" : "all", setPrimary)}
                >
                  {primary === "committing"
                    ? "committing…"
                    : focusMode
                      ? `Commit ${focusLabel}`
                      : "Commit all changes"}
                </button>
                <button className="discard" onClick={() => (onDiscard ? onDiscard() : setPrimary("discarded"))}>
                  Discard
                </button>
              </>
            )}
          </div>
          {focusMode &&
            otherUpdates > 0 &&
            (others === "committed" ? (
              <div className="proposal-done small">
                ✓ {otherUpdates} other update{otherUpdates > 1 ? "s" : ""} applied
              </div>
            ) : (
              <button
                className="apply-others"
                disabled={others === "committing"}
                onClick={() => commit("all", setOthers)}
              >
                {others === "committing"
                  ? "applying…"
                  : `＋ apply ${otherUpdates} other update${otherUpdates > 1 ? "s" : ""}`}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}
