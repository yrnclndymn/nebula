import { useEffect, useState } from "react";
import { fetchRecentAcquisitions } from "./api";
import type { Acquisition } from "./types";

// The M&A page (issue #45, epic #26 M&A Intelligence): recent deals across the
// tracked space, newest announced first, filterable by topic (either endpoint in
// that topic) and by acquirer. Modal-as-page, same shell as the Digest / What's
// new panels. Read-only over the ACQUIRED edges written by the #43 propose→commit
// flow. Deal facts come from graph data; the `source`/`amount_source` URLs
// originate from crawled evidence (untrusted) so everything renders as escaped
// text and a link only appears when the URL is http(s). An amount is shown ONLY
// next to its `amount_source` citation — an uncited figure is never surfaced.

function isHttpUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

// A parseable date renders localised; otherwise keep the raw string (or nothing).
function whenLabel(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const t = Date.parse(raw);
  return Number.isNaN(t) ? raw : new Date(t).toLocaleDateString();
}

export function MAPage({ topics, onClose }: { topics: string[]; onClose: () => void }) {
  const [deals, setDeals] = useState<Acquisition[]>([]);
  const [topic, setTopic] = useState("");
  const [acquirer, setAcquirer] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Topic is a server-side filter (needs the graph); acquirer is filtered client
  // -side over the loaded rows so typing is instant and doesn't refetch.
  useEffect(() => {
    let stop = false;
    setLoading(true);
    setError(null);
    fetchRecentAcquisitions({ topic: topic || undefined, limit: 100 })
      .then((d) => !stop && setDeals(d))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, [topic]);

  const needle = acquirer.trim().toLowerCase();
  const rows = needle ? deals.filter((d) => d.acquirer.toLowerCase().includes(needle)) : deals;

  return (
    <div className="backfill-overlay" onClick={onClose}>
      <div className="backfill-modal activity-modal" onClick={(e) => e.stopPropagation()}>
        <div className="backfill-head">
          <strong>🤝 Mergers &amp; acquisitions</strong>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="filters whatsnew-filters">
          <select value={topic} onChange={(e) => setTopic(e.target.value)}>
            <option value="">All topics</option>
            {topics.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <input
            type="search"
            placeholder="Filter by acquirer…"
            value={acquirer}
            onChange={(e) => setAcquirer(e.target.value)}
          />
        </div>

        <div className="backfill-table-wrap">
          {error ? (
            <div className="proposal-err">⚠ couldn&rsquo;t load deals: {error}</div>
          ) : loading ? (
            <div className="muted" style={{ padding: "1rem" }}>
              loading deals…
            </div>
          ) : rows.length === 0 ? (
            <p className="muted" style={{ padding: "1rem" }}>
              No acquisitions{topic ? " in this topic" : ""} yet. Deals are added by the acquisition
              research flow (propose → review → commit).
            </p>
          ) : (
            <table className="ma-table">
              <thead>
                <tr>
                  <th>Announced</th>
                  <th>Acquirer</th>
                  <th>Target</th>
                  <th>Amount</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d, i) => (
                  <tr key={`${d.acquirer}→${d.target}-${i}`}>
                    <td className="muted small">{whenLabel(d.announced_at) ?? "—"}</td>
                    <td>
                      <strong>{d.acquirer}</strong>
                    </td>
                    <td>{d.target}</td>
                    <td>
                      {d.amount && isHttpUrl(d.amount_source) ? (
                        <span className="deal-amount">
                          {d.currency ? `${d.currency} ` : ""}
                          {d.amount}{" "}
                          <a href={d.amount_source} target="_blank" rel="noreferrer">
                            source ↗
                          </a>
                        </span>
                      ) : (
                        <span className="muted small">—</span>
                      )}
                    </td>
                    <td>
                      {isHttpUrl(d.source) ? (
                        <a href={d.source} target="_blank" rel="noreferrer">
                          deal ↗
                        </a>
                      ) : (
                        <span className="muted small">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="backfill-foot">
          <span className="muted small">
            {loading ? "" : `${rows.length} deal${rows.length === 1 ? "" : "s"}`}
          </span>
          <button className="discard" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
