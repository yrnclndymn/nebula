"""The enrichment agent (ADK): given a company, research it and save it.

This is Nebula's first real *agent* (vs. the importer's one-shot LLM call): it
reasons in a loop, decides which tools to call, and produces a side effect
(a graph write). Tools: two read tools (web_search, fetch_page) and one write
tool (save_company). `root_agent` is the ADK entry point — `adk web`/`adk run`
discover it, and `enrich.py` runs it programmatically.
"""

from google.adk.agents import Agent

from app.config import settings
from app.tools.graph_tools import save_company
from app.tools.web import fetch_page, web_search

_INSTRUCTION = """You are the research agent for Nebula, a company-research graph.
You are given a company's name, website, and a research topic. Gather factual
information about the company and save it to the graph.

Process:
1. First call fetch_page on the company's website for the basics (what they do,
   HQ, leadership, founding).
2. Use web_search for anything still missing: HQ location, headcount, year
   founded, funding/investors, notable partnerships, notable clients, leadership
   (names + titles), and whether it is a B-Corp / ESOP / employee-owned /
   co-operative / non-profit. fetch_page on the most promising results to confirm.
3. Then call save_company EXACTLY ONCE with everything you found. Use "" for
   unknown text, 0 for unknown numbers, and [] for unknown lists. Format each
   leadership entry as "Name | Title". Pass the topic through unchanged.
4. Only record facts you actually found in the sources — never guess or invent.
5. Finish with a 2-3 sentence summary of what you saved and any gaps.
"""

root_agent = Agent(
    name="enrichment_agent",
    model=settings.agent_model,
    description="Researches a company from its name + website and saves structured facts to the Nebula graph.",
    instruction=_INSTRUCTION,
    tools=[fetch_page, web_search, save_company],
)
