import { useEffect, useMemo, useState } from "react";
import "./App.css";
import { fetchCompanies, fetchCompanyTypes, fetchCompany, fetchTopics } from "./api";
import type { CompanyDetail, CompanyRow } from "./types";
import { CompanyDrawer } from "./CompanyDrawer";
import { ChatPanel } from "./ChatPanel";

type SortKey = "name" | "headcount" | "yearFounded" | "partnerCount" | "clientCount";

const COLUMNS: { key: SortKey; label: string; numeric?: boolean }[] = [
  { key: "name", label: "Company" },
  { key: "headcount", label: "Headcount", numeric: true },
  { key: "yearFounded", label: "Founded", numeric: true },
  { key: "partnerCount", label: "Partners", numeric: true },
  { key: "clientCount", label: "Clients", numeric: true },
];

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
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState("");
  const [topic, setTopic] = useState("");
  const [companyType, setCompanyType] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortAsc, setSortAsc] = useState(true);

  const [selected, setSelected] = useState<CompanyDetail | null>(null);
  const [chatOpen, setChatOpen] = useState(false);

  useEffect(() => {
    Promise.all([fetchCompanies(), fetchTopics(), fetchCompanyTypes()])
      .then(([c, t, ct]) => {
        setCompanies(c);
        setTopics(t);
        setTypes(ct);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const filtered = companies.filter((c) => {
      if (topic && !c.topics.includes(topic)) return false;
      if (companyType && !c.companyTypes.includes(companyType)) return false;
      if (needle) {
        const hay = `${c.name} ${c.about ?? ""} ${c.hqLocation ?? ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
    filtered.sort((a, b) => (sortAsc ? 1 : -1) * compare(a, b, sortKey));
    return filtered;
  }, [companies, search, topic, companyType, sortKey, sortAsc]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) setSortAsc((v) => !v);
    else {
      setSortKey(key);
      setSortAsc(key === "name");
    }
  }

  function openCompany(name: string) {
    fetchCompany(name).then(setSelected).catch((e) => setError(String(e)));
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
          <button className="chat-toggle" onClick={() => setChatOpen((v) => !v)}>
            💬 Assistant
          </button>
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
        <select value={companyType} onChange={(e) => setCompanyType(e.target.value)}>
          <option value="">All types</option>
          {types.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
        {(search || topic || companyType) && (
          <button
            className="clear"
            onClick={() => {
              setSearch("");
              setTopic("");
              setCompanyType("");
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
              {COLUMNS.map((col) => (
                <th
                  key={col.key}
                  className={col.numeric ? "num" : ""}
                  onClick={() => toggleSort(col.key)}
                >
                  {col.label}
                  {sortKey === col.key && <span className="arrow">{sortAsc ? " ▲" : " ▼"}</span>}
                </th>
              ))}
              <th>HQ</th>
              <th>Types</th>
              <th>Funding</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.name} onClick={() => openCompany(c.name)}>
                <td className="name">{c.name}</td>
                <td className="num">{c.headcount ?? "—"}</td>
                <td className="num">{c.yearFounded ?? "—"}</td>
                <td className="num">{c.partnerCount || "—"}</td>
                <td className="num">{c.clientCount || "—"}</td>
                <td className="muted">{c.hqLocation ?? "—"}</td>
                <td>
                  {c.companyTypes.map((t) => (
                    <span key={t} className="tag">
                      {t}
                    </span>
                  ))}
                </td>
                <td className="muted">{c.funding ?? "—"}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={8} className="empty">
                  No companies match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && <CompanyDrawer company={selected} onClose={() => setSelected(null)} />}
      {chatOpen && <ChatPanel onClose={() => setChatOpen(false)} />}
    </div>
  );
}
