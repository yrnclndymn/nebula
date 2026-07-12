# Graph model

Central node is `:Company`; columns that point at other entities become edges.

### Nodes

| Label          | Key (MERGE on) | Properties |
| -------------- | -------------- | ---------- |
| `:Company`     | `name` (unique) | `priority, about, source, website, linkedin, hqLocation, headcount, estimatedRevenue, yearFounded, funding, notes, updatedAt` |
| `:Person`      | `name` (indexed, not unique) | — |
| `:Topic`       | `name` (unique) | research domain, e.g. "SAP ecosystem", "AI-native engineering" |
| `:CompanyType` | `name` (unique) | e.g. "B-Corp", "ESOP" |
| `:Tool`        | `name` (unique) | `website` — AI-native domain, agent-populated later |

### Relationships

| Edge | Meaning |
| ---- | ------- |
| `(:Company)-[:PARTNERS_WITH]->(:Company)` | partnership (query undirected: `-[:PARTNERS_WITH]-`) |
| `(:Company)-[:HAS_CLIENT]->(:Company)`    | client relationship (directed: vendor → client) |
| `(:Person)-[:LEADS {title}]->(:Company)`  | leadership; role on the edge |
| `(:Company)-[:TAGGED_AS]->(:Topic)`       | research domain |
| `(:Company)-[:CLASSIFIED_AS]->(:CompanyType)` | b-corp, ESOP, … |
| `(:Company)-[:INVESTED_IN {round, amountUsd, announcedOn}]->(:Company)` | funding — reserved; agent-populated once `funding` text is structured |
| `(:Company)-[:MAKES]->(:Tool)`            | reserved; AI-native tools, agent-populated |

### Design notes

- **Partners, clients, and investor firms are all `:Company`** — a partner is an
  org that may later become a research target; the edge carries the meaning.
- **`funding` is raw text** from the sheet's notes for now. Structuring it into
  `:INVESTED_IN` edges is what unlocks "companies a given VC funded" queries.
- **People dedupe by name** — imperfect; the enrichment agent will re-key by
  LinkedIn URL.

### Example query (the motivating one)

```cypher
MATCH (vc:Company {name: $vc})-[:INVESTED_IN]->(c:Company)
MATCH (c)-[:PARTNERS_WITH]-(:Company {name: 'Anthropic'})
WHERE c.headcount < 100
RETURN c.name, c.headcount
```

Schema (constraints/indexes) lives in `schema.py`; writes in `repository.py`;
the record shape in `models.py`.

## Retention & pruning (issue #37)

Neo4j **Aura Free caps at 200,000 nodes**, and periodic signal capture (#34/#35)
grows the graph without bound. Retention is therefore a **launch requirement, not
cleanup**. Three scheduled prune jobs keep the graph bounded (declared in
`schedules.py`, dispatched by the schedule tick; each reports what it removed on
the `:Job` so the activity page shows it):

| Job | Deletes | Policy knob (in `config.py`) | Cadence |
| --- | --- | --- | --- |
| `cache_prune` | stale `:Page` / `:SiteClients` crawl-cache nodes | `cache_ttl_days` (pruned at 2×) | weekly |
| `job_prune` | old `:Job` history nodes | `job_retention_days` | daily |
| `signal_prune` | `:Signal` nodes past the caps below | `signal_max_per_company`, `signal_max_age_days` | weekly |

### Signal policy (the growth driver)

A signal is **kept** iff it clears **both** caps for at least one company that
mentions it, and **pruned** otherwise:

- **Count cap — `signal_max_per_company` (default 50).** Keep only the newest N
  signals per company **per kind** (`news` / `blog` / `event`). This is a *hard*
  node bound: `Signal` nodes ≤ `companies × kinds × N`.
- **Age cap — `signal_max_age_days` (default 365).** Drop anything older than
  this even for a company below the count cap, so stale news ages out.

**Why these defaults.** At ~200 tracked companies the count cap alone bounds
signals at `200 × 3 × 50 = 30K` nodes — comfortably under the 200K Aura cap even
counting linked `:Source` nodes and relationships — while a year of history is
plenty for a research view. Both are env-overridable if the graph or the tracked
set grows.

**Effective date** for both caps is `coalesce(publishedAt, capturedAt)` (the same
ordering the read queries use). A **shared** story (mentioned by several
companies) survives if it is still within cap for *any* of them — deleting the
node would remove it everywhere.

**Safety.** The prune never deletes a signal cited by **un-reviewed work** — a
`:Job` that is `ready` but not yet committed (the same exception `job_prune`
makes for proposals). Deletes are batched (irreversible), and the runner records
per-kind counts plus the resulting node total on the job.

The selection logic (which signals survive) is **pure and unit-tested** in
`retention.py` (`select_signals_to_prune`, `tests/test_retention.py`); the
scheduled runner and its deletion guard are integration-tested in
`tests/test_schedules.py`.

### Page-cache expiry for signal crawls

Pages fetched while capturing signals are ordinary `:Page` cache nodes
(`cache.py`), so the **existing `cache_prune` schedule already expires them** on
`cache_ttl_days` — its match is on the label, independent of what triggered the
fetch. No second cache pruner is needed.

### Observability

`GET /health/graph/size` reports total nodes / relationships against the 200K
cap plus the per-kind signal breakdown; the `signal_prune` job logs the same size
after each run.
