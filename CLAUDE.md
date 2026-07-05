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

## Build sequence (status: skeleton)

Health checks + Neo4j connection are wired. Remaining, in order:

1. **Graph model** — node/edge types + uniqueness constraints in `app/graph/`.
2. **Seed import** — pull the existing Google Sheet into the graph.
3. **Enrichment agent** — `{name, website}` → search/scrape → structured output → upsert.
4. **API + SPA tables** — query endpoints and curated table views.
5. **Auth + deploy** — Firebase Auth gate; Cloud Run (API) + Firebase Hosting (SPA).
