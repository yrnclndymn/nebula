# Nebula

An agentic research tool. You add a company (name + website); research agents
enrich it — HQ, LinkedIn, headcount, partners, investors, topic tags — and store
it in a **Neo4j graph** you can query across relationships (e.g. *all companies a
given VC has funded, that partner with Anthropic, with headcount < 100*). A React
front end presents the graph as filterable tables.

Replaces the manual Google Sheet workflow for tracking the **SAP ecosystem** and
**AI-native engineering** company/tool landscapes.

## Architecture

| Layer            | Tech                                                        |
| ---------------- | ----------------------------------------------------------- |
| Graph DB         | Neo4j — Docker locally, **Neo4j Aura** (managed) in prod     |
| Research agents  | Google ADK (Python), structured output → Cypher `MERGE`      |
| Backend API      | FastAPI + `neo4j` async driver → deployed on **Cloud Run**   |
| Frontend         | React + Vite SPA → **Firebase Hosting**                      |
| Auth             | **Firebase Auth** (Google), restricted to owner account(s)   |

Deploys into the same Firebase / GCP project as the `emergent-strategies` site
(as a separate hosting target or subdomain).

## Layout

```
nebula/
├── backend/            # uv project: FastAPI + ADK agents + Neo4j driver
│   └── app/{agents,graph,api}/
├── frontend/           # Vite + React SPA
├── docker-compose.yml  # local Neo4j
└── Makefile            # dev commands
```

## Getting started (local)

```bash
cp .env.example backend/.env      # local Neo4j creds are pre-filled
make db-up                        # start Neo4j (needs Docker) — http://localhost:7474
make install                      # backend deps (uv)
make dev                          # API on http://localhost:8080
make frontend-install             # frontend deps
make frontend-dev                 # SPA on http://localhost:5173
```

Verify wiring: `curl localhost:8080/health` (API up) and
`curl localhost:8080/health/graph` (Neo4j reachable).

Run `make` targets from the repo root. See the `Makefile` for the full list.

## Status

Skeleton stage. Health checks + Neo4j connection wired; graph model, Sheet
import, enrichment agents, and table UI still to build. See `CLAUDE.md` for the
build sequence.
