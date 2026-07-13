"""Durable, API-triggered company-site signal-capture job (#34).

Flow for one company:
  1. fetch the homepage HTML and autodiscover RSS/Atom feeds (``feeds``);
  2. parse each feed into items (title / date / summary);
  3. FALLBACK — if no feed yields items, detect on-site news/blog/events index
     pages (``sections``) and LLM-extract their item lists (crawled through the
     cached ``fetch_page`` so cache retention covers it);
  4. normalise item dates (``dates``) and write each as a Signal through the
     existing ``signals.upsert_signal`` — which dedupes on the canonical URL, so a
     re-run only adds items not seen before, and records provenance.

The job never mutates company facts and never lets crawled content steer a write
beyond creating its own Signal node — it is a content stream, not a graph-fact
write path. Spend is bounded by a per-run budget (``budget.budget_for``); the
shared tool helpers charge it, and direct feed fetches charge it here.
"""

import asyncio
import logging
import uuid

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel

from app import budget
from app.capture.dates import normalise_date
from app.capture.feeds import FeedItem, discover_feeds, parse_feed
from app.capture.people import link_signal_people
from app.capture.sections import classify_section, find_section_pages
from app.config import settings
from app.genai_retry import generate_with_retry
from app.graph import cache, jobs
from app.graph.driver import get_driver
from app.graph.models import SignalRecord, canonicalise_url
from app.graph.signals import upsert_signal
from app.tools.web import _HEADERS, fetch_page

logger = logging.getLogger("nebula.capture")

_TIMEOUT = 15
_MAX_FEEDS = 5
_MAX_ITEMS_PER_FEED = 25
_MAX_FALLBACK_PAGES = 4
_MAX_ITEMS_PER_PAGE = 20
_TITLE_MAX = 300


# --- direct fetch (feed XML + homepage head, which fetch_page can't expose) --


def _get_raw_live(url: str) -> bytes | None:
    """Blocking GET of the raw response BYTES (feed XML / page HTML), or None on
    error. Bytes — not ``resp.text`` — so the feed XML prolog / HTML meta-charset is
    honoured by the parser instead of requests' ISO-8859-1 fallback mangling UTF-8
    into mojibake (#89); ``discover_feeds``/``parse_feed`` both sniff the encoding
    from the bytes they're given."""
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001 — a dead feed just captures nothing
        return None
    return resp.content


async def _fetch_raw(url: str) -> bytes | None:
    """Raw bytes for a feed or a page's ``<head>`` — needed because ``fetch_page``
    strips ``<link>`` tags and parses HTML (it can't return feed XML). Charges the
    per-run page budget like any other network fetch."""
    budget.charge_page()
    return await asyncio.to_thread(_get_raw_live, url)


# --- LLM extraction for the index-page fallback -----------------------------


class _ExtractedItem(BaseModel):
    title: str
    url: str
    date: str | None = None
    summary: str | None = None


class _ExtractedItems(BaseModel):
    items: list[_ExtractedItem]


