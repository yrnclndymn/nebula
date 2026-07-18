import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { CompanyRow, FieldDef } from "./types";
import { fieldApplies, formatCustom, kindLabel } from "./types";

// The companies table: column configs, click-to-sort, drag-to-reorder columns.
// Column order is owned by the parent (the topbar's reset button needs it) and
// persisted via columnOrder.ts; sort state is purely a table concern and lives inside.

type SortKey = "name" | "headcount" | "yearFounded" | "partnerCount" | "clientCount";

type Column = {
  id: string;
  label: string;
  sortKey?: SortKey; // sortable when set
  numeric?: boolean;
  cellClass?: string;
  render: (c: CompanyRow) => ReactNode;
};

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
}: {
  rows: CompanyRow[];
  fields: FieldDef[];
  loading: boolean;
  order: string[];
  onReorder: (ids: string[]) => void;
  onOpenCompany: (name: string) => void;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortAsc, setSortAsc] = useState(true);
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);

  const sorted = useMemo(
    () => [...rows].sort((a, b) => (sortAsc ? 1 : -1) * compare(a, b, sortKey)),
    [rows, sortKey, sortAsc],
  );

  // Every column is one config object, so header and body render from the same list.
  const allColumns: Column[] = useMemo(
    () => [
      { id: "name", label: "Company", sortKey: "name", cellClass: "name", render: (c) => c.name },
      { id: "headcount", label: "Headcount", sortKey: "headcount", numeric: true, cellClass: "num", render: (c) => c.headcount ?? "—" },
      { id: "yearFounded", label: "Founded", sortKey: "yearFounded", numeric: true, cellClass: "num", render: (c) => c.yearFounded ?? "—" },
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
      { id: "funding", label: "Funding", cellClass: "muted", render: (c) => c.funding ?? "—" },
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
                  {col.render(c)}
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
