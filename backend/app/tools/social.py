"""Finding and canonicalising a company's social / profile URLs.

Both research paths need this. A social link on a site is usually an external <a href>
in the footer — often just an icon with no visible text — and `fetch_page` strips
external links while the page text has no URL, so an LLM never sees it. So we scan the
raw HTML for it deterministically:

- the back-fill (`field_extract`) grabs the one requested profile link, and
- the enrichment agent's `fetch_page` surfaces all of them under `social`, so it can
  prefer the site's OWN LinkedIn over a search-engine hit (which tends to be a
  locale-subdomain variant like `uk.linkedin.com`).

Lives here rather than in `web.py` to avoid a circular import (`field_extract` and
`web` both need it, and `field_extract` already imports from `web`).
"""

import re
from urllib.parse import urlparse

# Platform -> domain fragments, most-specific first so a "/company" path is preferred
# over a bare domain. Keys are matched against field labels as whole words.
SOCIAL_DOMAINS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com/company", "linkedin.com/school", "linkedin.com/in", "linkedin.com"),
    "twitter": ("x.com", "twitter.com"),
    "x": ("x.com", "twitter.com"),
    "github": ("github.com",),
    "facebook": ("facebook.com",),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "crunchbase": ("crunchbase.com",),
}
SHARE_MARKERS = ("/share", "sharer", "/intent", "sharearticle", "/sharing", "/shareon")

# Platforms worth surfacing from a fetched page for the enrichment agent to use.
_PAGE_SOCIAL_PLATFORMS = ("linkedin", "twitter", "github", "facebook", "instagram", "youtube")


def social_domains_for(label: str) -> tuple[str, ...]:
    """The social domains a field label maps to (e.g. 'LinkedIn' → linkedin.com), or
    () if it isn't a known social/profile field."""
    low = label.lower()
    for key, domains in SOCIAL_DOMAINS.items():
        if re.search(rf"\b{re.escape(key)}\b", low):
            return domains
    return ()


def pick_social_href(html: str, domains: tuple[str, ...]) -> str | None:
    """Pick the best matching profile URL from a page's hrefs (skips share links)."""
    hrefs = re.findall(r'href=["\']([^"\'#\s]+)["\']', html, re.I)
    hits = [
        h
        for h in hrefs
        if any(d in h.lower() for d in domains) and not any(m in h.lower() for m in SHARE_MARKERS)
    ]
    if not hits:
        return None
    for pref in domains:  # prefer a company/profile path over a bare domain
        for h in hits:
            if pref in h.lower():
                return h.split("?")[0]
    return hits[0].split("?")[0]


def normalize_linkedin(url: str) -> str:
    """Canonicalise a LinkedIn URL: force the host to www.linkedin.com (dropping a
    country subdomain like uk./de.), drop any query/fragment, and strip a trailing
    slash. A non-LinkedIn URL is returned unchanged."""
    if not url:
        return url
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = parsed.netloc.lower()
    # Exact host or a real subdomain only — NOT a substring, so we never rewrite
    # e.g. notlinkedin.com into a fabricated www.linkedin.com URL.
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return url
    return "https://www.linkedin.com" + parsed.path.rstrip("/")


def find_social_links(html: str) -> dict[str, str]:
    """The company's own social/profile URLs found in a page's hrefs, keyed by
    platform (linkedin, twitter, …). LinkedIn is returned in canonical form."""
    out: dict[str, str] = {}
    for platform in _PAGE_SOCIAL_PLATFORMS:
        url = pick_social_href(html, SOCIAL_DOMAINS[platform])
        if url:
            out[platform] = normalize_linkedin(url) if platform == "linkedin" else url
    return out
