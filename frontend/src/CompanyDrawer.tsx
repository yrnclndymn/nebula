import { useEffect, useState } from "react";
import { fetchCompany, fetchSimilar, setKind } from "./api";
import { AcquisitionsSection } from "./AcquisitionsSection";
import { DiscoveryPanel } from "./DiscoveryPanel";
import { Field, Chips } from "./Fields";
import { PersonDrawer } from "./PersonDrawer";
import { PersonResearchButton } from "./PersonProposalCard";
import { PotentialAcquirersSection } from "./PotentialAcquirers";
import { SignalsSection } from "./SignalsSection";
import type { CompanyDetail, FieldDef, SimilarCompany } from "./types";
import { fieldApplies, formatCustom, KINDS, kindLabel } from "./types";

// Human-readable "why" for a similarity match, e.g. "2 shared clients · same country".
function similarWhy(s: SimilarCompany): string {
  const plural = (n: number, word: string) => `${n} shared ${word}${n === 1 ? "" : "s"}`;
  const parts: string[] = [];
  if (s.shared_clients) parts.push(plural(s.shared_clients, "client"));
  if (s.shared_partners) parts.push(plural(s.shared_partners, "partner"));
  if (s.shared_topics) parts.push(plural(s.shared_topics, "topic"));
  if (s.same_kind) parts.push("same kind");
  if (s.same_country) parts.push("same country");
  return parts.join(" · ");
}

