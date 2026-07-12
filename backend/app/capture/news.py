"""Third-party news search for tracked companies (#35).

Where #34 captures a company's OWN-site news/blog/events, this captures *external*
coverage — third-party outlets writing ABOUT a tracked company — and stores each
article as a Signal with the outlet as its Source. It reuses #34's plumbing:
``dates.normalise_date`` for publish dates, the same ``signals.upsert_signal``
write path (canonical-URL dedup, so a third-party story that also appeared as a
site signal collapses to one node), and the same durable-job shape.


SOURCE-CHOICE SPIKE (2026-07-12) — DDGS-news vs Gemini grounded search vs a news API
-----------------------------------------------------------------------------------
The story flags DDGS as "unreliable for news", so before building we compared the
three candidate sources with a handful of live calls on well-known generic names
(OpenAI / Microsoft / Anthropic — never a tracked-company name):

* **DDGS ``.news()`` (chosen).** Free, no API key, no quota. Returns structured
  ``{date (ISO 8601), title, body, url, source (outlet name), image}`` — exactly
  the fields a Signal needs (outlet for provenance, ISO date, snippet). In testing
  it was reliable across repeated calls. Its known weakness is intermittent
  empties / soft rate-limits, but that degrades *gracefully* here: an empty result
  captures nothing, identical to a dead feed in #34, and never errors the job. Some
  URLs are Bing/MSN aggregator redirects rather than the true outlet URL — an
  accepted cost; ``canonicalise_url`` still yields a stable dedup key and the
  outlet name is preserved as the Source. **Cost: $0.**

* **Gemini grounded search (``GoogleSearch`` tool).** Rejected. It draws on the
  *same scarce free-tier Gemini quota the whole app already fights over* (issue
  #65) — the spike call 429'd (RESOURCE_EXHAUSTED) on the first attempt while
  other jobs held the key. Worse for our data model: grounding citations are
  ``vertexaisearch.cloud.google.com/grounding-api-redirect/...`` links, NOT canonical
  article URLs, so canonical-URL dedup against site signals breaks. It also costs
  one LLM call per company. Grounding is great for prose answers, poor for a
  structured, dedup-keyed signal feed.

* **A dedicated news API (NewsAPI.org / GNews / Bing News).** Rejected for now.
  Cleanest structured data, but: NewsAPI's free tier is developer-only (100 req/day,
  24h-delayed, no commercial use), so production means a paid plan ($449/mo for
  NewsAPI Business; GNews ~$50/mo for a small tier); Bing News Search is retiring.
  All add a secret to manage. Not worth it while DDGS covers the need at $0. Left
  as a documented upgrade path if DDGS reliability degrades.

**Decision:** DDGS ``.news()`` for retrieval + a pure, deterministic relevance /
entity-match filter (below) as the real value-add, with an optional, fail-safe,
batched Gemini confirm on top (default on, but the job never depends on it).


RELEVANCE / ENTITY-MATCH FILTER (``entity_relevance``)
------------------------------------------------------
Several tracked names are ordinary words, so "mentions the name" is not "is the
subject". The filter is pure, deterministic, and **fails closed** — an item is
irrelevant unless it earns enough evidence. We deliberately do NOT hardcode a
dictionary of which names are common words (the repo is public — no tracked names
in code): instead every *single-token* name is treated as collision-prone and, on
a body-only mention, must be corroborated by a business-context cue; a headline
mention or a multi-word name always passes. The optional LLM confirm can only
*prune* the pure filter's shortlist (reject false positives), never rescue what it
rejected — so the deterministic floor is authoritative.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass

from ddgs import DDGS
from google import genai
from google.genai import types
from pydantic import BaseModel

from app import budget
from app.capture.dates import normalise_date
from app.config import settings
from app.genai_retry import generate_with_retry
from app.graph import cache, jobs
from app.graph.driver import get_driver
from app.graph.models import SignalRecord, canonicalise_url
from app.graph.signals import upsert_signal

logger = logging.getLogger("nebula.capture.news")

_MAX_RESULTS = 12
_MAX_SIGNALS = 20
_TITLE_MAX = 300

# Business-context cues: their presence near a body-only mention of a single-word
# name is what tips an ambiguous common-word match from "probably a collision" to
# "plausibly the company". Deliberately broad but company-shaped.
_CONTEXT_CUES = frozenset(
    {
        "ai",
        "startup",
        "startups",
        "company",
        "companies",
        "firm",
        "platform",
        "software",
        "app",
        "saas",
        "enterprise",
        "raises",
        "raised",
        "raise",
        "funding",
        "funded",
        "round",
        "series",
        "seed",
        "valuation",
        "valued",
        "ipo",
        "launch",
        "launches",
        "launched",
        "announces",
        "announced",
        "unveils",
        "unveiled",
        "acquires",
        "acquired",
        "acquisition",
        "merger",
        "partnership",
        "partners",
        "ceo",
        "cto",
        "coo",
        "founder",
        "founders",
        "cofounder",
        "co-founder",
        "investors",
        "investor",
        "venture",
        "backed",
        "revenue",
        "customers",
        "model",
        "models",
        "tool",
        "tools",
    }
)


@dataclass
class NewsHit:
    """One third-party news result. ``published_at`` is the raw date as returned
    (normalised on write); ``outlet`` is the publisher name from the source."""

    title: str
    url: str
    published_at: str | None = None
    summary: str | None = None
    outlet: str | None = None


@dataclass(frozen=True)
class Relevance:
    """Outcome of the entity-match filter: whether the company is the subject, the
    evidence score, and a short human reason (for logs / debugging)."""

    relevant: bool
    score: int
    reason: str


# --- pure: relevance / entity-match -----------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise(text: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to a single space, padded
    with a leading/trailing space so whole-phrase matching is word-bounded."""
    lowered = (text or "").lower()
    return " " + _NON_ALNUM.sub(" ", lowered).strip() + " "


