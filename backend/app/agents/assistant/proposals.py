"""Propose → review → commit for chat-triggered enrichment.

The assistant can *propose* an enrichment but cannot *commit* it — committing is a
user action, keeping a human in the loop on every write.

Jobs are durable (stored in the graph, run via `app.graph.jobs`) so they survive
Cloud Run scale-to-zero: `propose_enrichment` creates a pending job and enqueues
it; the client polls `get_proposal` until ready; the user commits.
"""

import asyncio
import uuid
from contextvars import ContextVar
from urllib.parse import urlparse

from app.agents.assistant.proposal_diff import (
    citation_matches_focus,
    compute_diff,
    field_label,
    resolve_focus,
)
from app.agents.assistant.reconcile import reconcile_people
from app.agents.enrichment.enrich import enrich
from app.genai_retry import QuotaExhausted, run_with_quota_retry
from app.graph import jobs, queries
from app.graph.driver import get_driver
from app.graph.models import Citation, CompanyRecord
from app.graph.repository import upsert_company
from app.tools.graph_tools import proposal_sink
from app.tools.web import web_search

# Proposals started during the current chat turn (read by the /chat endpoint).
turn_proposals: ContextVar[list | None] = ContextVar("nebula_turn_proposals", default=None)


async def propose_enrichment(
    name: str,
    website: str,
    topic: str = "AI-native engineering",
    focus: str = "",
    enqueue_delay: float = 0.0,
) -> dict:
    """Start researching a company in the BACKGROUND to prepare a proposed graph
    update for the user to review. Returns immediately and does NOT save anything.
    Call it when the user asks to research, add, enrich, or update a company. After
    calling, tell the user you've STARTED researching and a proposal will appear
    shortly to review and commit — don't wait for it, and never claim you saved
    anything. If you don't have the website, ask first.

    focus: the SINGLE field the user asked about, if they named one — e.g.
    "headcount", "hq", "funding", "linkedin", "year founded", "revenue", "about".
    Leave it "" for a general "research/update <Company>" with no specific field.
    A focused proposal leads the review card with that field and commits just it.

    enqueue_delay: seconds to defer the job start (issue #65) so a batch of
    proposals staggers instead of firing all at once and exhausting Gemini quota.
    Chat proposals leave it 0 (start now)."""
    proposal_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        proposal_id,
        "proposal",
        {
            "proposal_id": proposal_id,
            "status": "pending",
            "name": name,
            "website": website,
            "topic": topic,
            "focus": focus,
        },
    )
    await jobs.enqueue(proposal_id, delay=enqueue_delay)

    collected = turn_proposals.get()
    if collected is not None:
        collected.append({"proposal_id": proposal_id, "status": "pending", "name": name})
    return {"proposal_id": proposal_id, "name": name, "status": "researching in the background"}


# Hosts that are never a company's own official site — social networks,
# directories, encyclopaedias, app stores, review/press sites. When discovering a
# website from search results we skip these and take the first result that isn't
# one of them.
_NON_OFFICIAL_HOSTS = (
    "linkedin.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "crunchbase.com",
    "bloomberg.com",
    "pitchbook.com",
    "github.com",
    "medium.com",
    "glassdoor.com",
    "indeed.com",
    "g2.com",
    "trustpilot.com",
    "reddit.com",
    "apps.apple.com",
    "play.google.com",
    "owler.com",
    "zoominfo.com",
    "craft.co",
    "producthunt.com",
    "similarweb.com",
    "clutch.co",
)


def _official_host(url: str) -> str | None:
    """The bare host of a search-result URL if it plausibly is a company's own
    site, else None (a social/directory/press host we should skip)."""
    if not url:
        return None
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    # Defensive: netloc may carry userinfo (user@host) or a port (host:1234) —
    # neither belongs in a discovered website / blocklist comparison.
    host = host.split("@")[-1].split(":")[0]
    host = host.removeprefix("www.")
    if not host:
        return None
    if any(host == bad or host.endswith("." + bad) for bad in _NON_OFFICIAL_HOSTS):
        return None
    return host


async def discover_website(name: str) -> str | None:
    """Find a company's official website via web search — for backlog stubs that
    arrive with no website. Returns a bare domain (e.g. "acme.com") or None.

    Deliberately simple: search for the company's official site and take the first
    organic result whose host isn't a social network / directory / press site. The
    pick is untrusted input — it only seeds the enrichment crawl, and the user sees
    and reviews the discovered site (and everything derived from it) before any
    commit. No auto-write, so a wrong guess costs a discard, not bad data."""
    results = (await asyncio.to_thread(web_search, f"{name} official website")).get("results", [])
    for hit in results:
        host = _official_host(hit.get("url", ""))
        if host:
            return host
    return None


