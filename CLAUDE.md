# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Nebula is

An agentic research tool that replaces a manual Google Sheet. The user tracks two
domains — the **SAP ecosystem** (companies) and **AI-native engineering**
(companies + tools, later techniques). Flow: add a company `{name, website}` →
research agents enrich it → store in a **Neo4j graph** → browse/query via a React
SPA. The graph (not tables) is the deliberate core: dimensions and relationship
types are meant to grow over time (funding, VCs, acquisitions, people…) without
schema migrations, and queries traverse relationships (e.g. *companies a VC
funded that partner with Anthropic and have headcount < 100*).

## Architecture

- **Graph DB — Neo4j.** Docker locally (`docker-compose.yml`), **Neo4j Aura**
  (managed) in production. Never assume a fixed relational schema — model new
  facts as nodes/edges.
- **Research agents — Google ADK (Python).** Each agent exposes a `root_agent`
  and returns **structured output** so the DB write is deterministic; the API
  upserts with Cypher `MERGE`. Live in `backend/app/agents/`.
- **Backend API — FastAPI** (`backend/app/`), async `neo4j` driver held for the
  app lifetime (`app/graph/driver.py`), deployed to **Cloud Run**.
- **Frontend — React + Vite SPA** (`frontend/`), deployed to **Firebase Hosting**.
- **Auth — Firebase Auth (Google provider)**, restricted to the owner. SPA gates
  on sign-in; the backend verifies the Firebase ID token. *(Not yet built.)*
- Deploys into the **same Firebase/GCP project as `emergent-strategies`** (the
  user's personal site), as a separate hosting target or subdomain.

## Commands

Run `make` targets from the repo root:

```bash
make db-up            # start local Neo4j (needs Docker) — browser at :7474
make install          # backend deps (uv sync in backend/)
make dev              # FastAPI on :8080 with reload
make test             # backend pytest
make lint             # ruff check + format --check
make frontend-install # npm install in frontend/
make frontend-dev     # Vite dev server on :5173
```

Single backend test: `cd backend && uv run pytest tests/test_health.py::test_health_ok`.

Health wiring: `GET /health` (process up, no DB) and `GET /health/graph`
(pings Neo4j, 503 if down).

## Conventions

- **Backend is a `uv` project** rooted at `backend/` — run Python via `uv run`,
  add deps with `uv add`. Not the repo root. Imports are absolute from `app`
  (e.g. `from app.graph.driver import ...`).
- **Config via `app/config.py`** (pydantic-settings). Local Neo4j creds default
  in-code; real creds come from `backend/.env` (copy `.env.example`), never
  committed. Aura creds use the `neo4j+s://` scheme.
- **Gemini models** in ADK agents (this and the user's other agent work target
  Google's stack, not the Claude API).
- Reusable prior art in the sibling `../adk-workspace/`:
  `company_linkedin_profile_agent/` (LinkedIn enrichment) and
  `google_sheets_agent/` (import the seed Sheet). Its deps show the research
  toolkit: `ddgs`, `beautifulsoup4`, `lxml`, `playwright`.

## Build sequence

1. ~~**Graph model**~~ — done. Node/edge types, constraints, and the
   `CompanyRecord` → `upsert_company` write path live in `app/graph/`
   (`schema.py`, `models.py`, `repository.py`; see `app/graph/README.md`).
   `make db-init` applies constraints.
2. ~~**Seed import**~~ — done. `app/importer/` reads a CSV export of the sheet:
   deterministic columns map straight through; freeform columns (Notes,
   Leadership, Partnerships, Clients) go through a Gemini structured-output
   extractor (`extract.py`) that splits year-founded / funding / company-type /
   residual notes and parses people+titles. Run: `make import CSV=data/x.csv
   TOPIC="SAP ecosystem"` (`--dry-run` to preview, `--no-llm` for a cheap
   heuristic pass). Verified end-to-end against local Neo4j.
3. ~~**Enrichment agent**~~ — done. A real ADK agent (`app/agents/enrichment/`,
   `root_agent`) with a tools module (`app/tools/`: `web_search`, `fetch_page`,
   `save_company`). `{name, website, topic}` → reasoning loop → `upsert_company`.
   Run: `make enrich NAME=... WEBSITE=... TOPIC=...` (prints the tool trajectory).
   Uses `gemini-3.1-flash-lite` (2.5-flash free tier is ~20 req/day). This is the
   hands-on surface for the agents course — see `LEARNING.md`.
4. ~~**API + SPA tables**~~ — done. Read queries in `app/graph/queries.py`;
   endpoints in `app/api/routes.py` (`/companies` with topic/search/type/headcount
   filters, `/companies/{name}` detail, `/topics`, `/company-types`). Frontend
   (`frontend/src/`): `App.tsx` filterable+sortable table, `CompanyDrawer.tsx`
   detail panel, `api.ts`/`types.ts`. Fetches all rows once, filters client-side
   (dataset is small). Run both: `make dev` + `make frontend-dev`.
5. **Auth + deploy** — Firebase Auth gate; Cloud Run (API) + Firebase Hosting (SPA).

**Model note:** the LLM extractor now uses **Gemini** (`gemini-3.1-flash-lite`),
but the provider is a live decision (see the `model-picker` skill / the "never
choose from memory" rule above) — moving the LLM layer to Claude is on the table.

**Gemini auth:** `google-genai` reads `GEMINI_API_KEY` / `GOOGLE_API_KEY` from
the env (already set in Andy's shell). Model in `app/config.py`
(`gemini-3.1-flash-lite`), overridable via `GEMINI_MODEL`.

**Picking an LLM model — never choose from memory.** Model lineups move faster
than any training cutoff (Gemini 3.x post-dates it). Before selecting or changing
a Gemini model, list what's actually available to the key:
`uv run python -c "import google.genai as g; [print(m.name) for m in g.Client().models.list()]"`.
For Claude/Anthropic models, use the `claude-api` skill (kept current beyond
training). Prefer the live list / skill over any hard-coded belief about versions.

The graph write path is deliberately shared: the Sheet importer (step 2) and the
agents (step 3) both build a `CompanyRecord` and call `upsert_company`.