def _brand_token(website: str | None) -> str | None:
    """The registrable brand label from a website (``acme.com`` -> ``acme``), a
    convenient extra alias. None when there's no usable host."""
    if not website:
        return None
    host = cache.domain_of(website)  # www-stripped, lowercased
    label = host.split(".")[0] if host else ""
    return label or None


def entity_relevance(
    name: str,
    title: str,
    summary: str | None = "",
    *,
    aliases: tuple[str, ...] = (),
    website: str | None = None,
    min_score: int = 2,
) -> Relevance:
    """Is ``name`` actually the *subject* of this item? Pure and fail-closed.

    Candidates are the name, any ``aliases``, and the website brand token; each is
    matched as a whole phrase (word-bounded, case/punctuation-insensitive) against
    the title and the summary. Scoring:

      +2  a candidate appears in the TITLE (headlines are a strong subject signal)
      +1  a candidate appears in the SUMMARY (body)
      +1  the matched candidate is multi-word (multi-word collisions are rare)
      +1  a business-context cue co-occurs with a mention

    ``relevant`` is ``score >= min_score`` (default 2). A single-word name that only
    appears in the body, with no cue, scores 1 and is dropped — the guard against
    common-word collisions. No mention at all scores 0.
    """
    candidates = [name, *aliases]
    brand = _brand_token(website)
    if brand:
        candidates.append(brand)

    norm_title = _normalise(title)
    norm_summary = _normalise(summary or "")

    def hit(norm_text: str) -> bool:
        return any(f" {_normalise(c).strip()} " in norm_text for c in candidates if c and c.strip())

    def multiword_hit(norm_text: str) -> bool:
        return any(
            len(_normalise(c).split()) >= 2 and f" {_normalise(c).strip()} " in norm_text
            for c in candidates
            if c and c.strip()
        )

    title_hit = hit(norm_title)
    summary_hit = hit(norm_summary)

    if not title_hit and not summary_hit:
        return Relevance(False, 0, "no mention of the company")

    score = 0
    reasons: list[str] = []
    if title_hit:
        score += 2
        reasons.append("title mention")
    if summary_hit:
        score += 1
        reasons.append("body mention")
    if multiword_hit(norm_title) or multiword_hit(norm_summary):
        score += 1
        reasons.append("multi-word name")

    tokens = set(norm_title.split()) | set(norm_summary.split())
    if tokens & _CONTEXT_CUES:
        score += 1
        reasons.append("business-context cue")

    relevant = score >= min_score
    return Relevance(relevant, score, ", ".join(reasons))


