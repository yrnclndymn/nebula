"""Pure-logic tests for Person identity keyed on a canonical LinkedIn URL (#39).

No database — these cover URL canonicalisation, name↔slug evidence matching, and
profile-link extraction. All fixtures use fictional people and
`linkedin.com/in/fictional-slug` URLs (public-repo rule).
"""

import pytest

from app.graph.person_identity import (
    canonical_linkedin,
    extract_person_linkedins,
    linkedin_slug_matches_name,
)

# --- canonical_linkedin -----------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Already canonical.
        (
            "https://www.linkedin.com/in/jane-placeholder",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # Trailing slash stripped.
        (
            "https://www.linkedin.com/in/jane-placeholder/",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # Slug case folded (LinkedIn slugs are case-insensitive).
        (
            "https://www.linkedin.com/in/Jane-Placeholder",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # Country subdomain -> www.
        (
            "https://uk.linkedin.com/in/jane-placeholder",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # Mobile subdomain -> www.
        (
            "https://m.linkedin.com/in/jane-placeholder",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # http scheme -> https.
        (
            "http://www.linkedin.com/in/jane-placeholder",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # Scheme-less bare host.
        ("linkedin.com/in/jane-placeholder", "https://www.linkedin.com/in/jane-placeholder"),
        # Query + fragment dropped.
        (
            "https://www.linkedin.com/in/jane-placeholder?trk=public&originalSubdomain=uk#about",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # Extra profile sub-path collapses to the profile itself.
        (
            "https://www.linkedin.com/in/jane-placeholder/detail/contact-info/",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
        # All the defeating variants collapse to ONE identity.
        (
            "HTTP://UK.LinkedIn.com/in/Jane-Placeholder/?utm_source=x",
            "https://www.linkedin.com/in/jane-placeholder",
        ),
    ],
)
def test_canonical_linkedin_variants_collapse(raw, expected):
    assert canonical_linkedin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        # Company / school pages are not a *person's* identity.
        "https://www.linkedin.com/company/acme-corp",
        "https://www.linkedin.com/school/globex-university",
        # Bare LinkedIn host, no profile.
        "https://www.linkedin.com",
        "https://www.linkedin.com/",
        # Not LinkedIn at all — must never be rewritten into a fake profile.
        "https://example.com/in/jane-placeholder",
        "https://notlinkedin.com/in/jane-placeholder",
        "not a url",
    ],
)
def test_canonical_linkedin_rejects_non_person_profiles(raw):
    assert canonical_linkedin(raw) is None


def test_canonical_linkedin_is_idempotent():
    once = canonical_linkedin("https://uk.linkedin.com/in/Jane-Placeholder/")
    assert canonical_linkedin(once) == once


# --- linkedin_slug_matches_name ---------------------------------------------


@pytest.mark.parametrize(
    "url_or_slug, name",
    [
        ("https://www.linkedin.com/in/jane-placeholder", "Jane Placeholder"),
        ("jane-placeholder", "Jane Placeholder"),
        # Trailing numeric disambiguation hash LinkedIn appends.
        ("jane-placeholder-8a4b12", "Jane Placeholder"),
        # Order-independent; middle name in the display name is fine.
        ("placeholder-jane", "Jane Q Placeholder"),
        # Case-insensitive.
        ("JANE-PLACEHOLDER", "jane placeholder"),
    ],
)
def test_slug_matches_when_first_and_last_present(url_or_slug, name):
    assert linkedin_slug_matches_name(url_or_slug, name) is True


@pytest.mark.parametrize(
    "url_or_slug, name",
    [
        # Only the surname — not enough evidence (namesakes).
        ("placeholder", "Jane Placeholder"),
        # Different person, same company page.
        ("john-stand-in", "Jane Placeholder"),
        # First name only.
        ("jane", "Jane Placeholder"),
        # A vanity slug with no name overlap.
        ("acme-ceo", "Jane Placeholder"),
        ("", "Jane Placeholder"),
        ("jane-placeholder", ""),
        # A single-token display name can never be confidently matched.
        ("jane-placeholder", "Cher"),
    ],
)
def test_slug_no_match_without_both_names(url_or_slug, name):
    assert linkedin_slug_matches_name(url_or_slug, name) is False


# --- extract_person_linkedins -----------------------------------------------


def test_extract_person_linkedins_finds_profiles_only():
    html = """
    <html><body>
      <a href="https://www.linkedin.com/company/acme-corp">Acme on LinkedIn</a>
      <a href="https://uk.linkedin.com/in/Jane-Placeholder/">Jane</a>
      <a href="https://www.linkedin.com/in/john-stand-in?trk=x">John</a>
      <a href="https://twitter.com/acme">Twitter</a>
      <a href="/team/jane">Jane bio</a>
    </body></html>
    """
    found = extract_person_linkedins(html)
    assert found == {
        "https://www.linkedin.com/in/jane-placeholder",
        "https://www.linkedin.com/in/john-stand-in",
    }


def test_extract_person_linkedins_empty_when_none():
    assert extract_person_linkedins("<html><body>no links</body></html>") == set()


# --- discovery orchestration (offline, monkeypatched I/O) --------------------


def test_discover_for_company_prefers_own_site_then_search(monkeypatch):
    from app.graph import person_discovery as disc

    # Own-site pages: home links a /leadership page which carries Jane's profile;
    # John is not on the site (forces the search fallback).
    pages = {
        "https://acme.example": '<a href="/leadership">Leaders</a>',
        "https://acme.example/leadership": '<a href="https://www.linkedin.com/in/jane-placeholder">Jane</a>',
    }
    monkeypatch.setattr(disc, "_fetch", lambda url: pages.get(url, ""))
    # Search fallback returns candidates; only the slug-matching one is attached,
    # and a non-matching hit for John is rejected (untrusted — proximity isn't enough).
    monkeypatch.setattr(
        disc,
        "web_search",
        lambda q: {
            "results": [
                {"url": "https://www.linkedin.com/in/someone-else"},
                {"url": "https://uk.linkedin.com/in/John-Standin/"},
            ]
        },
    )

    rows = disc.discover_for_company(
        "Acme", "https://acme.example", ["Jane Placeholder", "John Standin"]
    )
    by_person = {r["person"]: r for r in rows}
    assert by_person["Jane Placeholder"]["url"] == "https://www.linkedin.com/in/jane-placeholder"
    assert by_person["Jane Placeholder"]["evidence"] == "own-site"
    assert by_person["John Standin"]["url"] == "https://www.linkedin.com/in/john-standin"
    assert by_person["John Standin"]["evidence"] == "search"


def test_discover_for_company_skips_when_no_evidence(monkeypatch):
    from app.graph import person_discovery as disc

    monkeypatch.setattr(disc, "_fetch", lambda url: "<html>nothing useful</html>")
    # Search returns a profile for a DIFFERENT person — must not be attached.
    monkeypatch.setattr(
        disc,
        "web_search",
        lambda q: {"results": [{"url": "https://www.linkedin.com/in/unrelated-person"}]},
    )
    rows = disc.discover_for_company("Acme", "https://acme.example", ["Jane Placeholder"])
    assert rows == []
