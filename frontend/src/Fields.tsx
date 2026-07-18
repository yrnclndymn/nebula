import type { ReactNode } from "react";

// Shared drawer primitives (#152) — the company and person drawers both rendered
// their own identical copies of these.

// A labelled value row; renders nothing for empty/absent values so callers can
// list every possible field without guarding each one.
export function Field({ label, value }: { label: string; value: ReactNode }) {
  if (value == null || value === "") return null;
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-value">{value}</span>
    </div>
  );
}

// A labelled, counted block of chip tags; renders nothing when the list is empty.
export function Chips({ label, items }: { label: string; items: string[] }) {
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
