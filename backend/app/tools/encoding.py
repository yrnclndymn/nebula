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


def sanitize_surrogates(text: str, replacement: str = "�") -> str:
    """Replace lone UTF-16 surrogate code points so ``text`` is UTF-8-encodable (#127).

    Crawled/searched content occasionally decodes to lone surrogates (U+D800–U+DFFF)
    — e.g. an HTML numeric character reference like ``&#xDB11;``. A Python ``str``
    can hold them, but encoding to UTF-8 — as the Gemini client does when serializing
    a prompt — raises ``UnicodeEncodeError: surrogates not allowed`` and kills the
    job. Untrusted crawled input must never be able to do that, so we replace each
    surrogate with the Unicode replacement character at the fetch/evidence boundary.

    Clean text is returned unchanged (same object): the common case pays only one
    encode attempt, and the scan/rebuild happens solely for text that needs it.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "".join(replacement if "\ud800" <= ch <= "\udfff" else ch for ch in text)
    return text


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