export function CompanyDrawer({
  company,
  fields,
  onClose,
  onKindChange,
  onViewInGraph,
}: {
  company: CompanyDetail;
  fields: FieldDef[];
  onClose: () => void;
  onKindChange: (name: string, kind: string | null) => void;
  onViewInGraph: (name: string) => void;
}) {
  // `detail` normally mirrors the `company` prop, but clicking a "Similar company"
  // loads that company into the same drawer without involving the parent — so the
  // drawer can self-navigate. Reset whenever the parent selects a new company.
  const [detail, setDetail] = useState<CompanyDetail>(company);
  const [similar, setSimilar] = useState<SimilarCompany[]>([]);
  // Leader name → open that person's page (#42). Set when a leader with a resolved
  // Person id is clicked; renders <PersonDrawer> over this drawer.
  const [personId, setPersonId] = useState<string | null>(null);
  // Leader name → research that person in place (#178). This company scopes the person
  // (they lead it), so the enrich call never 404s on an unknown pairing.
  const [researchLeader, setResearchLeader] = useState<string | null>(null);

  useEffect(() => {
    setDetail(company);
  }, [company]);

  useEffect(() => {
    let alive = true;
    fetchSimilar(detail.name)
      .then((s) => alive && setSimilar(s))
      .catch(() => alive && setSimilar([]));
    return () => {
      alive = false;
    };
  }, [detail.name]);

  const customFields = fields.filter((f) => fieldApplies(f, detail.kind));

  async function changeKind(value: string) {
    const kind = value || null;
    setDetail((d) => ({ ...d, kind })); // optimistic (local)
    onKindChange(detail.name, kind); // keep the table + parent selection in sync
    try {
      await setKind(detail.name, kind);
    } catch {
      /* revert would need the old value; keep simple for a personal tool */
    }
  }

  const [similarError, setSimilarError] = useState<string | null>(null);

  function openSimilar(name: string) {
    setSimilarError(null);
    fetchCompany(name)
      .then(setDetail)
      .catch(() => setSimilarError(`Couldn't load ${name} — it may have been renamed or removed.`));
  }

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} aria-label="Close">
          ×
        </button>
        <h2>
          {detail.name}
          {detail.origin && (
            <span className={`origin origin-${detail.origin}`}>
              {detail.origin === "agent" ? "🤖 agent" : detail.origin === "sheet" ? "📄 sheet" : detail.origin}
            </span>
          )}
        </h2>
        <div className="drawer-kind">
          <span className="field-label">Kind</span>
          <select value={detail.kind ?? ""} onChange={(e) => changeKind(e.target.value)}>
            <option value="">— unset —</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {kindLabel(k)}
              </option>
            ))}
          </select>
        </div>
        <div className="drawer-links">
          <button className="graph-link" onClick={() => onViewInGraph(detail.name)}>
            🕸 View in graph
          </button>
          {detail.website && (
            <a href={detail.website} target="_blank" rel="noreferrer">
              Website ↗
            </a>
          )}
          {detail.linkedin && (
            <a href={detail.linkedin} target="_blank" rel="noreferrer">
              LinkedIn ↗
            </a>
          )}
        </div>

        {detail.about && <p className="about">{detail.about}</p>}

        <div className="fields">
          <Field label="Country" value={detail.hqCountry} />
          <Field label="City" value={detail.hqCity} />
          <Field label="State" value={detail.hqState} />
          {!detail.hqCountry && <Field label="HQ" value={detail.hqLocation} />}
          <Field label="Headcount" value={detail.headcount} />
          <Field label="Founded" value={detail.yearFounded} />
          <Field label="Revenue (est.)" value={detail.estimatedRevenue} />
          <Field label="Funding" value={detail.funding} />
          <Field label="Priority" value={detail.priority} />
          <Field label="Topics" value={detail.topics.join(", ")} />
          <Field label="Type" value={detail.companyTypes.join(", ")} />
          {customFields.map((f) => (
            <Field key={f.name} label={f.label} value={formatCustom(detail.custom?.[f.name])} />
          ))}
        </div>

        {detail.leadership.length > 0 && (
          <div className="chips-block">
            <span className="field-label">
              Leadership <span className="muted">({detail.leadership.length})</span>
            </span>
            <ul className="people">
              {detail.leadership.map((p) => (
                <li key={p.name}>
                  {p.id ? (
                    <button className="similar-name" onClick={() => setPersonId(p.id!)}>
                      {p.name}
                    </button>
                  ) : (
                    p.name
                  )}
                  {p.title && <span className="muted"> — {p.title}</span>}
                  {/* #178: research a resolved leader in place (bio/links/roles, each
                      cited) via #40's propose→review→commit. Only for leaders with a
                      Person id — an unresolved stub has no node to enrich. */}
                  {p.id && researchLeader !== p.name && (
                    <button
                      className="leader-research"
                      title={`Research ${p.name}`}
                      onClick={() => setResearchLeader(p.name)}
                    >
                      🔎
                    </button>
                  )}
                  {researchLeader === p.name && (
                    <PersonResearchButton
                      key={`research-${p.name}`}
                      name={p.name}
                      company={detail.name}
                      autoStart
                    />
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        <Chips label="Partners" items={detail.partners} />
        <Chips label="Clients" items={detail.clients} />

        {similar.length > 0 && (
          <div className="chips-block">
            <span className="field-label">
              Similar companies <span className="muted">({similar.length})</span>
            </span>
            <ul className="similar">
              {similar.map((s) => (
                <li key={s.name}>
                  <button className="similar-name" onClick={() => openSimilar(s.name)}>
                    {s.name}
                  </button>
                  <span className="muted"> — {similarWhy(s)}</span>
                </li>
              ))}
            </ul>
            {similarError && <div className="muted">{similarError}</div>}
            <DiscoveryPanel key={detail.name} seed={detail.name} />
          </div>
        )}

        <SignalsSection key={`sig-${detail.name}`} name={detail.name} hasWebsite={!!detail.website} />

        <AcquisitionsSection key={`acq-${detail.name}`} name={detail.name} />

        <PotentialAcquirersSection key={`pa-${detail.name}`} name={detail.name} />

        {detail.notes && <Field label="Notes" value={detail.notes} />}

        {detail.citations.length > 0 && (
          <div className="chips-block">
            <span className="field-label">
              Sources <span className="muted">({detail.citations.length})</span>
            </span>
            <ul className="sources">
              {detail.citations.map((c) => (
                <li key={`${c.field}-${c.source}`}>
                  <span className="src-field">{c.field}</span>: {c.value}{" "}
                  <a href={c.source} target="_blank" rel="noreferrer">
                    source ↗
                  </a>
                  {c.sourceDate && <span className="muted"> · {c.sourceDate}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </aside>
      {personId && <PersonDrawer personId={personId} onClose={() => setPersonId(null)} />}
    </div>
  );
}
