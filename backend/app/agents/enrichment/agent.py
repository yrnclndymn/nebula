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
from app.tools.web import fetch_page, find_clients, web_search

_INSTRUCTION = """You are the research agent for Nebula, a company-research graph.
You are given a company's name, website, and a research topic. Gather factual
information about the company and save it to the graph.

Process:
1. First call fetch_page on the company's website for the basics (what they do,
   HQ, leadership, founding). Its `social` field holds the company's own profile
   URLs — use social.linkedin as the LinkedIn value whenever it's present: it's the
   canonical link the company publishes. Only fall back to web_search for LinkedIn
   if the site exposes none.
2. Use web_search for anything still missing: HQ location, headcount, year
   founded, funding/investors, notable partnerships, leadership (names + titles),
   and whether it is a B-Corp / ESOP / employee-owned / co-operative / non-profit.
   fetch_page on the most promising results to confirm. Prefer the site's
   social.linkedin over a LinkedIn URL from search (search often returns a
   country-subdomain variant like uk.linkedin.com).

2b. To find CLIENTS / customers, call find_clients(website) ONCE. It crawls the
   company's client / "who we've helped" / case-study pages and their sub-pages and
   reads the client logos for you. Use its `clients` list directly — do not try to
   crawl for clients yourself, and don't stop at the handful mentioned in body text.
3. Then call save_company EXACTLY ONCE with everything you found. Use "" for
   unknown text, 0 for unknown numbers, and [] for unknown lists. Format each
   leadership entry as "Name | Title". Pass the topic through unchanged. Include
   the company's LinkedIn profile URL if found.
4. Every value you save MUST be directly stated in text you retrieved with a tool,
   and you must CITE it. Never guess, infer, estimate, extrapolate, or project.
   - For each checkable fact — especially every financial figure (funding,
     estimated_revenue) and headcount, plus year_founded and hq_location — add a
     `citations` entry "field | value | source_url | source_date", where source_url
     is the exact page you read it on and source_date is when the info is from.
   - If you cannot cite a specific source for a financial figure or headcount, DO
     NOT save it — leave it "" or 0. A number without a citation is a bug, and the
     system will DISCARD any uncited funding / estimated_revenue / headcount value,
     so an uncited number is wasted work — always cite these.
   - Record the source's own date; do not invent or forward-date it.
5. Finish with a 2-3 sentence summary of what you saved and any gaps. If you left
   financials or headcount empty because no source stated them, say so.
"""

root_agent = Agent(
    name="enrichment_agent",
    model=settings.agent_model,
    description="Researches a company from its name + website and saves structured facts to the Nebula graph.",
    instruction=_INSTRUCTION,
    tools=[fetch_page, web_search, find_clients, save_company],
)
