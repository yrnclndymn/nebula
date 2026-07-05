"""Cache round-trip (needs Neo4j): store → get → clear."""

import asyncio

import pytest

from app.graph import cache
from app.graph.driver import check_connectivity, close_driver, get_driver

URL = "https://__pytest_cache__.example.com/page"
DOMAIN = "__pytest_cache__.example.com"


def test_cache_page_and_clients_roundtrip():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        await cache.store_page(
            driver,
            {"url": URL, "text": "hello", "links": [{"url": "x", "text": "y"}], "images": []},
        )
        await cache.store_clients(driver, DOMAIN, ["Acme", "Globex"])

        page = await cache.get_cached_page(driver, URL)
        clients = await cache.get_cached_clients(driver, DOMAIN)
        # TTL: nothing is younger than -1 days, so this must miss.
        stale = await cache.get_cached_page(driver, URL, ttl_days=-1)

        cleared = await cache.clear_domain(driver, DOMAIN)
        gone = await cache.get_cached_page(driver, URL)
        await close_driver()
        return page, clients, stale, cleared, gone

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    page, clients, stale, cleared, gone = out
    assert page is not None and page["text"] == "hello" and page["links"][0]["url"] == "x"
    assert clients == ["Acme", "Globex"]
    assert stale is None
    assert cleared["pages_cleared"] >= 1
    assert gone is None  # cleared