# --- retrieval (DDGS news) --------------------------------------------------


def _news_search_live(query: str, max_results: int) -> list[NewsHit]:
    """Blocking DDGS news query -> NewsHits (run off-loop). Never raises: any
    backend hiccup captures nothing, exactly like a dead feed in #34."""
    try:
        with DDGS() as ddgs:
            hits = ddgs.news(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 — a flaky search just captures nothing
        logger.info("news search failed for %r: %s", query, exc)
        return []
    return [
        NewsHit(
            title=(h.get("title") or "").strip(),
            url=(h.get("url") or "").strip(),
            published_at=h.get("date"),
            summary=h.get("body"),
            outlet=h.get("source"),
        )
        for h in (hits or [])
    ]


async def search_news(query: str, max_results: int = _MAX_RESULTS) -> list[NewsHit]:
    """Third-party news results for a query. Charges the active per-run search
    budget (no-op when unbudgeted), then runs the blocking query off the loop."""
    budget.charge_search()
    return await asyncio.to_thread(_news_search_live, query, max_results)


# --- optional LLM confirm (fail-safe; only ever PRUNES the pure shortlist) ---


class _Verdict(BaseModel):
    index: int
    is_subject: bool


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


async def llm_filter_subjects(name: str, hits: list[NewsHit]) -> list[NewsHit]:
    """Batched LLM pass that keeps only hits where ``name`` is genuinely the
    subject. One call for the whole shortlist. Untrusted content: the model only
    *classifies* items already admitted by the pure filter — it can drop a false
    positive but never adds anything and never influences any other write. On any
    error (incl. quota exhaustion) it fails safe: returns the input unchanged."""
    if not hits:
        return hits
    listing = "\n".join(
        f"{i}. TITLE: {h.title}\n   SUMMARY: {(h.summary or '')[:300]}" for i, h in enumerate(hits)
    )
    prompt = (
        f"You are checking news search results for the company {name!r}. Company "
        "names are sometimes ordinary words, so a result may merely mention the "
        "word without the company being its subject. For each numbered item, decide "
        f"whether {name!r} (the company/organisation) is genuinely a subject of the "
        "article. Return a verdict for every index.\n\n"
        f"ITEMS:\n{listing}"
    )
    try:
        resp = await generate_with_retry(
            genai.Client(),
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_Verdicts,
                temperature=0,
            ),
        )
        parsed = resp.parsed
    except Exception as exc:  # noqa: BLE001 — fail safe to the pure shortlist
        logger.info("news LLM confirm unavailable for %s (%s); keeping pure filter", name, exc)
        return hits
    if not isinstance(parsed, _Verdicts):
        return hits
    keep = {v.index for v in parsed.verdicts if v.is_subject}
    return [h for i, h in enumerate(hits) if i in keep]


# --- record conversion ------------------------------------------------------


def _outlet_source(hit: NewsHit) -> str | None:
    """The third-party outlet's identity for the Source node: the article host as
    a URL (``https://<host>``), so all coverage from one outlet shares a Source.
    None when the URL has no host."""
    host = cache.domain_of(hit.url)
    return f"https://{host}" if host else None


def _to_record(hit: NewsHit, name: str) -> SignalRecord | None:
    """A validated third-party-news SignalRecord, or None to skip (no title/URL)."""
    canonical = canonicalise_url(hit.url)
    title = (hit.title or "").strip()
    if not canonical or not title:
        return None
    published = normalise_date(hit.published_at) or hit.published_at
    return SignalRecord(
        url=hit.url,
        title=title[:_TITLE_MAX],
        kind="news",  # third-party coverage is always news
        summary=hit.summary or None,
        published_at=published,
        source=_outlet_source(hit),  # provenance: the outlet, not the company
    )


