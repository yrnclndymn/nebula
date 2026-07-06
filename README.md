# Nebula

[![CI](https://github.com/yrnclndymn/nebula/actions/workflows/ci.yml/badge.svg)](https://github.com/yrnclndymn/nebula/actions/workflows/ci.yml)

A private, agentic research tool that replaces a manual spreadsheet for tracking
different company and tool landscapes - in particular, those in the **AI-native engineering** space.

You add a company as `{name, website}`. Research agents enrich it — HQ, headcount,
leadership, partners, clients, investors — with a **source citation attached to
every fact**. It all lands in a **Neo4j graph** you can query across relationships,
and a React SPA presents it as filterable tables with a chat assistant.

Built solo, and **running in production** on Cloud Run + Firebase + Neo4j Aura,
behind Google sign-in.

## Why a graph, not a spreadsheet or SQL

The point is questions that span relationships, over dimensions that keep growing:

> _companies a given VC has funded, that partner with Anthropic, with headcount < 100_

New dimensions (funding rounds, acquisitions, people, tools) become nodes and edges
with no schema migration. That's awkward in a relational schema and impossible in a
sheet.

## What's interesting in here

- **Research agents with provenance.** Google ADK agents enrich a company and return
  structured output. A guardrail drops any funding/headcount/revenue claim that
  arrives without a source, so no number lands uncited.
- **Human-in-the-loop writes.** The chat assistant can _propose_ an enrichment but
  not commit it — you review the result and commit. Same for schema changes and
  batch back-fills.
- **Background jobs that survive scale-to-zero.** Long research runs go through
  Cloud Tasks to an OIDC-authed `/jobs/run` endpoint, with job state stored in the
  graph. So the API scales to zero (≈ $0 idle) and jobs still complete and stay
  pollable across cold starts and instances.
- **Deterministic client discovery.** Rather than hoping the LLM spots every client
  logo, a tool crawls the site's client pages and reads the logos with vision, then
  the agent reasons over that list.
- **Graph-backed crawl cache.** Fetched pages and client lists are cached as nodes
  with a TTL, so repeat questions about a company don't re-crawl — chosen over
  SQLite because Cloud Run is ephemeral and multi-instance.
- **User-extensible schema.** Add a custom field from chat (scoped to a company
  kind), then batch-research it across existing companies with a review step.
- **MCP server.** The same graph is exposed to Claude Code / Desktop as MCP tools
  (reads + guarded read-only Cypher + enrichment).

## Architecture

```
Browser ──▶ Firebase Hosting (SPA) ──/api/**──▶ Cloud Run (FastAPI + ADK agents)
             Firebase Auth (Google)                │ ├──▶ Neo4j Aura (graph)
                                                    │ ├──▶ Gemini (research)
                                                    │ └──▶ Cloud Tasks ──▶ /jobs/run
Claude Code/Desktop ──▶ MCP server ──▶ Neo4j
```

| Layer           | Tech                                                         |
| --------------- | ----------------------------------------------------------- |
| Graph DB        | Neo4j — Docker locally, Neo4j Aura in production            |
| Research agents | Google ADK (Python), Gemini, structured output → Cypher    |
| Backend API     | FastAPI + async `neo4j` driver, on Cloud Run (scale-to-zero) |
| Background jobs | Cloud Tasks → OIDC-authed `/jobs/run`, state in the graph  |
| Frontend        | React + Vite SPA, on Firebase Hosting                      |
| Auth            | Firebase Auth (Google), server-verified, email allowlist   |
| Interop         | MCP server (stdio) over the same graph                     |

Same-origin in production: the SPA calls `/api/**`, which Firebase Hosting rewrites
to Cloud Run — one domain, no CORS, and the backend verifies the Firebase ID token
on every request.

## Local quickstart

```bash
cp .env.example backend/.env   # local Neo4j creds are pre-filled
make db-up                     # local Neo4j (Docker) — http://localhost:7474
make install                   # backend deps (uv)
make dev                       # API on http://localhost:8080
make frontend-install
make frontend-dev              # SPA on http://localhost:5173
```

Verify: `curl localhost:8080/health` (API up) and `curl localhost:8080/health/graph`
(Neo4j reachable). Everything is a `make` target — see the `Makefile`.

## Layout

```
backend/     uv project — FastAPI, ADK agents, Neo4j driver, MCP server
  app/{agents,graph,api,tools}/
frontend/    Vite + React SPA
docker-compose.yml   local Neo4j
Makefile     dev commands
```

## Further reading

- **`DEPLOYMENT.md`** — production architecture and the reasoning (scale-to-zero,
  auth model, Cloud Tasks).
- **`RUNBOOK.md`** — the exact deploy steps.
- **`LEARNING.md`** — how the build maps to graph-backed agent concepts.
- **`CLAUDE.md`** — orientation for working in the codebase.
