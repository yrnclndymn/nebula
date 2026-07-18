import { useEffect, useState } from "react";
import { commitResolution, getResolution, scanResolution } from "./api";
import { Page } from "./Page";
import type { Resolution, ResolutionDecision } from "./types";

// One reviewer's working choice for a proposed cluster: which spelling survives,
// which members fold into it, and whether to act on the cluster at all.
interface ClusterChoice {
  canonical: string;
  excluded: Set<string>; // members the user un-checked (kept as separate nodes)
  skip: boolean; // leave this cluster alone entirely
}

// Human-in-the-loop review for entity resolution. Detection only proposes;
// merges are irreversible, so nothing is written until the reviewer commits.
export function ResolvePage() {
  const [res, setRes] = useState<Resolution | null>(null);
  const [choices, setChoices] = useState<Record<number, ClusterChoice>>({});
  const [junk, setJunk] = useState<Set<string>>(new Set());
  const [committing, setCommitting] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Kick off a scan, then poll it to completion.
  useEffect(() => {
    let stop = false;
    let jobId: string | null = null;
    const poll = async () => {
      try {
        if (!jobId) jobId = (await scanResolution()).job_id;
        const r = await getResolution(jobId);
        if (stop) return;
        if (r.status === "ready" || r.status === "error") {
          setRes(r);
          setChoices(
            Object.fromEntries(
              r.clusters.map((c, i) => [
                i,
                {
                  canonical: c.canonical,
                  excluded: new Set<string>(),
                  skip: c.reason === "containment", // looser matches: opt-in, not default
                },
              ]),
            ),
          );
          setJunk(new Set(r.junk.map((j) => j.name)));
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (!stop) setTimeout(poll, 2000);
    };
    poll();
    return () => {
      stop = true;
    };
  }, []);

  function patch(i: number, next: Partial<ClusterChoice>) {
    setChoices((c) => ({ ...c, [i]: { ...c[i], ...next } }));
  }

  function toggleExcluded(i: number, name: string) {
    setChoices((c) => {
      const excluded = new Set(c[i].excluded);
      if (excluded.has(name)) excluded.delete(name);
      else excluded.add(name);
      return { ...c, [i]: { ...c[i], excluded } };
    });
  }

  function toggleJunk(name: string) {
    setJunk((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  // Build the decision batch from the current selections.
  function decisions(): ResolutionDecision[] {
    if (!res) return [];
    const out: ResolutionDecision[] = [];
    res.clusters.forEach((cluster, i) => {
      const ch = choices[i];
      if (!ch || ch.skip) return;
      const variants = cluster.members
        .map((m) => m.name)
        .filter((n) => n !== ch.canonical && !ch.excluded.has(n));
      if (variants.length) out.push({ action: "merge", canonical: ch.canonical, variants });
    });
    const names = [...junk];
    if (names.length) out.push({ action: "junk", names });
    return out;
  }

  const batch = decisions();
  const mergeCount = batch.filter((d) => d.action === "merge").length;

  async function commit() {
    if (!res || !batch.length) return;
    setCommitting(true);
    setError(null);
    try {
      const r = await commitResolution(res.job_id, batch);
      if (r.error) throw new Error(r.error);
      setDone(`✓ merged ${r.merged ?? 0}, flagged ${r.flagged ?? 0} junk. Refresh to see changes.`);
    } catch (e) {
      setError(String(e));
      setCommitting(false);
    }
  }

  return (
    <Page title="Resolve stub companies">

        {!res ? (
          <div className="muted" style={{ padding: "1rem" }}>
            🔎 scanning stub companies for duplicates…
          </div>
        ) : res.status === "error" ? (
          <div className="proposal-err">⚠ scan failed{res.error ? `: ${res.error}` : ""}</div>
        ) : (
          <div className="backfill-table-wrap">
            <p className="muted" style={{ margin: "0 0 0.75rem" }}>
              {res.stub_count} stub companies · {res.clusters.length} possible duplicate clusters ·{" "}
              {res.junk.length} look like junk. Merges are irreversible — review before committing.
            </p>

            {res.clusters.map((cluster, i) => {
              const ch = choices[i];
              if (!ch) return null;
              return (
                <div key={i} className={`proposal ${ch.skip ? "committed" : ""}`}>
                  <div className="proposal-head">
                    <strong>{ch.canonical}</strong>
                    <span className="tag">{cluster.reason}</span>
                    <label className="muted" style={{ marginLeft: "auto", fontWeight: "normal" }}>
                      <input
                        type="checkbox"
                        checked={ch.skip}
                        onChange={() => patch(i, { skip: !ch.skip })}
                      />{" "}
                      skip
                    </label>
                  </div>
                  <table>
                    <tbody>
                      {cluster.members.map((m) => {
                        const isCanon = m.name === ch.canonical;
                        return (
                          <tr key={m.name} className={ch.skip ? "muted" : ""}>
                            <td>
                              <input
                                type="radio"
                                name={`canon-${i}`}
                                checked={isCanon}
                                disabled={ch.skip}
                                onChange={() => patch(i, { canonical: m.name })}
                                title="Keep this as the canonical node"
                              />
                            </td>
                            <td>
                              <input
                                type="checkbox"
                                checked={!isCanon && !ch.excluded.has(m.name)}
                                disabled={ch.skip || isCanon}
                                onChange={() => toggleExcluded(i, m.name)}
                                title="Merge this variant into the canonical"
                              />
                            </td>
                            <td className="name">
                              {m.name}
                              {isCanon && <span className="muted"> — keep</span>}
                            </td>
                            <td className="muted num">{m.edges} edges</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              );
            })}

            {res.junk.length > 0 && (
              <div className="proposal">
                <div className="proposal-head">
                  <strong>Possible junk</strong>
                  <span className="muted">excluded from the company list</span>
                </div>
                <div className="name-chips">
                  {res.junk.map((j) => (
                    <label key={j.name} className={`chip ${junk.has(j.name) ? "" : "muted"}`}>
                      <input
                        type="checkbox"
                        checked={junk.has(j.name)}
                        onChange={() => toggleJunk(j.name)}
                      />{" "}
                      {j.name}
                    </label>
                  ))}
                </div>
              </div>
            )}

            {res.clusters.length === 0 && res.junk.length === 0 && (
              <p className="muted">No duplicate clusters or junk found. 🎉</p>
            )}
          </div>
        )}

        {error && <div className="proposal-err">{error}</div>}

        <div className="backfill-foot">
          {done ? (
            <span className="proposal-done">{done}</span>
          ) : (
            <>
              <button
                className="commit"
                onClick={commit}
                disabled={committing || !batch.length}
              >
                {committing
                  ? "committing…"
                  : `Commit ${mergeCount} merge${mergeCount === 1 ? "" : "s"}` +
                    (junk.size ? ` + ${junk.size} junk` : "")}
              </button>
            </>
          )}
        </div>
    </Page>
  );
}
