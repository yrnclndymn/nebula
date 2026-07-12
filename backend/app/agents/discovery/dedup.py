"""Dedup web candidates against what the graph already knows (issue #75).

A candidate is "known" if its domain matches a stored company website, or its name
normalises to the same key as an existing company name OR alias. Name
normalisation reuses `entity_resolution.normalize_name` (so "Acme, LLC" ~ "Acme")
and domain canonicalisation reuses `cache.domain_of` — the same rules the rest of
the graph uses, so dedup stays consistent with how the graph itself merges.

`known_index` is the one graph read; `is_known` / `filter_new` are pure so the
matching logic tests without a DB.
"""

from neo4j import AsyncDriver

from app.graph.cache import domain_of
from app.graph.entity_resolution import normalize_name


async def known_index(driver: AsyncDriver) -> tuple[set[str], set[str]]:
    """Build the dedup index from the graph: (normalised name+alias keys, website
    domains) across every company node."""
    name_keys: set[str] = set()
    domains: set[str] = set()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company) "
            "RETURN c.name AS name, coalesce(c.aliases, []) AS aliases, c.website AS website"
        )
        async for record in result:
            for candidate in [record["name"], *(record["aliases"] or [])]:
                key = normalize_name(candidate or "")
                if key:
                    name_keys.add(key)
            if record["website"]:
                d = domain_of(record["website"])
                if d:
                    domains.add(d)
    return name_keys, domains


def is_known(candidate: dict, name_keys: set[str], domains: set[str]) -> bool:
    """Is this candidate already captured in the graph (by domain or name key)?"""
    website = candidate.get("website")
    if website:
        d = domain_of(website)
        if d and d in domains:
            return True
    key = normalize_name(candidate.get("name", ""))
    return bool(key) and key in name_keys


def filter_new(candidates: list[dict], name_keys: set[str], domains: set[str]) -> list[dict]:
    """Drop candidates already in the graph; keep the genuinely new ones. Also
    de-dupes NEW candidates against each other by name key (two search results for
    the same not-yet-captured company collapse to one)."""
    out: list[dict] = []
    seen_keys: set[str] = set()
    for c in candidates:
        if is_known(c, name_keys, domains):
            continue
        key = normalize_name(c.get("name", ""))
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        out.append(c)
    return out
