import { useEffect, useState, type ReactNode } from "react";
import { fetchCompany, fetchSimilar, setKind } from "./api";
import { fetchCompanyAcquisitions } from "./api"; // #45 M&A drawer section
import { DiscoveryPanel } from "./DiscoveryPanel";
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
                  {p.name}
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

        <AcquisitionsSection key={`acq-${detail.name}`} name={detail.name} />

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
    </div>
  );
}

// --- #45 Acquisitions (M&A) drawer section ---------------------------------
// Self-contained: fetches this company's ACQUIRED edges (both directions) and
// splits them into deals it *made* (it is the acquirer) vs deals where it was
// *acquired* (it is the target). Each deal's `source` and the amount's
// `amount_source` render as citation links, http(s) only (crawled provenance is
// untrusted). An amount is shown ONLY next to its `amount_source` link — an
// uncited figure is never surfaced (the repo's provenance guarantee). Hides
// itself when there are no deals, like SignalsSection.
function isHttpUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function DealRow({
  deal,
  counterparty,
}: {
  deal: import("./types").Acquisition;
  counterparty: string;
}) {
  const when = deal.announced_at || deal.closed_at;
  return (
    <li className="deal-item">
      <div className="deal-head">
        <strong>{counterparty}</strong>
        {when && <span className="muted small"> · {when}</span>}
      </div>
      {deal.amount && isHttpUrl(deal.amount_source) && (
        <div className="deal-amount">
          {deal.currency ? `${deal.currency} ` : ""}
          {deal.amount}{" "}
          <a href={deal.amount_source} target="_blank" rel="noreferrer">
            source ↗
          </a>
        </div>
      )}
      {deal.thesis && <div className="deal-thesis muted small">{deal.thesis}</div>}
      {isHttpUrl(deal.source) && (
        <a className="deal-source small" href={deal.source} target="_blank" rel="noreferrer">
          deal source ↗
        </a>
      )}
    </li>
  );
}

export function AcquisitionsSection({ name }: { name: string }) {
  const [deals, setDeals] = useState<import("./types").Acquisition[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchCompanyAcquisitions(name)
      .then((d) => alive && setDeals(d))
      .catch(() => alive && setDeals([]))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, [name]);

  if (!loaded || deals.length === 0) return null;

  const made = deals.filter((d) => d.acquirer === name);
  const received = deals.filter((d) => d.target === name);

  return (
    <div className="chips-block acquisitions-section">
      <span className="field-label">
        Acquisitions <span className="muted">({deals.length})</span>
      </span>
      {made.length > 0 && (
        <div className="deal-group">
          <span className="field-sublabel muted small">Acquired ({made.length})</span>
          <ul className="deal-list">
            {made.map((d) => (
              <DealRow key={`m-${d.target}`} deal={d} counterparty={d.target} />
            ))}
          </ul>
        </div>
      )}
      {received.length > 0 && (
        <div className="deal-group">
          <span className="field-sublabel muted small">Acquired by ({received.length})</span>
          <ul className="deal-list">
            {received.map((d) => (
              <DealRow key={`r-${d.acquirer}`} deal={d} counterparty={d.acquirer} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
