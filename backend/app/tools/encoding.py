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
capture/job.py, graph/person_discovery.py) can import it without a cycle.
"""

import requests


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
    """
    if declared_charset(resp) is None:
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text
