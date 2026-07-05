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
