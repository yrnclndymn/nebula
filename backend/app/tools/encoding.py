"""UTF-8-safe decoding of ``requests`` responses (#89).

When a server returns a ``text/*`` body with no ``charset`` in its ``Content-Type``,
``requests`` follows RFC 2616 and decodes it as ISO-8859-1 — which mangles UTF-8
bytes into mojibake (``’`` becomes ``â€™``, ``…`` becomes ``â€¦``). Every raw fetch
site in the capture/crawl paths reads ``resp.text``, so charset-less UTF-8 pages
(the common case) were being corrupted before parsing.

`response_text` centralises the policy: honour an explicitly declared charset, but
when none is declared prefer the body's own sniffed encoding (``apparent_encoding``)
and fall back to UTF-8 — never the ISO-8859-1 default. A genuinely ISO-8859-1 page
that *declares* its charset is untouched and stays correct.

This module deliberately depends only on ``requests`` so every fetch site (web.py,
capture/job.py, agents/people/person_discovery.py) can import it without a cycle.
"""

import requests

# `sanitize_surrogates` now lives in the graph layer so `app.graph.cache` can
# reuse it without importing up into `app.tools` (the layered contract forbids
# that direction). Re-exported here so this module's own fetch-site callers and
# the evidence-boundary consumers keep their `from app.tools.encoding import ...`.
from app.graph.sanitize import sanitize_surrogates

__all__ = ["declared_charset", "response_text", "sanitize_surrogates"]


def declared_charset(resp: requests.Response) -> str | None:
    """The charset explicitly declared in the response's ``Content-Type`` header,
    or None when the header omits one. Parsed directly (not via requests) so we can
    tell an *explicit* ``charset=iso-8859-1`` apart from requests' silent default."""
    content_type = resp.headers.get("content-type", "")
    for part in content_type.split(";")[1:]:
        key, sep, value = part.strip().partition("=")
        if sep and key.strip().lower() == "charset":
            return value.strip().strip('"').strip("'") or None
    return None


def response_text(resp: requests.Response) -> str:
    """Decoded body text, choosing the encoding UTF-8-safely.

    If the server declared a charset we trust it (so a real ISO-8859-1 page stays
    correct). If it declared none, we prefer the body-sniffed encoding and default
    to UTF-8 — anything but requests' ISO-8859-1 fallback, which is what produced
    the captured mojibake.

    Two hardenings (#131): a *sniffed* UTF-7 is distrusted — effectively extinct on
    the real web, it's a known sniffer false positive on '+xxx-' runs, and decoding
    a UTF-8 body with it yields mojibake plus lone surrogates. And the returned text
    is always UTF-8-encodable: UTF-7 (and only a handful of codecs like it) can
    decode to lone surrogates, which crash the very first downstream UTF-8 encode —
    lxml encodes parser input before any later sanitization can run.
    """
    if declared_charset(resp) is None:
        sniffed = resp.apparent_encoding or "utf-8"
        if sniffed.replace("-", "_").lower() == "utf_7":
            sniffed = "utf-8"
        resp.encoding = sniffed
    return sanitize_surrogates(resp.text)
