# Learning agentic development with Nebula

This project doubles as a hands-on lab for the
[Kaggle × Google 5-Day AI Agents Intensive](https://www.kaggle.com/learn-guide/5-day-agents).
The course is built around five agent components — **Models, Tools, Orchestration,
Memory, Evaluation** — plus **AgentOps** (reliability, governance, security). Each
day below maps to real code you can run and extend.

The useful contrast this project gives you: the **CSV importer is *not* an agent**
(a single structured LLM call, `app/importer/extract.py`), while the **enrichment
step *is*** (a reasoning loop with tools, `app/agents/enrichment/`). Comparing them
is Day 1's core lesson — *when is a plain model call enough vs. when do you need an
agent?*

| Day | Concept | Where in the code | Status |
| --- | ------- | ----------------- | ------ |
| **1 — Agents** | reasoning loop, agent vs. one-shot call, agent components | `app/agents/enrichment/agent.py` (`root_agent`), run loop in `enrich.py`; baseline non-agent in `app/importer/extract.py` | ✅ done |
| **2 — Tools / MCP** | function-calling, tool design, read vs. write tools, **MCP interoperability** | `app/tools/` (agent tools) + `app/mcp_server.py` (MCP server exposing the graph); registered in `.mcp.json` | ✅ done |
| **3 — Context / Memory** | sessions (short-term), **long-term memory**, stateful agents | `app/agents/assistant/`: chat agent + graph tools; session = multi-turn per run; `memory.py` = long-term memory as `(:Memory)` nodes | ✅ done (`make chat`) |
| **4 — Quality** | observability, **LLM-as-Judge**, **trajectory eval**, field checks | `backend/evals/`: golden dataset, deterministic field checks, `judge.py` (LLM-as-Judge), trajectory checks; `enrich.py` captures the tool trajectory | ✅ eval done · ⬜ Cloud Trace / structured tracing |
| **5 — Production** | deployment, A2A, security/governance | roadmap step 5 | ⬜ Cloud Run + Firebase Auth + (optionally) Vertex AI Agent Engine |

## Try it

```bash
make enrich NAME="Anthropic" WEBSITE="anthropic.com" TOPIC="AI-native engineering"
```

Watch the printed trajectory: the agent decides to `fetch_page` the site, falls
back to `web_search` for gaps, `fetch_page`s a promising result, then calls
`save_company` once — and you can inspect the reasoning path, which is exactly
what Day 4's observability is about.

## Notes / decisions

- **Provider.** The course is ADK + Gemini native, so the agent uses ADK; concepts
  (tools, memory, eval, MCP, multi-agent) are provider-agnostic and portable to
  Claude's Agent SDK later. See the `model-picker` skill and `CLAUDE.md`.
- **Free-tier quota is tight.** `gemini-2.5-flash` free tier is ~20 requests/day;
  an agent makes several calls per run, so the agent uses `gemini-3.1-flash-lite`
  (far more headroom). For sustained agent + eval work, consider a paid Gemini tier
  or the Claude route.
- **Shared write path.** Both the importer and the agent produce a `CompanyRecord`
  and call `upsert_company`, so a sheet import and an agent enrichment land
  identically in the graph.

## MCP server

`app/mcp_server.py` (FastMCP, stdio) exposes the graph to any MCP client — Claude
Code, Claude Desktop, or an agent. Tools: `search_companies`, `get_company`,
`list_topics`, `list_company_types`, `graph_overview`, `run_cypher` (READ-ONLY,
write clauses rejected — a guardrail is the security cross-cutting theme), and
`enrich_company` (runs the agent; slow, writes). Registered in `.mcp.json`.

Use it from Claude Code: `claude mcp list` to confirm `nebula` is connected, then
ask e.g. *"using nebula, which employee-owned companies partner with Anthropic?"*
Requires local Neo4j running (`make db-up`).

## Eval harness

`backend/evals/` grades the enrichment agent (`make eval`; `ARGS=--grade-only`
re-grades cached traces without re-running the agent). Four scorers: deterministic
**field checks** (known values), **trajectory checks** (searched + fetched, saved
once), **provenance checks** (every financial figure / headcount saved must carry
a citation), and an **evidence-grounded LLM-as-Judge**.

Key lesson learned the hard way: the *first* judge scored against its own
knowledge and produced false hallucination flags (stale model calling real 2026
figures "overstated", past dates "future projections"). The fix was **provenance
+ evidence-grounded judging**: the agent now cites a source URL + date for each
fact (stored as `(:Company)-[:CITES]->(:Source)`), and the judge validates *"is
this value supported by the evidence the agent actually retrieved?"* rather than
its memory. Faithfulness went from avg 3.3 → 4.7 and the flags became trustworthy
(it still catches Replit citing an unsupported headcount). The provenance check
immediately surfaced that the agent sets `funding` without citing it. This is the
deepest Day-4 lesson: **you have to eval your eval**, and provenance is what makes
faithfulness checkable — in the eval and in production (`origin` + Sources show in
the company drawer).

Free-tier note: flash-lite is ~15 req/min, so the harness bursts into 429s; the
generate/grade split + `app/genai_retry.py` handle it.

## Chat assistant + memory

`make chat` (interactive) or `make chat ARGS="a question"` (one-shot) talks to a
research assistant over the graph. Two kinds of memory, which is the lesson:
- **Short-term (session):** multi-turn context within one run, via ADK's
  `InMemorySessionService` — the assistant remembers earlier turns.
- **Long-term:** durable facts stored as `(:Memory)` nodes in Neo4j
  (`app/agents/assistant/memory.py`), loaded at start and written by the `remember`
  tool — so a fact learned in one run is recalled in a later, separate run.
  (Verified: "remember I focus on employee-owned companies" persisted and was
  recalled by a fresh process.) Mirrors an ADK MemoryService, kept in the graph so
  it's persistent + inspectable.

## Suggested next builds (in course order)

1. **Structured tracing** — Day 4; Cloud Trace / OpenTelemetry over the agent runs.
2. **Day 5 — Production**: Firebase Auth + Cloud Run deploy (roadmap step 5).
3. **Multi-agent** decomposition (basics / funding / partnerships sub-agents) — Day 1/5.
