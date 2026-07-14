"""UTF-8-safe response decoding (#89): captured signal text was mojibake because
`requests` decodes a charset-less ``text/*`` body as ISO-8859-1, mangling UTF-8.

These are pure tests — no network, no DB. Bodies carry curly quotes / ellipsis /
accented European characters served WITHOUT a charset header (the bug path), plus
the control case of a genuinely ISO-8859-1 body WITH a declared charset (must stay
correct). Fixtures are fictional (Acme/Globex).
"""

import requests

from app.capture.feeds import discover_feeds, parse_feed
from app.tools.encoding import declared_charset, response_text, sanitize_surrogates

# Curly quote, ellipsis, and an accented letter — the characters seen as mojibake.
_TRICKY = "Acme’s café raises €5m… “more to come”"


def _resp(body: bytes, content_type: str) -> requests.Response:
    """A Response as `requests` hands it back: `encoding` seeded from the headers
    exactly like a real network fetch (ISO-8859-1 for a charset-less text body)."""
    resp = requests.Response()
    resp._content = body
    resp.status_code = 200
    resp.headers["Content-Type"] = content_type
    resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
    return resp


def test_declared_charset_parsing():
    assert declared_charset(_resp(b"x", "text/html")) is None
    assert declared_charset(_resp(b"x", "text/html; charset=utf-8")) == "utf-8"
    assert declared_charset(_resp(b"x", 'text/html; charset="ISO-8859-1"')) == "ISO-8859-1"


def test_utf8_body_without_charset_is_decoded_as_utf8():
    # The bug: no charset header -> requests would fall back to ISO-8859-1 mojibake.
    resp = _resp(_TRICKY.encode("utf-8"), "text/html")
    assert response_text(resp) == _TRICKY


def test_declared_charset_is_respected_utf8():
    resp = _resp(_TRICKY.encode("utf-8"), "text/html; charset=utf-8")
    assert response_text(resp) == _TRICKY


def test_genuine_latin1_with_declared_charset_stays_correct():
    # A real ISO-8859-1 page that SAYS so must not be re-sniffed into garbage.
    body = "café résumé".encode("latin-1")
    resp = _resp(body, "text/html; charset=iso-8859-1")
    assert response_text(resp) == "café résumé"


def test_raw_mojibake_is_what_the_old_path_produced():
    # Guard the premise: the old `resp.text` really did mangle this body, so the
    # fix above is doing real work (not a no-op on an already-correct string).
    resp = _resp(_TRICKY.encode("utf-8"), "text/html")
    assert resp.text != _TRICKY  # ISO-8859-1 fallback = mojibake
    assert response_text(resp) == _TRICKY


# --- #127: lone surrogates in crawled text must be made UTF-8-encodable ----------

# The exact lone high surrogate that crashed an acquisition_proposal job in prod.
_SURROGATE = "\udb11"


def test_sanitize_surrogates_replaces_lone_surrogate():
    out = sanitize_surrogates(f"Acme acquired Globex {_SURROGATE} in 2024")
    assert _SURROGATE not in out
    assert "�" in out
    out.encode("utf-8")  # the prod crash was this encode raising — must not now


def test_sanitize_surrogates_is_noop_on_clean_text():
    # Clean text (curly quotes / accents included) is returned unchanged, same object.
    clean = "Acme’s café — €5m…"
    assert sanitize_surrogates(clean) is clean


def test_sanitize_surrogates_preserves_surrounding_text():
    assert sanitize_surrogates(f"a{_SURROGATE}b") == "a�b"


def test_sanitize_surrogates_handles_low_and_high_surrogates_and_empty():
    assert sanitize_surrogates("") == ""
    # Whole surrogate range D800–DFFF is scrubbed (low surrogate too).
    assert sanitize_surrogates("x\uddffy") == "x�y"


# --- Feed XML parses from bytes, honouring the in-band encoding -----------------

_RSS_UTF8 = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<rss version="2.0"><channel>'
    "<item><title>Acme’s café opens…</title>"
    "<link>https://acme.example/news/x</link>"
    "<description>Résumé of the “launch” — €5m raised.</description></item>"
    "</channel></rss>"
).encode("utf-8")


def test_parse_feed_accepts_utf8_bytes_and_preserves_text():
    items = parse_feed(_RSS_UTF8, "https://acme.example/feed.xml")
    assert len(items) == 1
    assert items[0].title == "Acme’s café opens…"
    assert "Résumé" in items[0].summary and "“launch”" in items[0].summary


def test_parse_feed_still_accepts_str():
    rss = (
        '<rss version="2.0"><channel><item><title>Acme’s news…</title>'
        "<link>https://acme.example/y</link></item></channel></rss>"
    )
    items = parse_feed(rss, "https://acme.example/feed.xml")
    assert items[0].title == "Acme’s news…"


def test_discover_feeds_accepts_bytes():
    html = (
        '<html><head><link rel="alternate" type="application/rss+xml" '
        'href="/feed.xml"></head><body>Globex ships… café</body></html>'
    ).encode("utf-8")
    assert discover_feeds(html, "https://globex.example/") == ["https://globex.example/feed.xml"]
