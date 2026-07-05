import type { ReactNode } from "react";
import { setKind } from "./api";
import type { CompanyDetail, FieldDef } from "./types";
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

export function CompanyDrawer({
  company,
  fields,
  onClose,
  onKindChange,
}: {
  company: CompanyDetail;
  fields: FieldDef[];
  onClose: () => void;
  onKindChange: (name: string, kind: string | null) => void;
}) {
  const customFields = fields.filter((f) => fieldApplies(f, company.kind));
  async function changeKind(value: string) {
    const kind = value || null;
    onKindChange(company.name, kind); // optimistic
    try {
      await setKind(company.name, kind);
    } catch {
      /* revert would need the old value; keep simple for a personal tool */
    }
  }

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} aria-label="Close">
          ×
        </button>
        <h2>
          {company.name}
          {company.origin && (
            <span className={`origin origin-${company.origin}`}>
              {company.origin === "agent" ? "🤖 agent" : company.origin === "sheet" ? "📄 sheet" : company.origin}
            </span>
          )}
        </h2>
        <div className="drawer-kind">
          <span className="field-label">Kind</span>
          <select value={company.kind ?? ""} onChange={(e) => changeKind(e.target.value)}>
            <option value="">— unset —</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {kindLabel(k)}
              </option>
            ))}
          </select>
        </div>
        <div className="drawer-links">
          {company.website && (
            <a href={company.website} target="_blank" rel="noreferrer">
              Website ↗
            </a>
          )}
          {company.linkedin && (
            <a href={company.linkedin} target="_blank" rel="noreferrer">
              LinkedIn ↗
            </a>
          )}
        </div>

        {company.about && <p className="about">{company.about}</p>}

        <div className="fields">
          <Field label="HQ" value={company.hqLocation} />
          <Field label="Headcount" value={company.headcount} />
          <Field label="Founded" value={company.yearFounded} />
          <Field label="Revenue (est.)" value={company.estimatedRevenue} />
          <Field label="Funding" value={company.funding} />
          <Field label="Priority" value={company.priority} />
          <Field label="Topics" value={company.topics.join(", ")} />
          <Field label="Type" value={company.companyTypes.join(", ")} />
          {customFields.map((f) => (
            <Field key={f.name} label={f.label} value={formatCustom(company.custom?.[f.name])} />
          ))}
        </div>

        {company.leadership.length > 0 && (
          <div className="chips-block">
            <span className="field-label">
              Leadership <span className="muted">({company.leadership.length})</span>
            </span>
            <ul className="people">
              {company.leadership.map((p) => (
                <li key={p.name}>
                  {p.name}
                  {p.title && <span className="muted"> — {p.title}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}

        <Chips label="Partners" items={company.partners} />
        <Chips label="Clients" items={company.clients} />

        {company.notes && <Field label="Notes" value={company.notes} />}

        {company.citations.length > 0 && (
          <div className="chips-block">
            <span className="field-label">
              Sources <span className="muted">({company.citations.length})</span>
            </span>
            <ul className="sources">
              {company.citations.map((c) => (
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
