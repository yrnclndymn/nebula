"""Targeted extraction of one custom field for a company, over its (cached) site.

Used by the back-fill: instead of re-running the whole enrichment agent, we fetch
the company's relevant pages (homepage + a services/'what we do' page, from cache)
and run a single structured LLM call for just the requested field. Cheap because
the pages are cached after the first research.
"""

import asyncio

import requests
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app import llm
from app.tools.social import normalize_linkedin, pick_social_href, social_domains_for
from app.tools.web import _HEADERS, fetch_page

_PAGE_KEYWORDS = (
    "service",
    "what-we-do",
    "what we do",
    "capabilit",
    "expertise",
    "solution",
    "practice",
    "offering",
    "approach",
)


class _FieldExtract(BaseModel):
    values: list[str]
    source_url: str


def _find_social_url(start: str, domains: tuple[str, ...]) -> str | None:
    """Fetch a page and pick the best profile URL from its hrefs (LinkedIn canonicalised)."""
    try:
        resp = requests.get(start, timeout=12, headers=_HEADERS)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    href = pick_social_href(resp.text, domains)
    return normalize_linkedin(href) if href else None


async def extract_field(website: str, label: str, description: str, field_type: str) -> dict:
    """Return {"value": list|str, "source": url} for one field, or empty if not found."""
    empty = [] if field_type == "list" else ""
    start = website if website.startswith("http") else "https://" + website

    # Social/profile URL fields: grab the footer link directly (the LLM path can't
    # see it), skipping the page crawl + LLM call entirely.
    domains = social_domains_for(label)
    if domains:
        url = await asyncio.to_thread(_find_social_url, start, domains)
        if url:
            return {"value": [url] if field_type == "list" else url, "source": start}
        return {"value": empty, "source": ""}

    home = await fetch_page(start)
    if "error" in home:
        return {"value": empty, "source": ""}

    candidates = [
        link["url"]
        for link in home.get("links", [])
        if any(k in (link["url"] + " " + link.get("text", "")).lower() for k in _PAGE_KEYWORDS)
    ]
    texts = [home.get("text", "")]
    for url in candidates[:2]:
        page = await fetch_page(url)
        if "error" not in page:
            texts.append(page.get("text", ""))

    shape = (
        "Return the list of items." if field_type == "list" else "Return a single concise value."
    )
    prompt = (
        f"For the company at {start}, find: {label} — {description}. {shape} "
        "Use only what is stated in the text; if not found, return an empty list. "
        "Also return the source_url (one of the pages below) you took it from.\n\n"
        + " ".join(texts)[:12000]
    )
    resp = await llm.generate(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_FieldExtract,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    if not isinstance(parsed, _FieldExtract) or not parsed.values:
        return {"value": empty, "source": ""}
    value = parsed.values if field_type == "list" else parsed.values[0]
    return {"value": value, "source": parsed.source_url or start}
