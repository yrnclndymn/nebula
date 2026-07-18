import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import "./App.css";
import {
  fetchCompanies,
  fetchCompanyTypes,
  fetchCountries,
  fetchFields,
  fetchTopics,
} from "./api";
import type { CompanyRow, FieldDef } from "./types";
import { ChatPanel } from "./ChatPanel";
import { ClassifyPage } from "./ClientClassification";
import { CompaniesPage } from "./CompaniesPage";
import { DigestPage } from "./DigestPanel";
import { ResolvePage } from "./EntityResolution";
import { MAPage } from "./MAPage";
import { QueuePage } from "./QueuePanel";
import { Sidebar } from "./Sidebar";
import { WhatsNewPage } from "./WhatsNew";

// Tab strip for a flow with sub-views (Review, News). Tabs are sub-routes, so
// each is deep-linkable and the back button walks them.
function TabbedFlow({ tabs }: { tabs: { to: string; label: string }[] }) {
  return (
    <div>
      <nav className="tabs">
        {tabs.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            className={({ isActive }) => (isActive ? "tab-link active" : "tab-link")}
          >
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </div>
  );
}

const REVIEW_TABS = [
  { to: "queue", label: "📋 Queue & activity" },
  { to: "resolve", label: "🧩 Resolve stubs" },
  { to: "classify", label: "🏷 Classify clients" },
];

const NEWS_TABS = [
  { to: "whatsnew", label: "🆕 What's new" },
  { to: "digest", label: "📰 Digest" },
  { to: "ma", label: "🤝 M&A" },
];

export default function App() {
  const [companies, setCompanies] = useState<CompanyRow[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [fields, setFields] = useState<FieldDef[]>([]);
  const [countries, setCountries] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [chatOpen, setChatOpen] = useState(false);

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

  function updateCompanyKind(name: string, newKind: string | null) {
    setCompanies((cs) => cs.map((c) => (c.name === name ? { ...c, kind: newKind } : c)));
  }

  return (
    <BrowserRouter>
      <div className={chatOpen ? "app-shell chat-open" : "app-shell"}>
        <Sidebar chatOpen={chatOpen} onToggleChat={() => setChatOpen((v) => !v)} />
        <main className="app">
          {error && <div className="error">{error}</div>}
          <Routes>
            <Route path="/" element={<Navigate to="/companies" replace />} />
            <Route
              path="/companies"
              element={
                <CompaniesPage
                  companies={companies}
                  topics={topics}
                  types={types}
                  countries={countries}
                  fields={fields}
                  loading={loading}
                  onKindChange={updateCompanyKind}
                  onError={setError}
                />
              }
            />
            <Route path="/review" element={<TabbedFlow tabs={REVIEW_TABS} />}>
              <Route index element={<Navigate to="queue" replace />} />
              <Route path="queue" element={<QueuePage />} />
              <Route path="resolve" element={<ResolvePage />} />
              <Route path="classify" element={<ClassifyPage />} />
            </Route>
            <Route path="/news" element={<TabbedFlow tabs={NEWS_TABS} />}>
              <Route index element={<Navigate to="whatsnew" replace />} />
              <Route path="whatsnew" element={<WhatsNewPage topics={topics} />} />
              <Route path="digest" element={<DigestPage />} />
              <Route path="ma" element={<MAPage topics={topics} />} />
            </Route>
            <Route path="*" element={<Navigate to="/companies" replace />} />
          </Routes>
        </main>
        {chatOpen && <ChatPanel onClose={() => setChatOpen(false)} />}
      </div>
    </BrowserRouter>
  );
}
