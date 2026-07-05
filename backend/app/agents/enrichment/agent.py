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
from app.tools.web import fetch_page, identify_logos, web_search

_INSTRUCTION = """You are the research agent for Nebula, a company-research graph.
You are given a company's name, website, and a research topic. Gather factual
information about the company and save it to the graph.

Process:
1. First call fetch_page on the company's website for the basics (what they do,
   HQ, leadership, founding).
2. Use web_search for anything still missing: HQ location, headcount, year
   founded, funding/investors, notable partnerships, leadership (names + titles),
   and whether it is a B-Corp / ESOP / employee-owned / co-operative / non-profit.
   fetch_page on the most promising results to confirm.

2b. CLIENTS & PARTNERS are usually shown as LOGOS, often on a dedicated page. From
   fetch_page's `links`, open pages whose text/URL suggests clients — e.g.
   "clients", "who we've helped", "customers", "case studies", "work", "customer
   stories" — AND follow their sub-pages (e.g. per-sector pages like /defence/,
   /healthcare/). On each such page:
   - Read client names from `images`: logo filenames and alt text usually contain
     the company (e.g. "logo-equifax.jpg" → Equifax).
   - For logos whose company you can't tell from the filename/alt, collect their
     image `src` URLs and call identify_logos to read them from the image.
   - Also take names from the page text.
   Be thorough: a firm may list 20+ clients across several sub-pages — gather them
   all, don't stop at the first handful.
3. Then call save_company EXACTLY ONCE with everything you found. Use "" for
   unknown text, 0 for unknown numbers, and [] for unknown lists. Format each
   leadership entry as "Name | Title". Pass the topic through unchanged.
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
    tools=[fetch_page, web_search, identify_logos, save_company],
)
