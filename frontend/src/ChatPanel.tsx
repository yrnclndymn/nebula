import { useEffect, useRef, useState } from "react";
import { commitProposal, commitResolution, getProposal, sendChat } from "./api";
import type { Backfill, MergeProposal, Proposal, ScalarDiff } from "./types";
import { BackfillCard, BackfillModal } from "./BackfillReview";

interface JobRef {
  job_id: string;
  field: string;
  total: number;
}

interface Msg {
  role: "user" | "assistant";
  text: string;
  proposals?: Proposal[];
  backfills?: JobRef[];
  merges?: MergeProposal[];
}

const SUGGESTIONS = [
  "Which companies partner with Anthropic?",
  "List employee-owned companies under 200 people.",
  "Research Cursor (cursor.com) and prepare it to add.",
];

type CommitStatus = "idle" | "committing" | "committed" | "discarded";

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
export function ProposalCard({
  p: initial,
  onDiscard,
}: {
  p: Proposal;
  onDiscard?: () => void;
}) {
  const [prop, setProp] = useState<Proposal>(initial);
  const [primary, setPrimary] = useState<CommitStatus>("idle");
  const [others, setOthers] = useState<CommitStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  // Poll while the background research is still running.
  useEffect(() => {
    if (prop.status !== "pending") return;
    const iv = setInterval(async () => {
      try {
        const updated = await getProposal(prop.proposal_id);
        if (updated.status !== "pending") setProp(updated);
      } catch {
        /* transient — keep polling */
      }
    }, 2500);
    return () => clearInterval(iv);
  }, [prop.status, prop.proposal_id]);

  async function commit(scope: "focus" | "all", setState: (s: CommitStatus) => void) {
    setState("committing");
    setError(null);
    try {
      const res = await commitProposal(prop.proposal_id, scope);
      if (res.error) throw new Error(res.error);
      setState("committed");
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

export function ChatPanel({ onClose }: { onClose: () => void }) {
  const [sessionId] = useState(() => crypto.randomUUID());
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [reviewJob, setReviewJob] = useState<Backfill | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function send(text: string) {
    const q = text.trim();
    if (!q || loading) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: q }]);
    setLoading(true);
    try {
      const res = await sendChat(sessionId, q);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          text: res.reply,
          proposals: res.proposals,
          backfills: res.backfills,
          merges: res.merges,
        },
      ]);
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", text: "⚠ " + String(e) }]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <aside className="chat">
      <div className="chat-head">
        <span>Assistant</span>
        <button className="chat-close" onClick={onClose} aria-label="Close chat">
          ×
        </button>
      </div>

      <div className="chat-body">
        {messages.length === 0 && (
          <div className="chat-hint">
            <p>Ask about the research graph, or ask me to research and add a company.</p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {flash && <div className="chat-flash">{flash}</div>}
        {messages.map((m, i) => (
          <div key={i}>
            <div className={`msg msg-${m.role}`}>{m.text}</div>
            {m.proposals?.map((p) => (
              <ProposalCard key={p.proposal_id} p={p} />
            ))}
            {m.backfills?.map((b) => (
              <BackfillCard key={b.job_id} job={b} onReview={setReviewJob} />
            ))}
            {m.merges?.map((mg) => (
              <MergeCard key={mg.job_id} m={mg} />
            ))}
          </div>
        ))}
        {loading && <div className="msg msg-assistant thinking">thinking…</div>}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          placeholder="Ask a question…  (Enter to send)"
          rows={2}
        />
        <button onClick={() => send(input)} disabled={loading || !input.trim()}>
          Send
        </button>
      </div>

      {reviewJob && (
        <BackfillModal
          job={reviewJob}
          onClose={() => setReviewJob(null)}
          onCommitted={(n) => {
            setReviewJob(null);
            setFlash(`✓ committed ${n} ${reviewJob.field.label} values — refresh the table to see them.`);
          }}
        />
      )}
    </aside>
  );
}
