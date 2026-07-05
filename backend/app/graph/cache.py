"""Graph-backed crawl cache: reuse fetched pages and derived client lists so we
don't re-crawl a company's site for every new question.

Read-through with a TTL. Lives in the graph deliberately: on Cloud Run + Neo4j
Aura it needs no extra infra (SQLite doesn't survive a serverless, scale-to-zero
filesystem), it's shared across instances, and it matches local dev (same Neo4j).
A page cache is exact-key lookup — Neo4j's strength — not full-text search.

Model: (:Page {url, text, linksJson, imagesJson, fetchedAt}),
       (:SiteClients {domain, clients, fetchedAt}).
"""

import json
from urllib.parse import urlparse

from neo4j import AsyncDriver

from app.config import settings


def domain_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return netloc.removeprefix("www.")


async def get_cached_page(
    driver: AsyncDriver, url: str, ttl_days: int | None = None
) -> dict | None:
    """Return a cached page (same shape as a live fetch) if fresh, else None."""
    ttl = settings.cache_ttl_days if ttl_days is None else ttl_days
    async with driver.session() as session:
        result = await session.run(
            "MATCH (p:Page {url: $url}) "
            "WHERE p.fetchedAt >= datetime() - duration({days: $ttl}) "
            "RETURN p.text AS text, p.linksJson AS links, p.imagesJson AS images",
            url=url,
            ttl=ttl,
        )
        record = await result.single()
    if record is None:
        return None
    return {
        "url": url,
        "text": record["text"] or "",
        "links": json.loads(record["links"] or "[]"),
        "images": json.loads(record["images"] or "[]"),
    }


async def store_page(driver: AsyncDriver, page: dict) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (p:Page {url: $url}) "
            "SET p.text = $text, p.linksJson = $links, p.imagesJson = $images, "
            "    p.fetchedAt = datetime()",
            url=page["url"],
            text=page.get("text", ""),
            links=json.dumps(page.get("links", [])),
            images=json.dumps(page.get("images", [])),
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
    return record["clients"] if record else None


async def store_clients(driver: AsyncDriver, domain: str, clients: list[str]) -> None:
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
