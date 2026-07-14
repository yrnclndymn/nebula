import { useEffect, useState, type ReactNode } from "react";
import { fetchCompany, fetchSimilar, setKind } from "./api";
import { DiscoveryPanel } from "./DiscoveryPanel";
import { PersonDrawer } from "./PersonDrawer";
import { SignalsSection } from "./SignalsSection";
import type { CompanyDetail, FieldDef, SimilarCompany } from "./types";
import { fieldApplies, formatCustom, KINDS, kindLabel } from "./types";

function Field({ label, value }: { label: string; value: ReactNode }) {
  if (value == null || value === "") return null;
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-value">{value}</span>
    </div>
  );
}

function Chips({ label, items }: { label: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="chips-block">
      <span className="field-label">
        {label} <span className="muted">({items.length})</span>
      </span>
      <div className="chips">
        {items.map((it) => (
          <span key={it} className="chip">
            {it}
          </span>
        ))}
      </div>
    </div>
  );
}

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

        <SignalsSection key={detail.name} name={detail.name} hasWebsite={!!detail.website} />

        <PotentialAcquirersSection key={detail.name} name={detail.name} />

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

// --- Potential acquirers (#44) --------------------------------------------------
// Self-contained drawer section: fetches ranked candidate acquirers for the open
// company and renders each with its explainable why-reasons; deal facts link back
// to their source. Kept fully self-contained (own imports, appended at EOF) so it
// merges cleanly alongside #45's Acquisitions section and #42's leadership edit.
// React hooks are reused from the top-of-file import.
import { fetchPotentialAcquirers } from "./api";
import type { AcquirerCandidate, AcquirerWhy } from "./types";

// Deal sources are crawled/researched URLs (untrusted) — link only when http(s),
// so a hostile javascript:/data: scheme can never become a clickable href
// (PR #121 review; same guard #45 uses for its deal citations).
function isHttpAcqUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function AcquirerDeals({ deals }: { deals: { target: string; source: string | null }[] }) {
  return (
    <>
      {deals.map((deal, i) => (
        <span key={deal.target}>
          {i > 0 && ", "}
          {isHttpAcqUrl(deal.source) ? (
            <a href={deal.source} target="_blank" rel="noreferrer">
              {deal.target} ↗
            </a>
          ) : (
            deal.target
          )}
        </span>
      ))}
    </>
  );
}

// One why-reason as a list line. Deal-bearing signals show the acquired companies
// linking to their source; overlap signals list the shared names.
function AcquirerReason({ why }: { why: AcquirerWhy }) {
  const d = why.detail;
  if (why.signal === "acquired-in-topic" || why.signal === "acquired-same-kind") {
    const label =
      why.signal === "acquired-in-topic"
        ? `Acquired ${d.count} in this space`
        : `Acquired ${d.count} of the same kind${d.kind ? ` (${d.kind})` : ""}`;
    return (
      <li>
        <span className="acq-reason">{label}</span>{" "}
        <AcquirerDeals deals={d.deals ?? []} />
      </li>
    );
  }
  if (why.signal === "direct-partner")
    return (
      <li>
        <span className="acq-reason">Already a partner</span>
      </li>
    );
  if (why.signal === "shared-partners")
    return (
      <li>
        <span className="acq-reason">Shared partners</span>{" "}
        <span className="muted">{(d.partners ?? []).join(", ")}</span>
      </li>
    );
  if (why.signal === "shared-clients")
    return (
      <li>
        <span className="acq-reason">Shared clients</span>{" "}
        <span className="muted">{(d.clients ?? []).join(", ")}</span>
      </li>
    );
  if (why.signal === "active-acquirer")
    return (
      <li>
        <span className="muted">{d.total_acquisitions} acquisitions on record</span>
      </li>
    );
  return null;
}

export function PotentialAcquirersSection({ name }: { name: string }) {
  const [candidates, setCandidates] = useState<AcquirerCandidate[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    fetchPotentialAcquirers(name)
      .then((c) => alive && setCandidates(c))
      .catch(() => alive && setCandidates([]))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, [name]);

  // Only researched companies with acquisition ties surface anything; hide otherwise.
  if (!loaded || candidates.length === 0) return null;

  return (
    <div className="chips-block acquirers-section">
      <span className="field-label">
        Potential acquirers <span className="muted">({candidates.length})</span>
      </span>
      <ul className="acquirers">
        {candidates.map((c) => (
          <li key={c.acquirer} className="acquirer">
            <span className="acquirer-name">{c.acquirer}</span>
            <ul className="acquirer-why">
              {c.why.map((w, i) => (
                <AcquirerReason key={`${w.signal}-${i}`} why={w} />
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}
