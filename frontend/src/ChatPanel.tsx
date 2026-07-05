import { useEffect, useRef, useState } from "react";
import { sendChat } from "./api";

interface Msg {
  role: "user" | "assistant";
  text: string;
}

const SUGGESTIONS = [
  "Which companies partner with Anthropic?",
  "List employee-owned companies under 200 people.",
  "Who leads Moderne?",
];

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
      const { reply } = await sendChat(sessionId, q);
      setMessages((m) => [...m, { role: "assistant", text: reply }]);
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
            <p>Ask about the research graph. It queries the same graph you see in the table.</p>
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
          <div key={i} className={`msg msg-${m.role}`}>
            {m.text}
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
