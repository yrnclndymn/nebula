"""LinkedIn canonicalisation + surfacing a page's social links."""

from app.tools.social import (
    find_social_links,
    normalize_linkedin,
    pick_social_href,
    social_domains_for,
)


def test_normalize_drops_country_subdomain():
    # The exact case from the field: a search-sourced uk. URL → canonical www.
    assert (
        normalize_linkedin("https://uk.linkedin.com/company/nextwave-consulting")
        == "https://www.linkedin.com/company/nextwave-consulting"
    )
    assert (
        normalize_linkedin("https://de.linkedin.com/company/acme/")
        == "https://www.linkedin.com/company/acme"
    )


def test_normalize_forces_www_and_strips_trailing_slash():
    assert (
        normalize_linkedin("https://www.linkedin.com/company/acme-ltd/")
        == "https://www.linkedin.com/company/acme-ltd"
    )
    assert (
        normalize_linkedin("linkedin.com/company/acme") == "https://www.linkedin.com/company/acme"
    )


def test_normalize_drops_query_and_fragment():
    assert (
        normalize_linkedin("https://uk.linkedin.com/company/acme/?originalSubdomain=uk")
        == "https://www.linkedin.com/company/acme"
    )


def test_normalize_leaves_non_linkedin_untouched():
    assert normalize_linkedin("https://x.com/acme") == "https://x.com/acme"
    assert normalize_linkedin("") == ""


def test_normalize_does_not_fabricate_from_lookalike_host():
    # A host that merely ends with the string "linkedin.com" must NOT be rewritten
    # into a canonical LinkedIn URL (that would fabricate false provenance).
    for host in ("notlinkedin.com", "evil-linkedin.com", "mylinkedin.com.attacker.io"):
        url = f"https://{host}/company/acme"
        assert normalize_linkedin(url) == url


def test_find_social_links_surfaces_canonical_linkedin():
    html = """
    <footer>
      <a href="https://www.linkedin.com/sharer/?url=https://acme.com">Share</a>
      <a href="https://uk.linkedin.com/company/acme-ltd/">LinkedIn</a>
      <a href="https://twitter.com/acme">Twitter</a>
      <a href="https://github.com/acme">GitHub</a>
    </footer>
    """
    social = find_social_links(html)
    # Prefers the /company/ profile over the share link, and canonicalises it.
    assert social["linkedin"] == "https://www.linkedin.com/company/acme-ltd"
    assert social["twitter"] == "https://twitter.com/acme"
    assert social["github"] == "https://github.com/acme"


def test_find_social_links_empty_when_none_present():
    assert find_social_links("<p>no socials here</p>") == {}


def test_handles_protocol_relative_hrefs():
    # Footer icons emitted as //linkedin.com/... must still be found (and canonicalised).
    html = '<a href="//linkedin.com/company/acme">in</a><a href="//twitter.com/acme">tw</a>'
    social = find_social_links(html)
    assert social["linkedin"] == "https://www.linkedin.com/company/acme"
    assert social["twitter"] == "https://twitter.com/acme"


def test_pick_rejects_lookalike_host():
    # A look-alike host must NOT be matched as LinkedIn at the pick layer either, so
    # the two layers agree and nothing gets surfaced as the company's LinkedIn.
    lookalike = '<a href="https://business-linkedin.com/company/acme">x</a>'
    assert pick_social_href(lookalike, social_domains_for("LinkedIn")) is None
    assert find_social_links(lookalike) == {}
    # A real subdomain still matches.
    real = '<a href="https://uk.linkedin.com/company/acme">x</a>'
    assert (
        pick_social_href(real, social_domains_for("LinkedIn"))
        == "https://uk.linkedin.com/company/acme"
    )
