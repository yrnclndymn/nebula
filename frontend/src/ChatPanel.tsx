import { useEffect, useRef, useState } from "react";
import { commitProposal, sendChat } from "./api";
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

type PropStatus = "pending" | "committing" | "committed" | "discarded";

function ProposalCard({ p }: { p: Proposal }) {
  const [status, setStatus] = useState<PropStatus>("pending");
  const [error, setError] = useState<string | null>(null);
  const r = p.record;

  async function commit() {
    setStatus("committing");
    setError(null);
    try {
      const res = await commitProposal(p.proposal_id);
      if (res.error) throw new Error(res.error);
      setStatus("committed");
    } catch (e) {
      setError(String(e));
      setStatus("pending");
    }
  }

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
    <div className={`proposal ${status}`}>
      <div className="proposal-head">
        <strong>{p.name}</strong>
        <span className={`origin ${p.exists ? "" : "origin-agent"}`}>
          {p.exists ? "updates existing" : "new"}
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
      {status === "committed" ? (
        <div className="proposal-done">✓ committed to the graph</div>
      ) : status === "discarded" ? (
        <div className="proposal-done muted">discarded</div>
      ) : (
        <div className="proposal-actions">
          <button className="commit" onClick={commit} disabled={status === "committing"}>
            {status === "committing" ? "committing…" : "Commit"}
          </button>
          <button className="discard" onClick={() => setStatus("discarded")}>
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
