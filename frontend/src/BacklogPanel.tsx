import { useEffect, useMemo, useState } from "react";
import { fetchBacklog, getProposal, researchBacklog } from "./api";
import type { BacklogRow, Proposal } from "./types";
import { ProposalCard } from "./ChatPanel";

// Server-side sanity cap: at most this many companies per "Research selected"
// request (mirrors MAX_BACKLOG_RESEARCH on the backend). The UI enforces the same
// ceiling so the user can't build a selection the server will reject.
const MAX_SELECT = 10;

type Emphasis = "score" | "client" | "partner";

// Research backlog page (issue #31): the ranked list of un-researched stubs with
// their score components, simple filters, multi-select, and a "Research selected"
// trigger that runs each stub through the durable propose→review→commit flow.
// Nothing is written to the graph until the user commits each proposal here (HITL).
export function BacklogModal({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<BacklogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [minMentions, setMinMentions] = useState(1);
  const [emphasis, setEmphasis] = useState<Emphasis>("score");

  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Proposals keyed by the backlog name they were triggered for.
  const [triggered, setTriggered] = useState<Record<string, Proposal>>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    fetchBacklog()
      .then((r) => !stop && setRows(r))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, []);

  // Poll the triggered proposals until each resolves (ready/error). ProposalCard
  // does its own polling, but we poll here too so the table row's status badge
  // stays live; the card is only rendered once a proposal is no longer pending, so
  // there is no duplicate polling of the same proposal.
  useEffect(() => {
    const pending = Object.values(triggered).filter((p) => p.status === "pending");
    if (pending.length === 0) return;
    const iv = setInterval(() => {
      pending.forEach(async (p) => {
        try {
          const updated = await getProposal(p.proposal_id);
          if (updated.status !== "pending") {
            setTriggered((t) => ({ ...t, [p.name]: { ...updated, name: p.name } }));
          }
        } catch {
          /* transient — keep polling */
        }
      });
    }, 2500);
    return () => clearInterval(iv);
  }, [triggered]);

  const view = useMemo(() => {
    const filtered = rows.filter((r) => r.mention_count >= minMentions);
    const cmp: Record<Emphasis, (a: BacklogRow, b: BacklogRow) => number> = {
      score: (a, b) => b.rank_score - a.rank_score || b.mention_count - a.mention_count,
      client: (a, b) => b.client_mentions - a.client_mentions || b.rank_score - a.rank_score,
      partner: (a, b) =>
        b.partner_mentions - a.partner_mentions ||
        b.cloud_isv_partner_mentions - a.cloud_isv_partner_mentions ||
        b.rank_score - a.rank_score,
    };
    return [...filtered].sort((a, b) => cmp[emphasis](a, b) || a.name.localeCompare(b.name));
  }, [rows, minMentions, emphasis]);

  function toggle(name: string) {
    setNotice(null);
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(name)) {
        next.delete(name);
      } else if (next.size >= MAX_SELECT) {
        setNotice(`You can research at most ${MAX_SELECT} companies at a time.`);
        return s;
      } else {
        next.add(name);
      }
      return next;
    });
  }

  async function research() {
    if (!selected.size || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const res = await researchBacklog([...selected]);
      setTriggered((t) => {
        const next = { ...t };
        for (const { name, proposal_id } of res.proposals) {
          next[name] = { proposal_id, name, status: "pending" };
        }
        return next;
      });
      setSelected(new Set());
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  }

  const reviewList = Object.values(triggered).filter(
    (p) => p.status === "ready" || p.status === "error",
  );

  return (
    <div className="backfill-overlay" onClick={onClose}>
      <div className="backfill-modal backlog-modal" onClick={(e) => e.stopPropagation()}>
        <div className="backfill-head">
          <strong>Research backlog</strong>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="backlog-filters">
          <label>
            Min mentions
            <input
              type="number"
              min={1}
              value={minMentions}
              onChange={(e) => setMinMentions(Math.max(1, Number(e.target.value) || 1))}
            />
          </label>
          <label>
            Emphasis
            <select value={emphasis} onChange={(e) => setEmphasis(e.target.value as Emphasis)}>
              <option value="score">Balanced (score)</option>
              <option value="client">Client-of</option>
              <option value="partner">Partner-of</option>
            </select>
          </label>
          <span className="muted small">
            Select up to {MAX_SELECT} un-researched stubs, then research them. Results come back as
            proposals to review and commit below — nothing is saved automatically.
          </span>
        </div>

        <div className="backfill-table-wrap">
          {error ? (
            <div className="proposal-err">⚠ couldn't load the backlog: {error}</div>
          ) : loading ? (
            <div className="muted" style={{ padding: "1rem" }}>
              loading backlog…
            </div>
          ) : view.length === 0 ? (
            <p className="muted" style={{ padding: "1rem" }}>
              No un-researched stubs match this filter. 🎉
            </p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th></th>
                  <th>Company</th>
                  <th className="num" title="client_mentions + partner_mentions + boosted cloud/ISV partners">
                    Score
                  </th>
                  <th className="num">Mentions</th>
                  <th className="num" title="Distinct researched companies that name it as a client">
                    Client-of
                  </th>
                  <th className="num" title="Distinct researched companies that name it as a partner">
                    Partner-of
                  </th>
                  <th className="num" title="Partners that are cloud providers / ISVs (score-boosted)">
                    Cloud/ISV
                  </th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {view.map((r) => {
                  const p = triggered[r.name];
                  return (
                    <tr key={r.name}>
                      <td>
                        {!p && (
                          <input
                            type="checkbox"
                            checked={selected.has(r.name)}
                            onChange={() => toggle(r.name)}
                            aria-label={`Select ${r.name}`}
                          />
                        )}
                      </td>
                      <td>{r.name}</td>
                      <td className="num">
                        <strong>{r.rank_score}</strong>
                      </td>
                      <td className="num">{r.mention_count}</td>
                      <td className="num">{r.client_mentions}</td>
                      <td className="num">{r.partner_mentions}</td>
                      <td className="num">{r.cloud_isv_partner_mentions || "—"}</td>
                      <td>{p ? <StatusBadge status={p.status} /> : <span className="muted">—</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {reviewList.length > 0 && (
            <div className="backlog-review">
              <div className="diff-group-h">Proposals to review</div>
              {reviewList.map((p) => (
                <ProposalCard key={p.proposal_id} p={p} />
              ))}
            </div>
          )}
        </div>

        {notice && <div className="proposal-err">{notice}</div>}

        <div className="backfill-foot">
          <button className="commit" onClick={research} disabled={busy || !selected.size}>
            {busy ? "starting…" : `Research selected (${selected.size})`}
          </button>
          <button className="discard" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: Proposal["status"] }) {
  if (status === "ready") return <span className="proposal-done">ready</span>;
  if (status === "error") return <span className="proposal-err">error</span>;
  return <span className="muted">researching…</span>;
}
