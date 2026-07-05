"""Targeted extraction of one custom field for a company, over its (cached) site.

Used by the back-fill: instead of re-running the whole enrichment agent, we fetch
the company's relevant pages (homepage + a services/'what we do' page, from cache)
and run a single structured LLM call for just the requested field. Cheap because
the pages are cached after the first research.
"""

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.genai_retry import generate_with_retry
from app.tools.web import fetch_page

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


async def extract_field(website: str, label: str, description: str, field_type: str) -> dict:
    """Return {"value": list|str, "source": url} for one field, or empty if not found."""
    empty = [] if field_type == "list" else ""
    start = website if website.startswith("http") else "https://" + website
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
    resp = await generate_with_retry(
        genai.Client(),
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
