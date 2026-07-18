"""Graph-backed crawl cache: reuse fetched pages and derived client lists so we
don't re-crawl a company's site for every new question.

Read-through with a TTL. Lives in the graph deliberately: on Cloud Run + Neo4j
Aura it needs no extra infra (SQLite doesn't survive a serverless, scale-to-zero
filesystem), it's shared across instances, and it matches local dev (same Neo4j).
A page cache is exact-key lookup — Neo4j's strength — not full-text search.

Model: (:Page {url, text, linksJson, imagesJson, socialJson, fetchedAt}),
       (:SiteClients {domain, clients, fetchedAt}).
"""

import json
from urllib.parse import urlparse

from neo4j import AsyncDriver

from app.config import settings
from app.graph.sanitize import deep_sanitize


def domain_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return netloc.removeprefix("www.")


# Read-side rationale (#130): legacy `:Page` entries written before the source
# fix (#131) can hold surrogate escapes inside linksJson/imagesJson (json.dumps
# escapes what the driver would reject; json.loads resurrects it on read), so
# reads scrub before the page can reach a Gemini prompt. The walk itself now
# lives in app.graph.sanitize for the write side and tools to share.
_deep_sanitize = deep_sanitize


async def get_cached_page(
    driver: AsyncDriver, url: str, ttl_days: int | None = None
) -> dict | None:
    """Return a cached page (same shape as a live fetch) if fresh, else None."""
    ttl = settings.cache_ttl_days if ttl_days is None else ttl_days
    async with driver.session() as session:
        result = await session.run(
            "MATCH (p:Page {url: $url}) "
            "WHERE p.fetchedAt >= datetime() - duration({days: $ttl}) "
            "RETURN p.text AS text, p.linksJson AS links, p.imagesJson AS images, "
            "       p.socialJson AS social",
            url=url,
            ttl=ttl,
        )
        record = await result.single()
    if record is None:
        return None
    # Sanitize on read: json.loads can resurrect lone surrogates escaped into
    # linksJson/imagesJson by pre-#131 writes; scrub them before this page reaches
    # a UTF-8 prompt encode (#130). No-op fast path keeps the clean case ~free.
    return _deep_sanitize(
        {
            "url": url,
            "text": record["text"] or "",
            "links": json.loads(record["links"] or "[]"),
            "images": json.loads(record["images"] or "[]"),
            "social": json.loads(record["social"] or "{}"),  # {} for pages cached pre-social
        }
    )


async def store_page(driver: AsyncDriver, page: dict) -> None:
    # Sanitize the whole page dict before writing: a lone surrogate anywhere in it
    # — a link href/text, an image alt, a social URL, or the raw `text`/`url` params
    # passed straight to the driver — makes the driver's UTF-8 encode raise and would
    # kill the research job (#146). The read side already scrubs (#130); guard the
    # write symmetrically so a poisoned crawl never reaches the encoder, and so no
    # surrogate is stashed into linksJson/imagesJson for a later read to resurrect.
    page = _deep_sanitize(page)
    async with driver.session() as session:
        await session.run(
            "MERGE (p:Page {url: $url}) "
            "SET p.text = $text, p.linksJson = $links, p.imagesJson = $images, "
            "    p.socialJson = $social, p.fetchedAt = datetime()",
            url=page["url"],
            text=page.get("text", ""),
            links=json.dumps(page.get("links", [])),
            images=json.dumps(page.get("images", [])),
            social=json.dumps(page.get("social", {})),
        )


async def get_cached_clients(
    driver: AsyncDriver, domain: str, ttl_days: int | None = None
) -> list[str] | None:
    ttl = settings.cache_ttl_days if ttl_days is None else ttl_days
    async with driver.session() as session:
        result = await session.run(
            "MATCH (sc:SiteClients {domain: $domain}) "
            "WHERE sc.fetchedAt >= datetime() - duration({days: $ttl}) "
            "RETURN sc.clients AS clients",
            domain=domain,
            ttl=ttl,
        )
        record = await result.single()
    # No read-time sanitize needed here (#130): `sc.clients` is a native Neo4j
    # string list, not JSON — a raw surrogate can't be written to it (the driver
    # rejects it) and nothing gets json.loads-resurrected on read, so the
    # linksJson/imagesJson poisoning mechanism can't reach this field.
    return record["clients"] if record else None


async def store_clients(driver: AsyncDriver, domain: str, clients: list[str]) -> None:
    # Same guard as store_page (#146 review): a client name mined from logo alt
    # text can carry a lone surrogate, and the driver rejects it on encode.
    clients = deep_sanitize(clients)
    async with driver.session() as session:
        await session.run(
            "MERGE (sc:SiteClients {domain: $domain}) "
            "SET sc.clients = $clients, sc.fetchedAt = datetime()",
            domain=domain,
            clients=clients,
        )


async def clear_domain(driver: AsyncDriver, domain: str) -> dict:
    """Drop cached pages + client list for a domain so the next crawl is fresh."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (p:Page) WHERE p.url CONTAINS $domain DETACH DELETE p RETURN count(p) AS pages",
            domain=domain,
        )
        pages = (await result.single())["pages"]
        await session.run(
            "MATCH (sc:SiteClients {domain: $domain}) DETACH DELETE sc", domain=domain
        )
    return {"domain": domain, "pages_cleared": pages}
