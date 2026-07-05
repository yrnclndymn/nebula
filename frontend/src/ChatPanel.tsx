import { useEffect, useRef, useState } from "react";
import { commitProposal, getProposal, sendChat } from "./api";
import type { Proposal } from "./types";

interface Msg {
  role: "user" | "assistant";
  text: string;
  proposals?: Proposal[];
}

const SUGGESTIONS = [
  "Which companies partner with Anthropic?",
  "List employee-owned companies under 200 people.",
  "Research Cursor (cursor.com) and prepare it to add.",
];

type CommitStatus = "idle" | "committing" | "committed" | "discarded";

function ProposalCard({ p: initial }: { p: Proposal }) {
  const [prop, setProp] = useState<Proposal>(initial);
  const [commitState, setCommitState] = useState<CommitStatus>("idle");
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

  async function commit() {
    setCommitState("committing");
    setError(null);
    try {
      const res = await commitProposal(prop.proposal_id);
      if (res.error) throw new Error(res.error);
      setCommitState("committed");
    } catch (e) {
      setError(String(e));
      setCommitState("idle");
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
  if (prop.status === "error" || !prop.record) {
    return (
      <div className="proposal">
        ⚠ couldn't research <strong>{prop.name}</strong>
        {prop.error ? `: ${prop.error}` : ""}
      </div>
    );
  }

  const r = prop.record;
  const facts: [string, unknown][] = [
    ["HQ", r.hq_location],
    ["Founded", r.year_founded],
    ["Headcount", r.headcount],
    ["Funding", r.funding],
    ["Revenue", r.estimated_revenue],
    ["Types", r.company_types.join(", ") || null],
  ];
  const leaders = r.leadership.map((l) => (l.title ? `${l.name} — ${l.title}` : l.name));
  const lists: [string, string[]][] = [
    ["Clients", r.clients],
    ["Partners", r.partnerships],
    ["Leadership", leaders],
  ];

  return (
    <div className={`proposal ${commitState}`}>
      <div className="proposal-head">
        <strong>{prop.name}</strong>
        <span className={`origin ${prop.exists ? "" : "origin-agent"}`}>
          {prop.exists ? "updates existing" : "new"}
        </span>
      </div>
      <div className="proposal-facts">
        {facts
          .filter(([, v]) => v !== null && v !== undefined && v !== "")
          .map(([k, v]) => (
            <div key={k}>
              <span className="pf-k">{k}</span> {String(v)}
            </div>
          ))}
      </div>
      {lists
        .filter(([, items]) => items.length > 0)
        .map(([label, items]) => (
          <details key={label} className="proposal-sources" open={label === "Clients"}>
            <summary>
              {items.length} {label.toLowerCase()}
            </summary>
            <div className="name-chips">
              {items.map((name) => (
                <span key={name} className="chip">
                  {name}
                </span>
              ))}
            </div>
          </details>
        ))}
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
      {commitState === "committed" ? (
        <div className="proposal-done">✓ committed to the graph</div>
      ) : commitState === "discarded" ? (
        <div className="proposal-done muted">discarded</div>
      ) : (
        <div className="proposal-actions">
          <button className="commit" onClick={commit} disabled={commitState === "committing"}>
            {commitState === "committing" ? "committing…" : "Commit"}
          </button>
          <button className="discard" onClick={() => setCommitState("discarded")}>
            Discard
          </button>
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
      setMessages((m) => [...m, { role: "assistant", text: res.reply, proposals: res.proposals }]);
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
        {messages.map((m, i) => (
          <div key={i}>
            <div className={`msg msg-${m.role}`}>{m.text}</div>
            {m.proposals?.map((p) => (
              <ProposalCard key={p.proposal_id} p={p} />
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
    </aside>
  );
}
