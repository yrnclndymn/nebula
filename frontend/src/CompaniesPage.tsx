import { useMemo, useState } from "react";
import { fetchCompany } from "./api";
import type { CompanyDetail, CompanyRow, FieldDef } from "./types";
import { KINDS, kindLabel } from "./types";
import { CompanyDrawer } from "./CompanyDrawer";
import { CompanyTable } from "./CompanyTable";
import { loadColumnOrder, saveColumnOrder } from "./columnOrder";
import { ClearFiltersButton, FilterBar, FilterSelect, HeadcountRange } from "./FilterBar";
import { headcountInRange } from "./filters";
import { GraphView } from "./GraphView";

// The Companies flow (#151): filterable table with a table ⇄ graph view toggle,
// plus the company/person drawers. Owns all browse state (filters, column order,
// selection); the shared dataset comes from the App shell.
export function CompaniesPage({
  companies,
  topics,
  types,
  countries,
  fields,
  loading,
  onKindChange,
  onError,
}: {
  companies: CompanyRow[];
  topics: string[];
  types: string[];
  countries: string[];
  fields: FieldDef[];
  loading: boolean;
  onKindChange: (name: string, kind: string | null) => void;
  onError: (msg: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [topic, setTopic] = useState("");
  const [companyType, setCompanyType] = useState("");
  const [kind, setKind] = useState("");
  const [country, setCountry] = useState("");
  // Headcount range (#7): min/max as strings ("" = unbounded); filtered client-side
  // over the already-loaded rows, like the other filters.
  const [hcMin, setHcMin] = useState("");
  const [hcMax, setHcMax] = useState("");

  const [order, setOrder] = useState<string[]>(loadColumnOrder);

  const [selected, setSelected] = useState<CompanyDetail | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [graphSeed, setGraphSeed] = useState<string | null>(null);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return companies.filter((c) => {
      if (topic && !c.topics.includes(topic)) return false;
      if (companyType && !c.companyTypes.includes(companyType)) return false;
      if (kind && c.kind !== kind) return false;
      if (country && c.hqCountry !== country) return false;
      if (!headcountInRange(c.headcount, hcMin, hcMax)) return false;
      if (needle) {
        const hay = `${c.name} ${c.about ?? ""} ${c.hqLocation ?? ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }, [companies, search, topic, companyType, kind, country, hcMin, hcMax]);

  function reorderColumns(ids: string[]) {
    setOrder(ids);
    saveColumnOrder(ids);
  }

  function openCompany(name: string) {
    fetchCompany(name)
      .then(setSelected)
      .catch((e) => onError(String(e)));
  }

  function openGraphFor(name: string) {
    setGraphSeed(name);
    setGraphOpen(true);
  }

  return (
    <section className="page">
      <div className="page-head">
        <strong>Companies</strong>
        <span className="page-head-tools">
          <span className="count">
            {loading ? "loading…" : `${rows.length} / ${companies.length} companies`}
          </span>
          {order.length > 0 && (
            <button className="chat-toggle" onClick={() => reorderColumns([])} title="Reset column order">
              ↺ Columns
            </button>
          )}
          <button
            className="chat-toggle"
            onClick={() => {
              setGraphSeed((s) => s ?? rows[0]?.name ?? null);
              setGraphOpen(true);
            }}
          >
            🕸 Graph
          </button>
        </span>
      </div>

      <FilterBar>
        <input
          className="search"
          placeholder="Search name, description, location…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <FilterSelect
          value={topic}
          onChange={setTopic}
          allLabel="All topics"
          options={topics.map((t) => ({ value: t, label: t }))}
        />
        <FilterSelect
          value={kind}
          onChange={setKind}
          allLabel="All kinds"
          options={KINDS.map((k) => ({ value: k, label: kindLabel(k) }))}
        />
        <FilterSelect
          value={country}
          onChange={setCountry}
          allLabel="All countries"
          options={countries.map((co) => ({ value: co, label: co }))}
        />
        <FilterSelect
          value={companyType}
          onChange={setCompanyType}
          allLabel="All types"
          options={types.map((t) => ({ value: t, label: t }))}
        />
        <HeadcountRange min={hcMin} max={hcMax} onMin={setHcMin} onMax={setHcMax} />
        {(search || topic || companyType || kind || country || hcMin || hcMax) && (
          <ClearFiltersButton
            onClear={() => {
              setSearch("");
              setTopic("");
              setCompanyType("");
              setKind("");
              setCountry("");
              setHcMin("");
              setHcMax("");
            }}
          />
        )}
      </FilterBar>

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
          onKindChange={(name, k) => {
            onKindChange(name, k);
            setSelected((s) => (s && s.name === name ? { ...s, kind: k } : s));
          }}
          onViewInGraph={openGraphFor}
        />
      )}
    </section>
  );
}
