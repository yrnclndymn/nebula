import { useCallback, useEffect, useState } from "react";
import { fetchPerson, getPersonExpertiseJob, regeneratePersonExpertise } from "./api";
import type { PersonProfile, PersonRole, PersonSignal } from "./types";
import { signalKindLabel } from "./types";
import { isHttpUrl } from "./urls";
import { signalWhen } from "./dates";
import { Field } from "./Fields";
import { PersonResearchButton } from "./PersonProposalCard";
import { usePollJob } from "./usePollJob";

// The person page (#42), rendered as a drawer over the company drawer. Opened by
// clicking a leader's name in CompanyDrawer. Shows identity + roles + their linked-
// signals timeline + a derived, advisory expertise summary (regenerable, dated).
// The summary and signal titles are crawled/derived — untrusted — so they render as
// plain (auto-escaped) text and only link out when a URL is http(s).


function relationLabel(relation: string): string {
  return relation.replace(/_/g, " ").toLowerCase();
}

function roleLine(r: PersonRole): string {
  const base = r.title ? `${r.title} at ${r.company}` : r.company;
  const span =
    r.from || r.to ? ` (${r.from ?? "?"}–${r.to ?? "present"})` : "";
  return base + span;
}

function formatGeneratedAt(iso: string | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

// The expertise block: the summary + its generation date + a regenerate button that
// enqueues the durable job and polls it, then refreshes the person on completion.
function ExpertiseSection({
  person,
  onRegenerated,
}: {
  person: PersonProfile;
  onRegenerated: () => void;
}) {
  const [phase, setPhase] = useState<"idle" | "running" | "error">("idle");
  const [note, setNote] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);

  // Poll the regeneration job until it settles, then refresh the person so the
  // new summary shows.
  usePollJob(
    phase === "running" && jobId !== null,
    async (cancelled) => {
      if (!jobId) return;
      try {
        const job = await getPersonExpertiseJob(jobId);
        if (cancelled()) return;
        if (job.status === "done") {
          setPhase("idle");
          setNote("");
          onRegenerated();
        } else if (job.status === "error") {
          setNote(job.error ?? "generation failed");
          setPhase("error");
        }
      } catch {
        /* keep polling through transient errors */
      }
    },
    { leading: true },
  );

  async function regenerate() {
    setJobId(null);
    setPhase("running");
    setNote("");
    try {
      const res = await regeneratePersonExpertise(person.id);
      setJobId(res.job_id);
    } catch {
      setNote("could not start generation");
      setPhase("error");
    }
  }

  const exp = person.expertise;
  return (
    <div className="chips-block expertise-section">
      <span className="field-label">Expertise</span>
      {exp ? (
        <>
          <p className="about">{exp.summary}</p>
          <div className="muted small">
            Generated {formatGeneratedAt(exp.generatedAt)}
            {exp.sources.length > 0 && (
              <>
                {" · "}
                {exp.sources.filter(isHttpUrl).map((src, i) => (
                  <a key={src} href={src} target="_blank" rel="noreferrer">
                    source {i + 1} ↗{" "}
                  </a>
                ))}
              </>
            )}
          </div>
        </>
      ) : (
        <p className="muted small">
          No expertise summary yet — generate one from this person's roles and linked signals.
        </p>
      )}
      <div className="capture-signals">
        <button type="button" onClick={regenerate} disabled={phase === "running"}>
          {phase === "running" ? "Generating…" : exp ? "🔄 Regenerate" : "✨ Generate summary"}
        </button>
        {note && <span className={phase === "error" ? "muted error" : "muted"}> {note}</span>}
      </div>
    </div>
  );
}

function PersonSignalList({ signals }: { signals: PersonSignal[] }) {
  return (
    <ul className="signal-list">
      {signals.map((s, i) => {
        const when = signalWhen(s);
        const title = s.title || s.url || "(untitled)";
        return (
          <li key={s.url || s.title || String(i)} className="signal-item">
            <div className="signal-head">
              <span className="signal-kind">{relationLabel(s.relation)}</span>
              <span className={`signal-kind kind-${s.kind}`}>{signalKindLabel(s.kind)}</span>
              {isHttpUrl(s.url) ? (
                <a className="signal-title" href={s.url} target="_blank" rel="noreferrer">
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
  );
}

export function PersonDrawer({ personId, onClose }: { personId: string; onClose: () => void }) {
  const [person, setPerson] = useState<PersonProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    let alive = true;
    setError(null);
    fetchPerson(personId)
      .then((p) => alive && setPerson(p))
      .catch(() => alive && setError("Couldn't load this person."));
    return () => {
      alive = false;
    };
  }, [personId]);

  useEffect(() => load(), [load]);

  return (
    // Stop the backdrop click bubbling to the company drawer's overlay behind it,
    // so closing this person drawer never also closes the company drawer.
    <div
      className="drawer-overlay"
      onClick={(e) => {
        e.stopPropagation();
        onClose();
      }}
    >
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} aria-label="Close">
          ×
        </button>
        {error && <p className="muted error">{error}</p>}
        {!person && !error && <p className="muted">Loading…</p>}
        {person && (
          <>
            <h2>
              {person.name}
              {person.flagged && (
                <span className="origin origin-flagged" title="Unreviewed signal-capture stub">
                  ⚠ unreviewed
                </span>
              )}
            </h2>
            <div className="drawer-links">
              {person.linkedin && (
                <a href={person.linkedin} target="_blank" rel="noreferrer">
                  LinkedIn ↗
                </a>
              )}
              {person.personalSite && (
                <a href={person.personalSite} target="_blank" rel="noreferrer">
                  Website ↗
                </a>
              )}
            </div>

            {person.bio && <p className="about">{person.bio}</p>}

            {/* #178: research this person (bio/links/roles, each cited) via #40's
                propose→review→commit. Scoped by a company they lead so the backend
                targets this exact human — needs at least one current (LEADS) role. */}
            {person.currentRoles.length > 0 && (
              <div className="person-research-section">
                <PersonResearchButton
                  name={person.name}
                  company={person.currentRoles[0].company}
                />
              </div>
            )}

            <ExpertiseSection person={person} onRegenerated={load} />

            {person.currentRoles.length > 0 && (
              <div className="chips-block">
                <span className="field-label">Roles</span>
                <ul className="people">
                  {person.currentRoles.map((r) => (
                    <li key={`cur-${r.company}-${r.title ?? ""}`}>{roleLine(r)}</li>
                  ))}
                </ul>
              </div>
            )}

            {person.priorRoles.length > 0 && (
              <div className="chips-block">
                <span className="field-label">
                  Prior roles <span className="muted">({person.priorRoles.length})</span>
                </span>
                <ul className="people">
                  {person.priorRoles.map((r) => (
                    <li key={`prior-${r.company}-${r.title ?? ""}`}>{roleLine(r)}</li>
                  ))}
                </ul>
              </div>
            )}

            <Field label="Talks" value={person.talks.filter(isHttpUrl).length > 0 ? (
              <span>
                {person.talks.filter(isHttpUrl).map((t, i) => (
                  <a key={t} href={t} target="_blank" rel="noreferrer">
                    talk {i + 1} ↗{" "}
                  </a>
                ))}
              </span>
            ) : null} />

            <div className="chips-block signals-section">
              <span className="field-label">
                Signals <span className="muted">({person.signals.length})</span>
              </span>
              {person.signals.length > 0 ? (
                <PersonSignalList signals={person.signals} />
              ) : (
                <div className="muted small">No linked signals yet.</div>
              )}
            </div>
          </>
        )}
      </aside>
    </div>
  );
}