async def _extract_index_items(page_url: str, text: str, links: list[dict]) -> list[FeedItem]:
    """LLM-extract the list of posts/events from an index page's text + links.

    Crawled text is untrusted: the model only *extracts a list to display*, it
    never influences a company-fact write. Returns ``FeedItem``s (URLs absolutised
    against the page).
    """
    if not text.strip():
        return []
    link_lines = "\n".join(
        f"- {link.get('text', '').strip()} -> {link['url']}"
        for link in links[:40]
        if link.get("url")
    )
    prompt = (
        f"The text and links below are from {page_url}, a company's news / blog / "
        "events index page. List the individual items shown on it (articles, posts, "
        "press releases, or events) — for each: its title, its URL (pick the matching "
        "link from the list; make it absolute), the date exactly as shown (or empty), "
        "and a one-line summary if present. Only items actually listed on this page; "
        "ignore navigation, footer, and unrelated links. Return an empty list if none.\n\n"
        f"LINKS:\n{link_lines}\n\nTEXT:\n{text[:12000]}"
    )
    resp = await generate_with_retry(
        genai.Client(),
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_ExtractedItems,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    if not isinstance(parsed, _ExtractedItems):
        return []
    return [
        FeedItem(title=it.title, url=it.url, published_at=it.date, summary=it.summary)
        for it in parsed.items[:_MAX_ITEMS_PER_PAGE]
    ]


# --- capture core -----------------------------------------------------------


def _start_url(website: str) -> str:
    return website if website.startswith("http") else "https://" + website


def _build_record(item: FeedItem, kind: str, source: str, home_domain: str) -> SignalRecord | None:
    """A validated SignalRecord for a same-site item, or None to skip it."""
    canonical = canonicalise_url(item.url)
    title = (item.title or "").strip()
    if not canonical or not title:
        return None
    # Only the company's OWN site — never let a feed/page inject arbitrary external
    # URLs as this company's signals (untrusted-content guardrail). Subdomains of
    # the company's domain count as its own site (blogs often live on one).
    item_domain = cache.domain_of(canonical)
    if item_domain != home_domain and not item_domain.endswith("." + home_domain):
        return None
    if kind not in ("news", "blog", "event"):
        kind = "news"
    # Emit ISO when we can parse the date; else keep the raw string (upsert stores
    # it as publishedAtRaw, so ordering degrades gracefully and nothing is lost).
    published = normalise_date(item.published_at) or item.published_at
    return SignalRecord(
        url=item.url,
        title=title[:_TITLE_MAX],
        kind=kind,
        summary=item.summary or None,
        published_at=published,
        source=source,  # provenance: the feed / index page the item came from
    )


async def capture_company(name: str, website: str) -> list[SignalRecord]:
    """Gather Signal records from a company's own site (feeds first, index-page
    fallback). Stops cleanly on a budget cap, keeping whatever was gathered."""
    start = _start_url(website)
    home_domain = cache.domain_of(start)
    records: list[SignalRecord] = []
    seen: set[str] = set()

    def collect(items: list[FeedItem], kind: str, source: str) -> None:
        for item in items:
            record = _build_record(item, kind, source, home_domain)
            if record is None:
                continue
            canonical = record.canonical_url()
            if canonical in seen:
                continue
            seen.add(canonical)
            records.append(record)

    try:
        # 1) Feeds — autodiscover from the homepage <head>, then parse each.
        home_html = await _fetch_raw(start)
        feed_urls = discover_feeds(home_html or b"", start)[:_MAX_FEEDS]
        for feed_url in feed_urls:
            xml = await _fetch_raw(feed_url)
            if xml is None:
                continue
            kind = classify_section(feed_url) or "news"
            collect(parse_feed(xml, feed_url)[:_MAX_ITEMS_PER_FEED], kind, feed_url)

        # 2) Fallback — only when feeds produced nothing: crawl on-site index pages
        # (through the cached fetch_page) and LLM-extract their item lists.
        if not records:
            home = await fetch_page(start)
            if "error" not in home:
                sections = find_section_pages(home.get("links", []))
                pages = [(url, kind) for kind, urls in sections.items() for url in urls]
                for page_url, kind in pages[:_MAX_FALLBACK_PAGES]:
                    page = await fetch_page(page_url)
                    if "error" in page:
                        continue
                    items = await _extract_index_items(
                        page_url, page.get("text", ""), page.get("links", [])
                    )
                    collect(items, kind, page_url)
    except budget.BudgetExhausted as exc:
        logger.info("signal capture for %s hit budget cap (%s); keeping partial", name, exc.limit)

    return records


# --- durable job ------------------------------------------------------------


async def _company_website(driver, name: str) -> str | None:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company {name: $name}) RETURN c.website AS website", name=name
        )
        record = await result.single()
    return record["website"] if record else None


async def _existing_signal_urls(driver, urls: list[str]) -> set[str]:
    """Which of ``urls`` (canonical) already exist as Signals — so a re-run can
    report how many items are genuinely new (upsert dedupes either way)."""
    if not urls:
        return set()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (s:Signal) WHERE s.url IN $urls RETURN s.url AS url", urls=urls
        )
        return {record["url"] async for record in result}


async def start_signal_capture(name: str) -> dict:
    """Kick off a background capture of a company's own-site news/blog/events.
    Returns immediately with a job id to poll; nothing runs synchronously."""
    driver = get_driver()
    website = await _company_website(driver, name)
    if not website:
        return {"error": f"no company named {name!r} with a website to capture from"}
    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "signal_capture",
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
    return {"job_id": job_id, "status": "capturing in the background"}


async def run_signal_capture_job(job_id: str) -> None:
    """Job runner: capture items and write them as Signals (dedup via upsert)."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    run_budget = budget.budget_for("signal_capture", job.get("budget"))
    try:
        with budget.use_budget(run_budget):
            records = await capture_company(job["name"], job["website"])
    except Exception as exc:  # noqa: BLE001 — surface capture failures on the job
        await jobs.update_job(
            job_id, {**job, "error": "capture failed", "error_detail": str(exc)}, status="error"
        )
        return

    canonical_urls = [record.canonical_url() for record in records]
    existing = await _existing_signal_urls(driver, canonical_urls)
    new_count = sum(1 for url in canonical_urls if url not in existing)
    people_linked = people_flagged = 0
    for record in records:
        await upsert_signal(driver, record, companies=[job["name"]])
        # Link any authors/quoted people to the signal (#41). Best-effort: a
        # linking failure must never fail the capture job or lose the signals.
        try:
            counts = await link_signal_people(driver, record, company=job["name"])
            people_linked += counts["linked"]
            people_flagged += counts["flagged"]
        except Exception:  # noqa: BLE001 — people linking is auxiliary to capture
            logger.exception("people linking failed for %s", record.canonical_url())

    outcome = f"captured {len(records)} items ({new_count} new) from {job['name']}'s site"
    await jobs.update_job(
        job_id,
        {
            **job,
            "captured": len(records),
            "new": new_count,
            "people_linked": people_linked,
            "people_flagged": people_flagged,
            "outcome": outcome,
        },
        status="done",
    )


async def get_signal_capture(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)
