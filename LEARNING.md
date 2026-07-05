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
| **2 — Tools / MCP** | function-calling, tool design, read vs. write tools | `app/tools/web.py` (`web_search`, `fetch_page`), `app/tools/graph_tools.py` (`save_company`) | ✅ tools done · ⬜ MCP server (expose the graph) next |
| **3 — Context / Memory** | sessions (short-term), long-term memory, stateful agents | `enrich.py` uses `InMemorySessionService` (ephemeral) | ⬜ persistent sessions + a chat-over-graph assistant |
| **4 — Quality** | observability (traces), LLM-as-Judge, trajectory eval | `enrich.py` prints the tool-call trajectory (first observability taste) | ⬜ eval harness + tracing (copy `../../agy2-projects/ambient-expense-agent` eval setup) |
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

## Suggested next builds (in course order)

1. **MCP server** exposing the graph (`query`, `companies`, `upsert`) — Day 2; also
   usable from Claude Code/Desktop.
2. **Eval harness** — Day 4; an evalset of known companies + LLM-as-Judge scoring of
   enrichment accuracy, wired to `make eval`. Real regression insurance.
3. **Memory + chat assistant** over the graph — Day 3.
4. **Multi-agent** decomposition (basics / funding / partnerships sub-agents) — Day 1/5.
