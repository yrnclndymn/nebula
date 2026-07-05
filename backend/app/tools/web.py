"""Web research tools for the enrichment agent.

These are ADK function tools: plain functions with type hints + a docstring, from
which ADK builds the function-calling schema the model sees. Keep signatures and
return shapes simple — the return value is fed straight back to the model.
"""

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NebulaResearchBot/0.1)"}


def web_search(query: str) -> dict:
    """Search the web for a query and return the top results.

    Returns up to 6 results, each with a title, url, and snippet. Use this to find
    a company's HQ, headcount, founding year, funding, partners, clients, or
    leadership when they aren't on the company's own site.
    """
    with DDGS() as ddgs:
        hits = ddgs.text(query, max_results=6)
    return {
        "results": [
            {"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")} for h in hits
        ]
    }


def fetch_page(url: str) -> dict:
    """Fetch a web page and return its readable text (truncated to ~5000 chars).

    Use this to read a company's website or a promising search result to confirm
    facts. Returns {"url", "text"} on success or {"url", "error"} on failure.
    """
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — hand any fetch error back to the model
        return {"url": url, "error": str(exc)}

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return {"url": url, "text": text[:5000]}
