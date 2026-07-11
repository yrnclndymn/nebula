import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import "./App.css";
import {
  fetchCompanies,
  fetchCompanyTypes,
  fetchCompany,
  fetchCountries,
  fetchFields,
  fetchTopics,
} from "./api";
import type { CompanyDetail, CompanyRow, FieldDef } from "./types";
import { fieldApplies, formatCustom, KINDS, kindLabel } from "./types";
import { CompanyDrawer } from "./CompanyDrawer";
import { ChatPanel } from "./ChatPanel";
import { EntityResolutionModal } from "./EntityResolution";
import { AUTH_ENABLED, signOutUser } from "./firebase";

type SortKey = "name" | "headcount" | "yearFounded" | "partnerCount" | "clientCount";

type Column = {
  id: string;
  label: string;
  sortKey?: SortKey; // sortable when set
  numeric?: boolean;
  cellClass?: string;
  render: (c: CompanyRow) => ReactNode;
};

const ORDER_KEY = "nebula.columnOrder";

function loadOrder(): string[] {
  try {
    const raw = localStorage.getItem(ORDER_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
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

export default function App() {
  const [companies, setCompanies] = useState<CompanyRow[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [fields, setFields] = useState<FieldDef[]>([]);
  const [countries, setCountries] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState("");
  const [topic, setTopic] = useState("");
  const [companyType, setCompanyType] = useState("");
  const [kind, setKind] = useState("");
  const [country, setCountry] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortAsc, setSortAsc] = useState(true);

  const [order, setOrder] = useState<string[]>(loadOrder);
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);

  const [selected, setSelected] = useState<CompanyDetail | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [resolveOpen, setResolveOpen] = useState(false);

  useEffect(() => {
    Promise.all([
      fetchCompanies(),
      fetchTopics(),
      fetchCompanyTypes(),
      fetchFields(),
      fetchCountries(),
    ])
      .then(([c, t, ct, f, co]) => {
        setCompanies(c);
        setTopics(t);
        setTypes(ct);
        setFields(f);
        setCountries(co);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const filtered = companies.filter((c) => {
      if (topic && !c.topics.includes(topic)) return false;
      if (companyType && !c.companyTypes.includes(companyType)) return false;
      if (kind && c.kind !== kind) return false;
      if (country && c.hqCountry !== country) return false;
      if (needle) {
        const hay = `${c.name} ${c.about ?? ""} ${c.hqLocation ?? ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
    filtered.sort((a, b) => (sortAsc ? 1 : -1) * compare(a, b, sortKey));
    return filtered;
  }, [companies, search, topic, companyType, kind, country, sortKey, sortAsc]);

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

  function saveOrder(ids: string[]) {
    setOrder(ids);
    try {
      localStorage.setItem(ORDER_KEY, JSON.stringify(ids));
    } catch {
      /* localStorage unavailable — order just won't persist */
    }
  }

  function dropColumn(targetId: string) {
    if (!dragId || dragId === targetId) return;
    const ids = columns.map((c) => c.id);
    ids.splice(ids.indexOf(dragId), 1);
    ids.splice(ids.indexOf(targetId), 0, dragId);
    saveOrder(ids);
    setDragId(null);
    setDragOverId(null);
  }

  function updateCompanyKind(name: string, newKind: string | null) {
    setCompanies((cs) => cs.map((c) => (c.name === name ? { ...c, kind: newKind } : c)));
    setSelected((s) => (s && s.name === name ? { ...s, kind: newKind } : s));
  }

  function toggleSort(key: SortKey) {
    if (key === sortKey) setSortAsc((v) => !v);
    else {
      setSortKey(key);
      setSortAsc(key === "name");
    }
  }

  function openCompany(name: string) {
    fetchCompany(name)
      .then(setSelected)
      .catch((e) => setError(String(e)));
  }

  return (
    <div className={chatOpen ? "app chat-open" : "app"}>
      <header className="topbar">
        <h1>
          Nebula <span className="sub">research graph</span>
        </h1>
        <div className="topbar-right">
          <span className="count">
            {loading ? "loading…" : `${rows.length} / ${companies.length} companies`}
          </span>
          {order.length > 0 && (
            <button className="chat-toggle" onClick={() => saveOrder([])} title="Reset column order">
              ↺ Columns
            </button>
          )}
          <button className="chat-toggle" onClick={() => setResolveOpen(true)} title="Dedup stub companies">
            🧩 Resolve stubs
          </button>
          <button className="chat-toggle" onClick={() => setChatOpen((v) => !v)}>
            💬 Assistant
          </button>
          {AUTH_ENABLED && (
            <button className="chat-toggle" onClick={signOutUser}>
              Sign out
            </button>
          )}
        </div>
      </header>

      <div className="filters">
        <input
          className="search"
          placeholder="Search name, description, location…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={topic} onChange={(e) => setTopic(e.target.value)}>
          <option value="">All topics</option>
          {topics.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All kinds</option>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {kindLabel(k)}
            </option>
          ))}
        </select>
        <select value={country} onChange={(e) => setCountry(e.target.value)}>
          <option value="">All countries</option>
          {countries.map((co) => (
            <option key={co}>{co}</option>
          ))}
        </select>
        <select value={companyType} onChange={(e) => setCompanyType(e.target.value)}>
          <option value="">All types</option>
          {types.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
        {(search || topic || companyType || kind || country) && (
          <button
            className="clear"
            onClick={() => {
              setSearch("");
              setTopic("");
              setCompanyType("");
              setKind("");
              setCountry("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {error && <div className="error">{error}</div>}

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
            {rows.map((c) => (
              <tr key={c.name} onClick={() => openCompany(c.name)}>
                {columns.map((col) => (
                  <td key={col.id} className={col.cellClass ?? ""}>
                    {col.render(c)}
                  </td>
                ))}
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="empty">
                  No companies match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <CompanyDrawer
          company={selected}
          fields={fields}
          onClose={() => setSelected(null)}
          onKindChange={updateCompanyKind}
        />
      )}
      {chatOpen && <ChatPanel onClose={() => setChatOpen(false)} />}
      {resolveOpen && <EntityResolutionModal onClose={() => setResolveOpen(false)} />}
    </div>
  );
}
