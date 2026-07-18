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


def deep_sanitize(value):
    """Apply :func:`sanitize_surrogates` to every string in a JSON-like value.

    A crawled page carries surrogate-prone text in many nested places — link
    text, image ``alt``s, social URLs — not just the aggregate ``text`` field, and
    every one of them eventually hits a UTF-8 encoder (the Neo4j driver on write,
    the Gemini client when a tool result is serialized). One deep walk at the
    fetch boundary covers them all; clean strings come back unchanged (same
    object), so the common clean page pays almost nothing (#146 review).
    """
    if isinstance(value, str):
        return sanitize_surrogates(value)
    if isinstance(value, list):
        return [deep_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: deep_sanitize(v) for k, v in value.items()}
    return value
