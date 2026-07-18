import { useEffect, useRef, useState } from "react";
import { sendChat } from "./api";
import type { Backfill, MergeProposal, Proposal } from "./types";
import { AcquisitionProposalCard } from "./AcquisitionProposals";
import { BackfillCard, BackfillModal } from "./BackfillReview";
import { MergeCard } from "./MergeCard";
import { ProposalCard } from "./ProposalCard";

interface JobRef {
  job_id: string;
  field: string;
  total: number;
}

// A chat-started acquisition proposal (#147): just the job id + company; the card
// polls /ma detail itself, so this is all the turn needs to carry.
interface AcquisitionRef {
  job_id: string;
  company: string;
}

interface Msg {
  role: "user" | "assistant";
  text: string;
  proposals?: Proposal[];
  backfills?: JobRef[];
  merges?: MergeProposal[];
  acquisitions?: AcquisitionRef[];
}

const SUGGESTIONS = [
  "Which companies partner with Anthropic?",
  "List employee-owned companies under 200 people.",
  "Research Cursor (cursor.com) and prepare it to add.",
];

export function ChatPanel({ onClose }: { onClose: () => void }) {
  const [sessionId] = useState(() => crypto.randomUUID());
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [reviewJob, setReviewJob] = useState<Backfill | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  // Acquisition cards remove themselves on commit/discard by calling onResolved;
  // in chat there's no list to prune, so track resolved job ids and hide them (the
  // card otherwise stays stuck "committing…" waiting for a parent that never drops it).
  const [resolvedAcq, setResolvedAcq] = useState<Record<string, boolean>>({});
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
          acquisitions: res.acquisitions,
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
            {m.acquisitions
              ?.filter((a) => !resolvedAcq[a.job_id])
              .map((a) => (
                <AcquisitionProposalCard
                  key={a.job_id}
                  row={{
                    job_id: a.job_id,
                    company: a.company,
                    status: "pending",
                    deal_count: 0,
                    new_count: 0,
                    outcome: null,
                    error: null,
                    committed: false,
                    created_at: null,
                  }}
                  onResolved={(jobId) =>
                    setResolvedAcq((r) => ({ ...r, [jobId]: true }))
                  }
                />
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
