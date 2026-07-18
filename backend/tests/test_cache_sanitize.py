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


# --- #146: the WRITE side must scrub surrogates too (symmetry with the read) -----


class _FakeSession:
    """Captures the parameters store_page hands to session.run — no DB needed."""

    def __init__(self, captured: dict):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, _query, **params):
        self._captured.update(params)


class _FakeDriver:
    def __init__(self):
        self.captured: dict = {}

    def session(self):
        return _FakeSession(self.captured)


def test_store_page_sanitizes_write_params():
    """A page carrying a lone surrogate in its text, a link href/text, an image alt
    and a social URL must be scrubbed before it reaches the driver: `text`/`url` are
    passed raw (a surrogate there crashes the driver's UTF-8 encode — the #146 crash),
    and the JSON payloads must not stash a surrogate a later read resurrects."""
    driver = _FakeDriver()
    poisoned = {
        "url": f"https://x.example/{_SURROGATE}",
        "text": f"clean lead {_SURROGATE} tail",
        "links": [{"url": f"https://x.example/a{_SURROGATE}", "text": f"About {_SURROGATE} us"}],
        "images": [{"src": "https://x.example/logo.png", "alt": f"logo {_SURROGATE}"}],
        "social": {"linkedin": f"https://linkedin.example/{_SURROGATE}"},
    }
    asyncio.run(cache.store_page(driver, poisoned))

    # Raw string params must be UTF-8-encodable — this is exactly the encode the
    # real driver runs, and where the prod job died.
    driver.captured["text"].encode("utf-8")
    driver.captured["url"].encode("utf-8")
    assert _SURROGATE not in driver.captured["text"]
    assert "clean lead" in driver.captured["text"] and "tail" in driver.captured["text"]

    # JSON payloads: parse them back and confirm no surrogate survived the write.
    import json

    for key in ("links", "images", "social"):
        _assert_utf8_encodable(json.loads(driver.captured[key]))
    assert _SURROGATE not in json.loads(driver.captured["links"])[0]["url"]


def test_deep_sanitize_walks_all_shapes():
    """Strings anywhere in a JSON-like value are scrubbed; non-string leaves
    (numbers, bools, None) pass through untouched."""
    from app.graph.sanitize import deep_sanitize

    dirty = {
        "text": f"a{_SURROGATE}b",
        "links": [{"text": f"x{_SURROGATE}", "depth": 2}],
        "count": 3,
        "flag": True,
        "none": None,
    }
    clean = deep_sanitize(dirty)
    _assert_utf8_encodable(clean)
    assert clean["count"] == 3 and clean["flag"] is True and clean["none"] is None
    assert _SURROGATE not in clean["text"] and _SURROGATE not in clean["links"][0]["text"]


def test_store_clients_sanitizes_names():
    """A client name mined from logo alt text can carry a lone surrogate; the
    write must scrub it before the driver's UTF-8 encode (PR #159 review r2 —
    same bug class as store_page, sibling path)."""
    driver = _FakeDriver()
    asyncio.run(cache.store_clients(driver, "acme.example", ["Globex", f"Ac{_SURROGATE}me"]))
    _assert_utf8_encodable(driver.captured["clients"])
    assert all(_SURROGATE not in c for c in driver.captured["clients"])


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
