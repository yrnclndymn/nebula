import { useRef, useState } from "react";
import { getDiscovery, researchDiscovery, startDiscovery } from "./api";
import type { Discovery } from "./types";

// Web discovery (issue #75): from a researched company's drawer, use its in-graph
// similar cohort as a template to search the web for MORE companies like it that
// aren't captured yet. The user reviews the deduped candidates and selects which
// to feed into the existing research pipeline. Nothing is written here — selected
// candidates become proposals to review and commit on the activity page.

const MAX_RESEARCH = 10; // mirrors the backend cap (MAX_DISCOVERY_RESEARCH)

type Phase = "idle" | "running" | "ready" | "note" | "error";

export function DiscoveryPanel({ seed }: { seed: string }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [job, setJob] = useState<Discovery | null>(null);
  const [note, setNote] = useState<string>("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [researching, setResearching] = useState(false);
  const [startedCount, setStartedCount] = useState<number | null>(null);
  const stop = useRef(false);

  async function poll(jobId: string) {
    try {
      const d = await getDiscovery(jobId);
      if (stop.current) return;
      setJob(d);
      if (d.status === "ready") {
        setPhase("ready");
        return;
      }
      if (d.status === "error") {
        setPhase("error");
        return;
      }
    } catch {
      /* keep polling through transient errors */
    }
    if (!stop.current) setTimeout(() => poll(jobId), 2500);
  }

  async function discover() {
    stop.current = false;
    setPhase("running");
    setStartedCount(null);
    setSelected(new Set());
    try {
      const res = await startDiscovery(seed);
      if (res.job_id) {
        poll(res.job_id);
      } else {
        setNote(res.note ?? "No similar cohort to search from.");
        setPhase("note");
      }
    } catch {
      setPhase("error");
    }
  }

  function toggle(name: string) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else if (next.size < MAX_RESEARCH) next.add(name);
      return next;
    });
  }

  async function research() {
    if (!job || selected.size === 0) return;
    setResearching(true);
    try {
      const res = await researchDiscovery(job.job_id, [...selected]);
      setStartedCount(res.proposals.length);
      setSelected(new Set());
    } catch {
      /* leave the selection so the user can retry */
    } finally {
      setResearching(false);
    }
  }

  const candidates = job?.candidates ?? [];

  return (
    <div className="discovery">
      {phase === "idle" && (
        <button className="discover-btn" onClick={discover}>
          🔭 Discover more like these
        </button>
      )}

      {phase === "running" && <div className="muted">🔭 searching the web for more like {seed}…</div>}

      {phase === "note" && <div className="muted">{note}</div>}

      {phase === "error" && (
        <div className="muted">
          Discovery failed{job?.error ? `: ${job.error}` : ""}.{" "}
          <button className="linklike" onClick={discover}>
            Retry
          </button>
        </div>
      )}

      {phase === "ready" && (
        <>
          <div className="field-label">
            New candidates <span className="muted">({candidates.length})</span>
          </div>
          {job?.profile?.summary && <p className="muted discovery-summary">{job.profile.summary}</p>}
          {candidates.length === 0 ? (
            <div className="muted">No new companies found — the cohort looks well covered.</div>
          ) : (
            <>
              <ul className="discovery-candidates">
                {candidates.map((c) => {
                  const checked = selected.has(c.name);
                  const atCap = !checked && selected.size >= MAX_RESEARCH;
                  return (
                    <li key={c.website || c.name}>
                      <label className={atCap ? "muted" : ""}>
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={atCap}
                          onChange={() => toggle(c.name)}
                        />
                        <span className="candidate-name">{c.name}</span>
                        {c.website && (
                          <a
                            href={c.website.startsWith("http") ? c.website : `https://${c.website}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {c.website} ↗
                          </a>
                        )}
                      </label>
                      {c.why.length > 0 && (
                        <div className="chips">
                          {c.why.map((w) => (
                            <span key={w} className="chip">
                              {w}
                            </span>
                          ))}
                        </div>
                      )}
                      {c.sources.length > 0 && (
                        <div className="muted candidate-sources">
                          {c.sources.slice(0, 3).map((s, i) => (
                            <a key={s} href={s} target="_blank" rel="noreferrer">
                              source {i + 1} ↗
                            </a>
                          ))}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
              {startedCount != null ? (
                <div className="muted">
                  ✅ started researching {startedCount} — review each proposal on the activity page.
                </div>
              ) : (
                <div className="proposal-actions">
                  <button
                    className="commit"
                    onClick={research}
                    disabled={researching || selected.size === 0}
                  >
                    {researching
                      ? "starting…"
                      : `Research ${selected.size} selected${selected.size >= MAX_RESEARCH ? ` (max ${MAX_RESEARCH})` : ""}`}
                  </button>
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
