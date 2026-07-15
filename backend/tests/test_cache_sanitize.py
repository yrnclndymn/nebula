"""Cache-read surrogate sanitization (#130).

Legacy `:Page` entries written before the source fix (#131) can carry lone
UTF-16 surrogates JSON-escaped inside ``linksJson`` / ``imagesJson``: a poisoned
page whose first surrogate sat beyond the 5000-char ``text`` cap stored fine
(Neo4j rejects a *raw* surrogate, but ``json.dumps`` escapes it to ``\\uXXXX``),
and ``json.loads`` resurrects the surrogate on read. Read the graph again and the
resurrected surrogate crashes the first downstream UTF-8 encode (the Gemini
prompt serializer). ``get_cached_page`` sanitizes on read to cover those entries
until the TTL flushes them.

The pure transform (`_deep_sanitize`) is unit-tested without a DB; a round-trip
test exercises the real store→read path and skips gracefully without Neo4j.
"""

import asyncio

import pytest

from app.graph import cache

# The lone surrogate seen in production (an HTML numeric ref like &#xDB11;).
_SURROGATE = "\udb11"

URL = "https://__pytest_cache_sanitize__.example.com/page"


def _all_strings(value):
    """Yield every string leaf in a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _all_strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _all_strings(v)


def _assert_utf8_encodable(value):
    for s in _all_strings(value):
        s.encode("utf-8")  # raises UnicodeEncodeError on a lone surrogate


def test_deep_sanitize_scrubs_every_string_field():
    """A resurrected cached-page dict with surrogates in link/image text and a
    social value comes out fully UTF-8-encodable, structure preserved."""
    poisoned = {
        "url": URL,
        "text": f"clean lead text {_SURROGATE} tail",
        "links": [{"url": "https://x.example/a", "text": f"About {_SURROGATE} us"}],
        "images": [{"src": "https://x.example/logo.png", "alt": f"logo {_SURROGATE}"}],
        "social": {"linkedin": f"https://linkedin.example/{_SURROGATE}"},
    }
    out = cache._deep_sanitize(poisoned)

    _assert_utf8_encodable(out)  # the guarantee callers rely on
    # Shape and clean substrings survive; only the surrogate is replaced.
    assert out["links"][0]["url"] == "https://x.example/a"
    assert _SURROGATE not in out["links"][0]["text"]
    assert "About" in out["links"][0]["text"] and "us" in out["links"][0]["text"]
    assert _SURROGATE not in out["images"][0]["alt"]
    assert _SURROGATE not in out["social"]["linkedin"]
    assert _SURROGATE not in out["text"]


def test_deep_sanitize_is_noop_on_clean_page():
    """Clean text is returned unchanged (same object) at every leaf — the fast
    path that keeps read-time sanitization ~free for the common case."""
    clean_text = "Acme partners with Globex"
    page = {"url": URL, "text": clean_text, "links": [], "images": [], "social": {}}
    out = cache._deep_sanitize(page)
    assert out["text"] is clean_text


def test_cached_page_read_sanitizes_legacy_poison_roundtrip():
    """End-to-end reproduction: store a page whose link text holds a surrogate
    (json.dumps escapes it, so Neo4j accepts the write), then read it back and
    confirm the resurrected surrogate is scrubbed. Skips without Neo4j."""
    from app.graph.driver import check_connectivity, close_driver, get_driver

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        await cache.store_page(
            driver,
            {
                "url": URL,
                "text": "clean cached text",
                "links": [{"url": "https://x.example/a", "text": f"About {_SURROGATE} us"}],
                "images": [{"src": "https://x.example/l.png", "alt": f"logo {_SURROGATE}"}],
                "social": {},
            },
        )
        page = await cache.get_cached_page(driver, URL)
        await cache.clear_domain(driver, cache.domain_of(URL))
        await close_driver()
        return page

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    assert out is not None
    _assert_utf8_encodable(out)
    assert _SURROGATE not in out["links"][0]["text"]
    assert out["links"][0]["url"] == "https://x.example/a"