# --- gather -----------------------------------------------------------------


async def gather_company_news(
    name: str, website: str | None = None, aliases: tuple[str, ...] = ()
) -> list[SignalRecord]:
    """Find recent third-party coverage where ``name`` is the subject.

    Retrieve via DDGS news, drop collisions with the pure entity-match filter
    (fail-closed), optionally prune false positives with the LLM confirm, then
    convert survivors to deduped SignalRecords. Stops cleanly on a budget cap,
    keeping whatever was gathered."""
    records: list[SignalRecord] = []
    seen: set[str] = set()
    try:
        hits = await search_news(f'"{name}"')
        relevant = [
            h
            for h in hits
            if entity_relevance(name, h.title, h.summary, aliases=aliases, website=website).relevant
        ]
        if settings.news_llm_confirm and relevant:
            relevant = await llm_filter_subjects(name, relevant)
        for hit in relevant:
            record = _to_record(hit, name)
            if record is None:
                continue
            canonical = record.canonical_url()
            if canonical in seen:
                continue
            seen.add(canonical)
            records.append(record)
            if len(records) >= _MAX_SIGNALS:
                break
    except budget.BudgetExhausted as exc:
        logger.info("news capture for %s hit budget cap (%s); keeping partial", name, exc.limit)
    return records


# --- durable job ------------------------------------------------------------


async def _company_website(driver, name: str) -> tuple[bool, str | None]:
    """(exists, website) for a company by name. ``exists`` distinguishes an unknown
    company from a known one with no website (news search still works without it)."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company {name: $name}) RETURN c.website AS website", name=name
        )
        record = await result.single()
    if record is None:
        return False, None
    return True, record["website"]


async def _existing_signal_urls(driver, urls: list[str]) -> set[str]:
    """Which of ``urls`` (canonical) already exist as Signals — so a re-run reports
    how many are genuinely new (upsert dedupes either way, incl. against #34's
    site-sourced signals sharing the same canonical URL)."""
    if not urls:
        return set()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (s:Signal) WHERE s.url IN $urls RETURN s.url AS url", urls=urls
        )
        return {record["url"] async for record in result}


async def start_news_capture(name: str) -> dict:
    """Kick off a background search for third-party coverage of a company. Returns
    immediately with a job id to poll; nothing runs synchronously. 404-shaped
    ``error`` when the company is unknown."""
    driver = get_driver()
    exists, website = await _company_website(driver, name)
    if not exists:
        return {"error": f"no company named {name!r} to search news for"}
    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "news_capture",
        {
            "job_id": job_id,
            "status": "pending",
            "name": name,
            "website": website,
            "captured": 0,
            "new": 0,
        },
    )
    await jobs.enqueue(job_id)
    return {"job_id": job_id, "status": "searching news in the background"}


async def run_news_capture_job(job_id: str) -> None:
    """Job runner: gather third-party coverage and write each as a Signal (dedup
    via upsert on the canonical URL, incl. against site-sourced signals)."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    run_budget = budget.budget_for("news_capture", job.get("budget"))
    try:
        with budget.use_budget(run_budget):
            records = await gather_company_news(job["name"], job.get("website"))
    except Exception as exc:  # noqa: BLE001 — surface search/capture failures on the job
        await jobs.update_job(
            job_id,
            {**job, "error": "news capture failed", "error_detail": str(exc)},
            status="error",
        )
        return

    canonical_urls = [record.canonical_url() for record in records]
    existing = await _existing_signal_urls(driver, canonical_urls)
    new_count = sum(1 for url in canonical_urls if url not in existing)
    for record in records:
        await upsert_signal(driver, record, companies=[job["name"]])

    outcome = f"found {len(records)} third-party news items ({new_count} new) about {job['name']}"
    await jobs.update_job(
        job_id,
        {**job, "captured": len(records), "new": new_count, "outcome": outcome},
        status="done",
    )


async def get_news_capture(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)
