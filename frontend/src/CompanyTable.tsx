import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { patchCompanyField } from "./api";
import type { CompanyRow, FieldDef } from "./types";
import { fieldApplies, formatCustom, kindLabel } from "./types";

// The companies table: column configs, click-to-sort, drag-to-reorder columns.
// Column order is owned by the parent (the topbar's reset button needs it) and
// persisted via columnOrder.ts; sort state is purely a table concern and lives inside.

type SortKey = "name" | "headcount" | "yearFounded" | "partnerCount" | "clientCount";

// The three scalar columns the user may edit inline (#149). Headcount and funding
// need a source URL (the provenance rule); yearFounded is optional. The server
// re-validates all of this — the client hints only make the form friendlier.
type EditableField = "headcount" | "yearFounded" | "funding";
const SOURCE_REQUIRED: Record<EditableField, boolean> = {
  headcount: true,
  yearFounded: false,
  funding: true,
};

type Column = {
  id: string;
  label: string;
  sortKey?: SortKey; // sortable when set
  numeric?: boolean;
  cellClass?: string;
  editable?: EditableField; // editable inline when set (#149)
  render: (c: CompanyRow) => ReactNode;
};

// Inline editor for one scalar cell: a value input + a source-URL input + save/
// cancel. Stops click propagation so editing never opens the row drawer. On save
// it calls the PATCH endpoint; the server enforces the provenance/validation rule
// and a rejection surfaces as an inline error.
function CellEditor({
  company,
  field,
  initial,
  onSaved,
  onCancel,
}: {
  company: string;
  field: EditableField;
  initial: number | string | null;
  onSaved: (value: number | string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial == null ? "" : String(initial));
  const [sourceUrl, setSourceUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const requiresSource = SOURCE_REQUIRED[field];

  async function save() {
    setErr(null);
    setSaving(true);
    try {
      const res = await patchCompanyField(company, field, value, sourceUrl.trim() || null);
      onSaved(res.value);
    } catch {
      setErr("Save rejected — check the value and source URL.");
      setSaving(false);
    }
  }

  return (
    <div
      className="cell-editor"
      onClick={(e) => e.stopPropagation()}
      style={{ display: "inline-flex", flexDirection: "column", gap: 4, minWidth: 160 }}
    >
      <input
        aria-label={`${field} value`}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={field === "funding" ? "e.g. Series B, $40M" : "number"}
        autoFocus
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") onCancel();
        }}
      />
      <input
        aria-label={`${field} source URL`}
        value={sourceUrl}
        onChange={(e) => setSourceUrl(e.target.value)}
        placeholder={requiresSource ? "source URL (required)" : "source URL (optional)"}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") onCancel();
        }}
      />
      {err && (
        <span className="cell-editor-err" style={{ color: "#c0392b", fontSize: "0.8em" }}>
          {err}
        </span>
      )}
      <span style={{ display: "inline-flex", gap: 4 }}>
        <button type="button" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </span>
    </div>
  );
}

function compare(a: CompanyRow, b: CompanyRow, key: SortKey): number {
  const av = a[key];
  const bv = b[key];
  if (av == null && bv == null) return 0;
  if (av == null) return 1; // nulls last
  if (bv == null) return -1;
  if (typeof av === "number" && typeof bv === "number") return av - bv;
  return String(av).localeCompare(String(bv));
}

