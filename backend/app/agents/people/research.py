"""Person research: gather evidence, then one structured Gemini extraction.

Deliberately NOT an ADK reasoning loop — a bounded gather + a single
structured-output call keeps the commit deterministic (a fixed
:class:`PersonResearch` schema) and the spend easy to budget, matching the news
(#35) and discovery (#75) capture jobs rather than the free-form company agent.

Evidence sources reuse the shared tools (``web_search``, ``fetch_page``): a couple
of targeted searches for the person + company, and best-effort page fetches of the
person's known LinkedIn and the top organic results. All of it is UNTRUSTED — it
only feeds a *proposal* a human reviews, and provenance is re-checked
deterministically downstream (see :func:`app.agents.people.build.build_person_record`),
so a fabricated citation never survives to a write.

Every tool call charges the active per-run budget (``person_enrichment``), so the
gather stops cleanly at its caps.
"""

import asyncio
import logging

from google import genai
from google.genai import types

from app import budget
from app.agents.people.models import PersonResearch
from app.config import settings
from app.genai_retry import generate_with_retry
from app.tools.web import fetch_page, web_search

logger = logging.getLogger("nebula.people.research")

_MAX_EVIDENCE_PAGES = 3  # top organic results we fetch full text for
_EVIDENCE_CHARS = 16000  # cap the prompt payload


async def _gather_evidence(name: str, company: str, linkedin: str | None) -> list[str]:
    """Collect labelled evidence snippets for the extraction prompt.

    Best-effort and budget-charged: a failed fetch or a capped search simply
    contributes less evidence, never an error.
    """
    evidence: list[str] = []
    urls_to_fetch: list[str] = []
    if linkedin:
        urls_to_fetch.append(linkedin)

    for query in (f"{name} {company}", f"{name} {company} biography prior roles"):
        try:
            results = (await asyncio.to_thread(web_search, query)).get("results", [])
        except budget.BudgetExhausted:
            break
        for hit in results:
            url = hit.get("url") or ""
            evidence.append(f"[search] {hit.get('title', '')} — {url}\n{hit.get('snippet', '')}")
            if url.lower().startswith(("http://", "https://")) and len(urls_to_fetch) < (
                _MAX_EVIDENCE_PAGES + (1 if linkedin else 0)
            ):
                urls_to_fetch.append(url)

    for url in urls_to_fetch:
        try:
            page = await fetch_page(url)
        except budget.BudgetExhausted:
            break
        if "error" in page:
            continue
        social = page.get("social") or {}
        social_line = f"\nsocial links found on page: {social}" if social else ""
        evidence.append(f"[page] {url}\n{page.get('text', '')[:4000]}{social_line}")

    return evidence


_PROMPT = (
    "You are researching a specific person for a professional knowledge graph. Using "
    "ONLY the evidence below, extract what is publicly known about {name} "
    "(currently associated with {company}).\n\n"
    "Return, where the evidence supports it: their current title and company; a "
    "one-line professional bio; prior roles (title + company, with year span if "
    "shown); and public links — their personal LinkedIn profile URL, personal "
    "website, and links to public talks.\n\n"
    "CRITICAL: every fact you return MUST be backed by a citation whose `source` is "
    "the exact URL from the evidence where you found it. Use the field names: "
    "'title', 'bio', 'linkedin', 'personal_site', 'talks'. Do NOT invent facts, "
    "URLs, or citations — omit anything the evidence does not support. For each "
    "prior role, set its `source` to the URL it came from.\n\n"
    "EVIDENCE:\n{evidence}"
)


async def research_person(
    name: str, company: str, *, linkedin: str | None = None, verbose: bool = False
) -> PersonResearch:
    """Research one person and return raw, untrusted structured findings.

    ``company`` anchors the search (and later locates the graph node); ``linkedin``
    is the person's already-known profile URL, if any, seeded as evidence. Returns a
    :class:`PersonResearch` (possibly nearly empty when the evidence is thin) — the
    caller filters it through the provenance gate before proposing a commit.
    """
    evidence = await _gather_evidence(name, company, linkedin)
    if verbose:
        logger.info("person research for %s gathered %d evidence blocks", name, len(evidence))
    if not evidence:
        return PersonResearch(name=name, current_company=company)

    prompt = _PROMPT.format(
        name=name, company=company, evidence="\n\n".join(evidence)[:_EVIDENCE_CHARS]
    )
    resp = await generate_with_retry(
        genai.Client(),
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PersonResearch,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    if not isinstance(parsed, PersonResearch):
        return PersonResearch(name=name, current_company=company)
    parsed.name = parsed.name or name  # never lose the subject we were asked about
    return parsed
