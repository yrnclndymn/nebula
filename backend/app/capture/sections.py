"""Detect a company's news / blog / press / events index pages from its own links.

Pure keyword matching over on-site links (same idea as web.py's client-page
detection). Used only for the *fallback* path — when feed autodiscovery finds no
usable RSS/Atom feed, the capture job crawls these index pages and runs an LLM
extraction over them. The kind a section maps to (news / blog / event) becomes the
captured Signal's ``kind`` — one of ``models.SIGNAL_KINDS``.
"""

# Ordered so that a URL containing several hints resolves to the most specific
# kind first (an "events blog" is an event section, a "press blog" is news).
_SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("event", ("event", "webinar", "conference", "meetup", "workshop", "summit")),
    (
        "news",
        (
            "news",
            "press",
            "newsroom",
            "media-centre",
            "media-center",
            "announcement",
            "in-the-news",
        ),
    ),
    ("blog", ("blog", "insight", "article", "perspective", "thought-leadership", "stories")),
)


def classify_section(url: str, text: str = "") -> str | None:
    """The signal kind (news/blog/event) a link points at, or None if it's neither."""
    hay = (url + " " + text).lower()
    for kind, keywords in _SECTION_KEYWORDS:
        if any(keyword in hay for keyword in keywords):
            return kind
    return None


def find_section_pages(links: list[dict], limit_per_kind: int = 2) -> dict[str, list[str]]:
    """Group candidate index-page URLs by signal kind, from a page's links.

    ``links`` is the ``[{"url", "text"}]`` shape ``fetch_page`` returns. At most
    ``limit_per_kind`` URLs are kept per kind (bounds the fallback crawl), first
    occurrence wins, duplicates dropped.
    """
    grouped: dict[str, list[str]] = {}
    seen: set[str] = set()
    for link in links:
        url = (link.get("url") or "").strip()
        if not url or url in seen:
            continue
        kind = classify_section(url, link.get("text", ""))
        if kind is None:
            continue
        seen.add(url)
        bucket = grouped.setdefault(kind, [])
        if len(bucket) < limit_per_kind:
            bucket.append(url)
    return grouped
