# Research agents

Google ADK agents that enrich the graph. The first one to build:

**Enrichment agent** — input `{name, website}` → runs search + scrape tools →
returns a validated structured payload (HQ, LinkedIn URL, headcount, partners,
topic tags, …) → the API upserts it into Neo4j with a Cypher `MERGE`.

Lift heavily from `../../../../adk-workspace/company_linkedin_profile_agent/`
(LinkedIn enrichment) and reuse its research toolkit deps (`ddgs`,
`beautifulsoup4`, `playwright`). Each agent exposes a `root_agent` per ADK
convention and should return **structured output** so the DB write is
deterministic.
