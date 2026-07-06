"""Targeted extraction of one custom field for a company, over its (cached) site.

Used by the back-fill: instead of re-running the whole enrichment agent, we fetch
the company's relevant pages (homepage + a services/'what we do' page, from cache)
and run a single structured LLM call for just the requested field. Cheap because
the pages are cached after the first research.
"""

import asyncio
import re

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.genai_retry import generate_with_retry
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


# Some fields are really a social/profile URL. The value is an external <a href> in
# the site footer (often just an icon, no visible text) — but fetch_page strips
# external links and the text has no URL, so the LLM never sees it. Grab it
# deterministically instead. Domains are ordered most-specific first.
_SOCIAL_DOMAINS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com/company", "linkedin.com/school", "linkedin.com/in", "linkedin.com"),
    "twitter": ("x.com", "twitter.com"),
    "x": ("x.com", "twitter.com"),
    "github": ("github.com",),
    "facebook": ("facebook.com",),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "crunchbase": ("crunchbase.com",),
}
_SHARE_MARKERS = ("/share", "sharer", "/intent", "sharearticle", "/sharing", "/shareon")


def _social_domains_for(label: str) -> tuple[str, ...]:
    """The social domains a field label maps to (e.g. 'LinkedIn' → linkedin.com), or
    () if it isn't a known social/profile field."""
    low = label.lower()
    for key, domains in _SOCIAL_DOMAINS.items():
        if re.search(rf"\b{re.escape(key)}\b", low):
            return domains
    return ()


def _pick_social_href(html: str, domains: tuple[str, ...]) -> str | None:
    """Pick the best matching profile URL from a page's hrefs (skips share links)."""
    hrefs = re.findall(r'href=["\']([^"\'#\s]+)["\']', html, re.I)
    hits = [
        h
        for h in hrefs
        if any(d in h.lower() for d in domains) and not any(m in h.lower() for m in _SHARE_MARKERS)
    ]
    if not hits:
        return None
    for pref in domains:  # prefer a company/profile path over a bare domain
        for h in hits:
            if pref in h.lower():
                return h.split("?")[0]
    return hits[0].split("?")[0]


def _find_social_url(start: str, domains: tuple[str, ...]) -> str | None:
    try:
        resp = requests.get(start, timeout=12, headers=_HEADERS)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    return _pick_social_href(resp.text, domains)


async def extract_field(website: str, label: str, description: str, field_type: str) -> dict:
    """Return {"value": list|str, "source": url} for one field, or empty if not found."""
    empty = [] if field_type == "list" else ""
    start = website if website.startswith("http") else "https://" + website

    # Social/profile URL fields: grab the footer link directly (the LLM path can't
    # see it), skipping the page crawl + LLM call entirely.
    domains = _social_domains_for(label)
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
