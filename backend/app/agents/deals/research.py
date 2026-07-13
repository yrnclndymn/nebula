"""Acquisition research: gather evidence, then one structured Gemini extraction.

Deliberately NOT an ADK reasoning loop — a bounded gather + a single
structured-output call keeps the commit deterministic (a fixed
:class:`AcquisitionResearch` schema) and the spend easy to budget, matching the
people (#40) and news (#35) capture jobs rather than the free-form company agent.

Evidence reuses the shared tools (``web_search``, ``fetch_page``): a few targeted
searches for the company's acquisition history (deals it made and deals where it
was acquired), plus best-effort page fetches of the top organic results. All of it
is UNTRUSTED — it only feeds a *proposal* a human reviews, and provenance is
re-checked deterministically downstream (see
:func:`app.agents.deals.build.build_acquisition_record`), so a fabricated citation —
especially an uncited amount — never survives to a write.

Every tool call charges the active per-run budget (``acquisition_research``), so
the gather stops cleanly at its caps.
"""

import asyncio
import logging

from google import genai
from google.genai import types

from app import budget
from app.agents.deals.models import AcquisitionResearch
from app.config import settings
from app.genai_retry import generate_with_retry
from app.tools.web import fetch_page, web_search

logger = logging.getLogger("nebula.ma.research")

_MAX_EVIDENCE_PAGES = 4  # top organic results we fetch full text for
_EVIDENCE_CHARS = 18000  # cap the prompt payload


async def _gather_evidence(name: str) -> list[str]:
    """Collect labelled evidence snippets for the extraction prompt.

    Best-effort and budget-charged: a failed fetch or a capped search simply
    contributes less evidence, never an error.
    """
    evidence: list[str] = []
    urls_to_fetch: list[str] = []

    for query in (
        f"{name} acquisition history acquired companies",
        f"{name} acquired by OR merger deal value",
    ):
        try:
            results = (await asyncio.to_thread(web_search, query)).get("results", [])
        except budget.BudgetExhausted:
            break
        for hit in results:
            url = hit.get("url") or ""
            evidence.append(f"[search] {hit.get('title', '')} — {url}\n{hit.get('snippet', '')}")
            if url.lower().startswith(("http://", "https://")) and len(urls_to_fetch) < (
                _MAX_EVIDENCE_PAGES
            ):
                urls_to_fetch.append(url)

    for url in urls_to_fetch:
        try:
            page = await fetch_page(url)
        except budget.BudgetExhausted:
            break
        if "error" in page:
            continue
        evidence.append(f"[page] {url}\n{page.get('text', '')[:4000]}")

    return evidence


_PROMPT = (
    "You are researching the acquisition (M&A) history of a specific company for a "
    "professional knowledge graph. Using ONLY the evidence below, extract every "
    "acquisition involving {name} — BOTH deals where {name} was the acquirer AND "
    "deals where {name} was the company acquired.\n\n"
    "For each deal return: the `acquirer` company name, the `target` (acquired) "
    "company name, the announced date (`announced_at`) and closed date "
    "(`closed_at`) if shown, the deal value (`amount`, e.g. '$1.2 billion') and its "
    "`currency` if shown, and a one-line strategic `thesis` (why the deal happened) "
    "if stated.\n\n"
    "CRITICAL — CITATIONS: set each deal's `source` to the exact URL from the "
    "evidence that reports the deal. Set `amount_source` to the exact URL that "
    "states the deal value — ONLY if the evidence actually gives a figure. Do NOT "
    "invent, estimate, or infer an amount: if no source states a value, leave "
    "`amount`, `currency`, and `amount_source` empty. Do NOT invent companies, "
    "deals, URLs, or citations — omit anything the evidence does not support.\n\n"
    "EVIDENCE:\n{evidence}"
)


async def research_acquisitions(name: str, *, verbose: bool = False) -> AcquisitionResearch:
    """Research one company's acquisition history and return raw, untrusted findings.

    Returns an :class:`AcquisitionResearch` (possibly empty when the evidence is
    thin) — the caller filters it through the provenance gate before proposing a
    commit.
    """
    evidence = await _gather_evidence(name)
    if verbose:
        logger.info("acquisition research for %s gathered %d evidence blocks", name, len(evidence))
    if not evidence:
        return AcquisitionResearch(company=name)

    prompt = _PROMPT.format(name=name, evidence="\n\n".join(evidence)[:_EVIDENCE_CHARS])
    resp = await generate_with_retry(
        genai.Client(),
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AcquisitionResearch,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    if not isinstance(parsed, AcquisitionResearch):
        return AcquisitionResearch(company=name)
    parsed.company = parsed.company or name
    return parsed
