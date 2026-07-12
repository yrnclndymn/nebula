"""RSS/Atom feed autodiscovery and parsing.

Pure, deterministic, no network — the capture job hands us page HTML and feed XML.

- ``discover_feeds`` reads ``<link rel="alternate" type="application/rss+xml|
  atom+xml">`` tags out of a page's ``<head>`` (the standard feed-autodiscovery
  contract) and returns absolute feed URLs.
- ``parse_feed`` parses either RSS 2.0 (``<item>``) or Atom (``<entry>``) into a
  common ``FeedItem`` shape (title / url / raw published date / summary), matching
  elements by local tag name so feed namespaces don't matter. Malformed XML yields
  an empty list rather than raising — a broken feed just captures nothing.
"""

from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as SafeET

from bs4 import BeautifulSoup

# Feed MIME substrings we accept on a rel="alternate" <link> tag.
_FEED_TYPE_HINTS = ("rss", "atom", "xml+feed", "feed+")


@dataclass
class FeedItem:
    """One entry from a feed. ``published_at`` is the raw string as published (the
    caller normalises it); ``summary`` is plain text with any HTML stripped."""

    title: str
    url: str
    published_at: str | None = None
    summary: str | None = None


def discover_feeds(html: str | bytes, base_url: str) -> list[str]:
    """Absolute feed URLs autodiscovered from a page's ``<link rel=alternate>`` tags.

    Only ``alternate`` links whose ``type`` looks like a feed (rss/atom) are kept;
    a plain ``text/html`` alternate (e.g. a mobile page) is ignored. Order is
    preserved and duplicates are collapsed. Accepts raw bytes (the capture job hands
    us undecoded HTML so BeautifulSoup can sniff the meta charset — see #89).
    """
    soup = BeautifulSoup(html or "", "lxml")
    feeds: list[str] = []
    seen: set[str] = set()
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        rel_text = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
        if "alternate" not in rel_text:
            continue
        type_hint = (link.get("type") or "").lower()
        if not any(h in type_hint for h in _FEED_TYPE_HINTS):
            continue
        href = (link.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            feeds.append(absolute)
    return feeds


def _local(tag: str) -> str:
    """Local element name, dropping any ``{namespace}`` prefix (Atom is namespaced)."""
    return tag.rsplit("}", 1)[-1]


def _text(element) -> str | None:
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None


def _plain(value: str | None, limit: int = 500) -> str | None:
    """Strip any HTML markup from a summary and truncate; None stays None."""
    if not value:
        return None
    text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    text = " ".join(text.split())
    return text[:limit] or None


def _parse_entry(entry, base_url: str) -> FeedItem:
    title: str | None = None
    rss_link: str | None = None
    atom_link: str | None = None
    published: str | None = None
    updated: str | None = None
    summary: str | None = None
    content: str | None = None
    for child in entry:
        name = _local(child.tag)
        if name == "title":
            title = _text(child)
        elif name == "link":
            href = child.get("href")  # Atom carries the URL in an attribute
            if href:
                # Prefer rel="alternate" (the canonical page) over other rels.
                if child.get("rel", "alternate") == "alternate" or atom_link is None:
                    atom_link = href
            else:
                rss_link = _text(child)  # RSS carries it as element text
        elif name == "pubDate":
            published = _text(child)
        elif name == "published":
            published = _text(child)
        elif name == "updated":
            updated = _text(child)
        elif name in ("description", "summary"):
            summary = _text(child)
        elif name == "content":
            content = _text(child)
    url = rss_link or atom_link or ""
    if url and base_url:
        url = urljoin(base_url, url)
    return FeedItem(
        title=(title or "").strip(),
        url=url,
        published_at=published or updated,
        summary=_plain(summary or content),
    )


def parse_feed(xml_text: str | bytes, base_url: str = "") -> list[FeedItem]:
    """Parse RSS 2.0 or Atom feed XML into ``FeedItem``s (best-effort, never raises).

    Accepts either decoded text or raw bytes; passing the bytes straight from the
    fetch lets the parser honour the XML prolog / BOM, so UTF-8 feeds served without
    a charset header aren't mangled into mojibake (#89). Relative item links are
    absolutised against ``base_url``. Items with neither a title nor a URL are
    dropped. Unparseable XML returns ``[]``.
    """
    if not xml_text or not xml_text.strip():
        return []
    # Feed XML is untrusted external input: defusedxml blocks entity-expansion
    # bombs (billion laughs / quadratic blowup) that stock ElementTree allows.
    try:
        root = SafeET.fromstring(xml_text.strip())
    except (ET.ParseError, ValueError):
        return []
    items = [
        _parse_entry(el, base_url) for el in root.iter() if _local(el.tag) in ("item", "entry")
    ]
    return [it for it in items if it.title or it.url]