async def run_proposal_job(proposal_id: str) -> None:
    """Job runner: research the company (capture, don't write) and fill in the job."""
    job = await jobs.get_job(proposal_id)
    if job is None:
        return
    sink: list = []
    token = proposal_sink.set(sink)
    try:
        # Backlog stubs are enqueued with no website. Discover the official site
        # first so the enrichment agent has a domain to crawl; record it on the
        # job so the user sees which site the research is based on. Inside the
        # try so a discovery MISS *and* a discovery FAILURE (e.g. a search
        # network error) both error the proposal instead of escaping the runner
        # and leaving the job stuck pending.
        if not (job.get("website") or "").strip():
            discovered = await discover_website(job["name"])
            if not discovered:
                await jobs.update_job(
                    proposal_id,
                    {**job, "error": f"could not find an official website for {job['name']}"},
                    status="error",
                )
                return
            job = {**job, "website": discovered, "discovered_website": discovered}
            await jobs.update_job(proposal_id, job)  # persist before the slow research
        # A 429 RESOURCE_EXHAUSTED must not kill the job: re-run the whole enrich
        # on quota errors, honouring the server's RetryInfo delay, bounded. The
        # ADK Runner hides the per-request call, so retry is at run granularity —
        # the page cache makes the re-crawl cheap. After the bound it raises
        # QuotaExhausted, surfaced below as a friendly one-liner.
        result = await run_with_quota_retry(
            lambda: enrich(job["name"], job["website"], job["topic"], verbose=False)
        )
        if not sink:
            await jobs.update_job(
                proposal_id, {**job, "error": "no record produced"}, status="error"
            )
            return
        record = sink[-1]
        existing = await queries.get_company(get_driver(), record["name"])
        # Reconcile proposed leaders against existing people so name-variants
        # (Andy/Andrew) merge instead of creating duplicate :Person nodes; the
        # record we store writes the reconciled (deduped) leadership.
        leadership = reconcile_people(
            existing["leadership"] if existing else [], record.get("leadership", [])
        )
        record["leadership"] = leadership["reconciled"]
        focus_key = resolve_focus(job.get("focus"))
        diff = compute_diff(existing, record, leadership)
        await jobs.update_job(
            proposal_id,
            {
                **job,
                "name": record["name"],
                "exists": existing is not None,
                "summary": result.summary,
                "record": record,
                "focus_key": focus_key,
                "focus_label": field_label(focus_key) if focus_key else "",
                "diff": diff,
                # Human-readable completion line for the activity page (#49).
                "outcome": f"proposal ready for {record['name']}",
            },
            status="ready",
        )
    except QuotaExhausted as exc:
        # Human-readable one-liner on the Job (not a raw 429 JSON dump); the raw
        # error is kept under a separate key for debugging.
        await jobs.update_job(
            proposal_id,
            {**job, "error": exc.message, "error_detail": exc.detail},
            status="error",
        )
    except Exception as exc:  # noqa: BLE001 — surface research failures to the client
        await jobs.update_job(proposal_id, {**job, "error": str(exc)}, status="error")
    finally:
        proposal_sink.reset(token)


async def get_proposal(proposal_id: str) -> dict | None:
    return await jobs.get_job(proposal_id)


def _focus_record(job: dict) -> CompanyRecord | None:
    """A minimal record that writes ONLY the focused field (+ its citations), so a
    "just update the headcount" request never rewrites HQ, about, etc."""
    focus_key = job.get("focus_key")
    stored = job["record"]
    value = stored.get(focus_key) if focus_key else None
    if focus_key is None or value in (None, "", 0):
        return None
    citations = [
        Citation(**c)
        for c in stored.get("citations", [])
        if citation_matches_focus(c.get("field"), focus_key)
    ]
    return CompanyRecord(
        name=stored["name"], origin="agent", citations=citations, **{focus_key: value}
    )


async def commit_proposal(proposal_id: str, scope: str = "all") -> dict:
    """Write a reviewed, ready proposal to the graph. Called by the UI, not the agent.

    scope="focus" writes only the field the user asked about (used by focused
    proposals); scope="all" writes the full reconciled record. Both are idempotent,
    so committing focus then "all" is safe."""
    job = await jobs.get_job(proposal_id)
    if job is None or job.get("status") != "ready":
        return {"error": "proposal not found or not ready"}

    if scope == "focus":
        record = _focus_record(job)
        if record is None:
            return {"error": "no value found for the focused field"}
    else:
        record = CompanyRecord.model_validate(job["record"])

    await upsert_company(get_driver(), record)
    committed = set(job.get("committed_scopes", []))
    committed.add(scope)
    await jobs.update_job(
        proposal_id, {**job, "committed": True, "committed_scopes": sorted(committed)}
    )
    return {"committed": record.name, "scope": scope}
