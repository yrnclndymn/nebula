"""Canonical LinkedIn identity key — pure string logic (#39, #183).

A person is identified by their *canonical* LinkedIn personal-profile URL, so
trailing-slash / case / country- or mobile-subdomain variants can never mint two
identities for one human. :func:`canonical_linkedin` is the single reduction.

Deliberately parked in the ``graph`` layer — the same move ``sanitize.py`` makes,
and for the same reason. The committable record models (`graph.models.Leader`,
`graph.person_models.PersonRecord`) canonicalise their ``linkedin`` field in a
pydantic validator so a NON-canonical URL can never enter the write path; a
validator on a graph-layer model must therefore reach its canonicaliser
*downward* (or sideways within ``graph``), never *up* into ``app.agents.people``.
Placing it here dissolves that would-be upward import at the model level instead
of pinning it. ``app.agents.people.person_identity`` re-exports it so the people
domain (discovery, build) keeps its existing import unchanged.

Pure: no Neo4j, no ``app.tools`` dependency. It reimplements the minimal LinkedIn
host-normalisation it needs (scheme, subdomain, trailing slash) rather than
leaning on ``app.tools.social.normalize_linkedin`` — reaching UP into ``tools``
from ``graph`` is exactly the import the lattice forbids, and a person's identity
key is a strictly narrower operation (personal profiles only) than that generic
company-link normaliser.
"""

from urllib.parse import urlparse


def _ensure_scheme(url: str) -> str:
    """Give a URL a scheme so ``urlparse`` populates ``netloc`` — handling
    protocol-relative (``//linkedin.com/...``) and bare (``linkedin.com/...``)
    inputs alike."""
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if "://" not in u:
        return "https://" + u
    return u


def canonical_linkedin(url: str | None) -> str | None:
    """Reduce a LinkedIn *personal-profile* URL to its canonical identity key.

    Returns ``https://www.linkedin.com/in/<slug>`` (slug lower-cased) for a
    personal profile, or ``None`` for empty input, a company/school page, a bare
    LinkedIn host, or any non-LinkedIn URL. Scheme, ``www``/country/mobile
    subdomain, query, fragment, trailing slash and slug case are all normalised
    away, and any deeper profile sub-path (``/in/<slug>/detail/...``) collapses to
    the profile itself. Pure and idempotent.

    Company and school pages, and non-LinkedIn URLs, return ``None``: they are not
    a person's identity and must never be rewritten into a fake profile.
    """
    if not url or not url.strip():
        return None
    parsed = urlparse(_ensure_scheme(url))
    host = parsed.netloc.lower()
    # Exact host or a real subdomain only — NOT a substring, so we never rewrite
    # e.g. notlinkedin.com into a fabricated www.linkedin.com URL.
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    # Only a personal profile (/in/<slug>) identifies a person.
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "in":
        return None
    slug = parts[1].lower()  # LinkedIn slugs are case-insensitive
    return f"https://www.linkedin.com/in/{slug}" if slug else None