export function CompanyTable({
  rows,
  fields,
  loading,
  order,
  onReorder,
  onOpenCompany,
  onFieldEdited,
}: {
  rows: CompanyRow[];
  fields: FieldDef[];
  loading: boolean;
  order: string[];
  onReorder: (ids: string[]) => void;
  onOpenCompany: (name: string) => void;
  // Notified after a successful inline edit (#149), so a host that owns the shared
  // dataset can sync it. Optional: the table also self-updates via a local overlay,
  // so edits reflect immediately even when no host handler is wired.
  onFieldEdited?: (name: string, field: EditableField, value: number | string) => void;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortAsc, setSortAsc] = useState(true);
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  // Which cell is open for editing, and an overlay of just-saved values keyed by
  // company name — rows come in as props (owned upstream), so the overlay is what
  // makes an edit show immediately without a refetch.
  const [editing, setEditing] = useState<{ name: string; field: EditableField } | null>(null);
  const [overlay, setOverlay] = useState<
    Record<string, Partial<Record<EditableField, number | string>>>
  >({});

  function effective(c: CompanyRow, field: EditableField): number | string | null {
    const o = overlay[c.name]?.[field];
    return o !== undefined ? o : (c[field] ?? null);
  }

  function renderEditable(c: CompanyRow, field: EditableField): ReactNode {
    if (editing && editing.name === c.name && editing.field === field) {
      return (
        <CellEditor
          company={c.name}
          field={field}
          initial={effective(c, field)}
          onSaved={(value) => {
            setOverlay((o) => ({ ...o, [c.name]: { ...o[c.name], [field]: value } }));
            setEditing(null);
            onFieldEdited?.(c.name, field, value);
          }}
          onCancel={() => setEditing(null)}
        />
      );
    }
    const val = effective(c, field);
    const edited = overlay[c.name]?.[field] !== undefined;
    return (
      <span className="editable-cell" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <span className={edited ? "edited-value" : ""} style={edited ? { fontWeight: 600 } : undefined}>
          {val ?? "—"}
        </span>
        <button
          type="button"
          className="cell-edit-btn"
          title="Edit"
          aria-label={`Edit ${field}`}
          onClick={(e) => {
            e.stopPropagation();
            setEditing({ name: c.name, field });
          }}
          style={{
            border: "none",
            background: "transparent",
            cursor: "pointer",
            opacity: 0.5,
            padding: "0 2px",
          }}
        >
          ✎
        </button>
      </span>
    );
  }

  const sorted = useMemo(
    () => [...rows].sort((a, b) => (sortAsc ? 1 : -1) * compare(a, b, sortKey)),
    [rows, sortKey, sortAsc],
  );

  // Every column is one config object, so header and body render from the same list.
  const allColumns: Column[] = useMemo(
    () => [
      { id: "name", label: "Company", sortKey: "name", cellClass: "name", render: (c) => c.name },
      { id: "headcount", label: "Headcount", sortKey: "headcount", numeric: true, cellClass: "num", editable: "headcount", render: (c) => c.headcount ?? "—" },
      { id: "yearFounded", label: "Founded", sortKey: "yearFounded", numeric: true, cellClass: "num", editable: "yearFounded", render: (c) => c.yearFounded ?? "—" },
      { id: "partnerCount", label: "Partners", sortKey: "partnerCount", numeric: true, cellClass: "num", render: (c) => c.partnerCount || "—" },
      { id: "clientCount", label: "Clients", sortKey: "clientCount", numeric: true, cellClass: "num", render: (c) => c.clientCount || "—" },
      { id: "kind", label: "Kind", cellClass: "muted", render: (c) => (c.kind ? kindLabel(c.kind) : "—") },
      {
        id: "hq",
        label: "HQ",
        cellClass: "muted",
        render: (c) => [c.hqCity, c.hqCountry].filter(Boolean).join(", ") || c.hqLocation || "—",
      },
      {
        id: "types",
        label: "Types",
        render: (c) =>
          c.companyTypes.map((t) => (
            <span key={t} className="tag">
              {t}
            </span>
          )),
      },
      { id: "funding", label: "Funding", cellClass: "muted", editable: "funding", render: (c) => c.funding ?? "—" },
      ...fields.map(
        (f): Column => ({
          id: `custom:${f.name}`,
          label: f.label,
          cellClass: "muted",
          render: (c) => (fieldApplies(f, c.kind) ? formatCustom(c.custom?.[f.name]) : "—"),
        }),
      ),
    ],
    [fields],
  );

  // Apply the saved order; append any new columns, drop any that no longer exist.
  const columns: Column[] = useMemo(() => {
    const byId = new Map(allColumns.map((c) => [c.id, c]));
    const ordered = order.filter((id) => byId.has(id)).map((id) => byId.get(id)!);
    const rest = allColumns.filter((c) => !order.includes(c.id));
    return [...ordered, ...rest];
  }, [allColumns, order]);

  function dropColumn(targetId: string) {
    if (!dragId || dragId === targetId) return;
    const ids = columns.map((c) => c.id);
    ids.splice(ids.indexOf(dragId), 1);
    ids.splice(ids.indexOf(targetId), 0, dragId);
    onReorder(ids);
    setDragId(null);
    setDragOverId(null);
  }

  function toggleSort(key: SortKey) {
    if (key === sortKey) setSortAsc((v) => !v);
    else {
      setSortKey(key);
      setSortAsc(key === "name");
    }
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.id}
                className={[
                  col.numeric ? "num" : "",
                  dragId === col.id ? "dragging" : "",
                  dragOverId === col.id ? "drag-over" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                draggable
                onDragStart={() => setDragId(col.id)}
                onDragOver={(e) => {
                  e.preventDefault();
                  if (dragId && dragOverId !== col.id) setDragOverId(col.id);
                }}
                onDragLeave={() => setDragOverId((d) => (d === col.id ? null : d))}
                onDrop={() => dropColumn(col.id)}
                onDragEnd={() => {
                  setDragId(null);
                  setDragOverId(null);
                }}
                onClick={() => col.sortKey && toggleSort(col.sortKey)}
              >
                {col.label}
                {col.sortKey && sortKey === col.sortKey && (
                  <span className="arrow">{sortAsc ? " ▲" : " ▼"}</span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((c) => (
            <tr key={c.name} onClick={() => onOpenCompany(c.name)}>
              {columns.map((col) => (
                <td key={col.id} className={col.cellClass ?? ""}>
                  {col.editable ? renderEditable(c, col.editable) : col.render(c)}
                </td>
              ))}
            </tr>
          ))}
          {!loading && sorted.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="empty">
                No companies match these filters.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
