import { useEffect, useState } from "react";
import { fetchDigest, fetchDigests } from "./api";
import type { Digest, DigestSummaryRow } from "./types";
import { signalKindLabel } from "./types";
import { isHttpUrl } from "./urls";

// The weekly digest page (issue #51): a browsable history of "what changed" —
// new signals grouped by company, newly-researched companies, and notable job
// outcomes, produced by a scheduled job and stored per run. Modal-as-page, same
// shell as the "What's new" feed. Digest text is phrased from graph data, but the
// signal titles/company names inside it originate from crawled feeds — untrusted —
// so everything renders as auto-escaped text and a signal only links out when its
// URL is http(s) (same guard as SignalTimeline).


// A parseable date renders localised; otherwise keep the raw string (or nothing).
function whenLabel(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const t = Date.parse(raw);
  return Number.isNaN(t) ? raw : new Date(t).toLocaleDateString();
}

function DigestBody({ digest }: { digest: Digest }) {
  const p = digest.payload;
  const empty =
    p.newSignalsByCompany.length === 0 &&
    p.newlyResearched.length === 0 &&
    p.notableChanges.length === 0;

  return (
    <div className="digest-body">
      {digest.summary && <p className="digest-summary">{digest.summary}</p>}

      {empty && (
        <p className="muted" style={{ padding: "0.5rem 0" }}>
          A quiet week — nothing changed.
        </p>
      )}

      {p.newSignalsByCompany.length > 0 && (
        <section className="digest-section">
          <h4>New signals by company</h4>
          {p.newSignalsByCompany.map((g) => (
            <div key={g.company} className="digest-company">
              <div className="digest-company-head">
                <strong>{g.company}</strong>
                <span className="muted small">
                  {g.count} signal{g.count === 1 ? "" : "s"}
                </span>
              </div>
              <ul className="signal-list">
                {g.signals.map((s, i) => {
                  const title = s.title || s.url || "(untitled)";
                  const when = whenLabel(s.when);
                  return (
                    <li key={s.url || s.title || i} className="signal-item">
                      <div className="signal-head">
                        <span className={`signal-kind kind-${s.kind}`}>
                          {signalKindLabel(s.kind)}
                        </span>
                        {isHttpUrl(s.url) ? (
                          <a
                            className="signal-title"
                            href={s.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {title} ↗
                          </a>
                        ) : (
                          <span className="signal-title">{title}</span>
                        )}
                      </div>
                      {when && <div className="signal-when muted small">{when}</div>}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </section>
      )}

      {p.newlyResearched.length > 0 && (
        <section className="digest-section">
          <h4>Newly researched companies</h4>
          <ul className="digest-plain-list">
            {p.newlyResearched.map((c) => (
              <li key={c.name}>
                <strong>{c.name}</strong>
                {c.topics.length > 0 && (
                  <span className="chips digest-topics">
                    {c.topics.map((t) => (
                      <span key={t} className="chip">
                        {t}
                      </span>
                    ))}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {p.notableChanges.length > 0 && (
        <section className="digest-section">
          <h4>Notable changes</h4>
          <ul className="digest-plain-list">
            {p.notableChanges.map((c, i) => (
              <li key={i}>
                <span className="signal-kind">{c.type}</span> {c.outcome}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export function DigestModal({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<DigestSummaryRow[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [digest, setDigest] = useState<Digest | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the digest history (newest-first) and select the most recent one.
  useEffect(() => {
    let stop = false;
    setLoading(true);
    setError(null);
    fetchDigests()
      .then((r) => {
        if (stop) return;
        setRows(r);
        if (r.length > 0) setSelectedId(r[0].id);
      })
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, []);

  // Fetch the selected digest's full grouped payload.
  useEffect(() => {
    if (!selectedId) {
      setDigest(null);
      return;
    }
    let stop = false;
    setDetailLoading(true);
    fetchDigest(selectedId)
      .then((d) => !stop && setDigest(d))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setDetailLoading(false));
    return () => {
      stop = true;
    };
  }, [selectedId]);

  return (
    <div className="backfill-overlay" onClick={onClose}>
      <div className="backfill-modal activity-modal" onClick={(e) => e.stopPropagation()}>
        <div className="backfill-head">
          <strong>📰 Weekly digest</strong>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {rows.length > 0 && (
          <div className="filters whatsnew-filters">
            <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
              {rows.map((r) => (
                <option key={r.id} value={r.id}>
                  Week of {r.weekOf} — {r.totals.newSignals} signal
                  {r.totals.newSignals === 1 ? "" : "s"}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="backfill-table-wrap">
          {error ? (
            <div className="proposal-err">⚠ couldn&rsquo;t load digests: {error}</div>
          ) : loading ? (
            <div className="muted" style={{ padding: "1rem" }}>
              loading digests…
            </div>
          ) : rows.length === 0 ? (
            <p className="muted" style={{ padding: "1rem" }}>
              No digests yet. A weekly digest is generated automatically once there is activity to
              summarise.
            </p>
          ) : detailLoading || !digest ? (
            <div className="muted" style={{ padding: "1rem" }}>
              loading digest…
            </div>
          ) : (
            <DigestBody digest={digest} />
          )}
        </div>

        <div className="backfill-foot">
          <button className="discard" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
