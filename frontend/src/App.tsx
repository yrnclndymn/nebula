import { useEffect, useMemo, useState } from "react";
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
import { KINDS, kindLabel } from "./types";
import { CompanyDrawer } from "./CompanyDrawer";
import { CompanyTable } from "./CompanyTable";
import { loadColumnOrder, saveColumnOrder } from "./columnOrder";
import { GraphView } from "./GraphView";
import { ChatPanel } from "./ChatPanel";
import { EntityResolutionModal } from "./EntityResolution";
import { ClientClassificationModal } from "./ClientClassification";
import { BacklogModal } from "./BacklogPanel";
import { ActivityModal } from "./ActivityPanel";
import { WhatsNewModal } from "./WhatsNew";
import { DigestModal } from "./DigestPanel";
import { MAPage } from "./MAPage"; // #45 M&A view
import { AUTH_ENABLED, signOutUser } from "./firebase";

// The full-screen review/browse surfaces, each a modal-as-page; one is open at a
// time. The chat panel and graph view are separate: chat is a side panel that
// coexists with everything, and the graph carries its own seed state.
type Modal = "backlog" | "activity" | "whatsnew" | "digest" | "ma" | "resolve" | "classify";

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

  const [order, setOrder] = useState<string[]>(loadColumnOrder);

  const [selected, setSelected] = useState<CompanyDetail | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [modal, setModal] = useState<Modal | null>(null);
  const [graphSeed, setGraphSeed] = useState<string | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);

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
    return companies.filter((c) => {
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
  }, [companies, search, topic, companyType, kind, country]);

  function reorderColumns(ids: string[]) {
    setOrder(ids);
    saveColumnOrder(ids);
  }

  function updateCompanyKind(name: string, newKind: string | null) {
    setCompanies((cs) => cs.map((c) => (c.name === name ? { ...c, kind: newKind } : c)));
    setSelected((s) => (s && s.name === name ? { ...s, kind: newKind } : s));
  }

  function openCompany(name: string) {
    fetchCompany(name)
      .then(setSelected)
      .catch((e) => setError(String(e)));
  }

  function openGraphFor(name: string) {
    setGraphSeed(name);
    setGraphOpen(true);
  }

  const modalButtons: { modal: Modal; label: string; title: string }[] = [
    { modal: "backlog", label: "📋 Backlog", title: "Ranked un-researched stubs — review and research" },
    { modal: "activity", label: "📡 Activity", title: "Live agent job activity — running, completed, failed" },
    { modal: "whatsnew", label: "🆕 What's new", title: "Recent signals across all companies — news, blog posts, events" },
    { modal: "digest", label: "📰 Digest", title: "Weekly digest — what changed: new signals, newly-researched companies, notable changes" },
    { modal: "ma", label: "🤝 M&A", title: "Mergers & acquisitions — recent deals across the space, filter by topic/acquirer" },
    { modal: "resolve", label: "🧩 Resolve stubs", title: "Dedup stub companies" },
    { modal: "classify", label: "🏷 Classify clients", title: "Bulk-label end-customer stubs as clients" },
  ];

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
            <button className="chat-toggle" onClick={() => reorderColumns([])} title="Reset column order">
              ↺ Columns
            </button>
          )}
          {modalButtons.map((b) => (
            <button key={b.modal} className="chat-toggle" onClick={() => setModal(b.modal)} title={b.title}>
              {b.label}
            </button>
          ))}
          <button
            className="chat-toggle"
            onClick={() => {
              setGraphSeed((s) => s ?? rows[0]?.name ?? null);
              setGraphOpen(true);
            }}
          >
            🕸 Graph
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

      <CompanyTable
        rows={rows}
        fields={fields}
        loading={loading}
        order={order}
        onReorder={reorderColumns}
        onOpenCompany={openCompany}
      />

      {graphOpen && (
        <GraphView
          seed={graphSeed}
          onClose={() => setGraphOpen(false)}
          onOpenCompany={openCompany}
        />
      )}

      {selected && (
        <CompanyDrawer
          company={selected}
          fields={fields}
          onClose={() => setSelected(null)}
          onKindChange={updateCompanyKind}
          onViewInGraph={openGraphFor}
        />
      )}
      {chatOpen && <ChatPanel onClose={() => setChatOpen(false)} />}
      {modal === "resolve" && <EntityResolutionModal onClose={() => setModal(null)} />}
      {modal === "classify" && <ClientClassificationModal onClose={() => setModal(null)} />}
      {modal === "backlog" && <BacklogModal onClose={() => setModal(null)} />}
      {modal === "activity" && <ActivityModal onClose={() => setModal(null)} />}
      {modal === "whatsnew" && <WhatsNewModal topics={topics} onClose={() => setModal(null)} />}
      {modal === "digest" && <DigestModal onClose={() => setModal(null)} />}
      {modal === "ma" && <MAPage topics={topics} onClose={() => setModal(null)} />}
    </div>
  );
}
