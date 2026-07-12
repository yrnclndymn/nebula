"""Candidate extraction from web-search results (issue #75).

Pure: search-result dicts in, candidate companies out. Each result is
`{title, url, snippet}`; a candidate is `{name, website, why, sources}`, keyed and
de-duplicated by its official domain (the same company recurs across queries). The
`why` is the subset of profile match terms that appear in the result's text — the
evidence the reviewer sees. Directory / social / press hosts are skipped (they are
never a company's own site), matching the enrichment website-discovery filter.

Extracted names and domains are UNTRUSTED input: they only ever seed a proposal
the user reviews, never a write.
"""

from urllib.parse import urlparse

# Hosts that are never a company's own official site. Mirrors the enrichment
# discovery blocklist (app.agents.assistant.proposals) — kept local so this pure
# module stays free of the ADK import chain.
_NON_OFFICIAL_HOSTS = (
    "linkedin.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "crunchbase.com",
    "bloomberg.com",
    "pitchbook.com",
    "github.com",
    "medium.com",
    "glassdoor.com",
    "indeed.com",
    "g2.com",
    "trustpilot.com",
    "reddit.com",
    "apps.apple.com",
    "play.google.com",
    "owler.com",
    "zoominfo.com",
    "craft.co",
    "producthunt.com",
    "similarweb.com",
    "clutch.co",
    "forbes.com",
    "techcrunch.com",
    "gartner.com",
    "capterra.com",
    "quora.com",
)

# Title separators — a search-result title is usually "Company Name - tagline" or
# "Company | strapline"; we keep the leading segment as the company name.
_TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " · ", ": ", " • ")

_MAX_NAME_WORDS = 6  # a plausible company name, not a sentence lifted from a title


def official_domain(url: str) -> str | None:
    """The bare host of a result URL if it plausibly is a company's own site, else
    None (a social / directory / press host we skip)."""
    if not url:
        return None
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    host = host.split("@")[-1].split(":")[0].removeprefix("www.")
    if not host:
        return None
    if any(host == bad or host.endswith("." + bad) for bad in _NON_OFFICIAL_HOSTS):
        return None
    return host


def candidate_name(title: str) -> str:
    """A company name guessed from a search-result title (leading segment before a
    separator). Returns "" if it doesn't look like a name."""
    name = (title or "").strip()
    for sep in _TITLE_SEPARATORS:
        if sep in name:
            name = name.split(sep)[0].strip()
            break
    if not name or len(name.split()) > _MAX_NAME_WORDS:
        return ""
    return name


def extract_candidates(
    results: list[dict], terms: list[str], *, exclude_domains: set[str] | None = None
) -> list[dict]:
    """Turn raw search results into de-duplicated candidate companies.

    Keyed by official domain; a candidate that recurs across queries unions its
    source URLs and matched `why` terms. `exclude_domains` drops known hosts up
    front (e.g. the seed's own site). Ordering is deterministic: more matched terms
    first, then name — the caller may re-sort, but a stable base helps tests.
    """
    exclude = exclude_domains or set()
    lowered_terms = [t.lower() for t in terms if t]
    by_domain: dict[str, dict] = {}

    for hit in results:
        url = hit.get("url", "") or ""
        # Only real web URLs: `sources` is rendered as links in the review UI, so a
        # hostile result with a javascript:/data: scheme must never get through.
        if not url.lower().startswith(("http://", "https://")):
            continue
        domain = official_domain(url)
        if not domain or domain in exclude:
            continue
        name = candidate_name(hit.get("title", "") or "")
        if not name:
            continue
        haystack = f"{hit.get('title', '')} {hit.get('snippet', '')}".lower()
        matched = [t for t in lowered_terms if t in haystack]

        entry = by_domain.get(domain)
        if entry is None:
            entry = {"name": name, "website": domain, "why": [], "sources": []}
            by_domain[domain] = entry
        if url and url not in entry["sources"]:
            entry["sources"].append(url)
        for t in matched:
            if t not in entry["why"]:
                entry["why"].append(t)

    candidates = list(by_domain.values())
    candidates.sort(key=lambda c: (-len(c["why"]), c["name"].lower()))
    return candidates
