"""Lone-surrogate sanitization for untrusted crawled text (#127, #130).

Pure string logic with no dependencies, deliberately parked in the ``graph``
layer so the read-through page cache (`app.graph.cache`) can call it without
importing *up* into ``app.tools`` (the import lattice forbids that direction).
`app.tools.encoding` re-exports `sanitize_surrogates` so existing fetch-site and
evidence-boundary callers keep their import unchanged.
"""


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
