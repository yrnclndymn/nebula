"""Social/profile-URL field extraction (deterministic footer-link scan)."""

from app.tools.social import pick_social_href as _pick_social_href
from app.tools.social import social_domains_for as _social_domains_for


def test_label_maps_to_social_domains():
    assert "linkedin.com" in _social_domains_for("LinkedIn")
    assert "linkedin.com" in _social_domains_for("LinkedIn URL")
    assert "x.com" in _social_domains_for("Twitter")
    assert "github.com" in _social_domains_for("GitHub")
    # not a social field, and 'x' must not match inside another word
    assert _social_domains_for("Service Lines") == ()
    assert _social_domains_for("Extra headcount") == ()


def test_picks_company_profile_over_share_link():
    html = """
    <footer>
      <a href="https://www.linkedin.com/sharer/?url=https://acme.com">Share</a>
      <a href="https://www.linkedin.com/company/acme-ltd/">LinkedIn</a>
      <a href="https://twitter.com/acme">Twitter</a>
    </footer>
    """
    assert (
        _pick_social_href(html, _social_domains_for("LinkedIn"))
        == "https://www.linkedin.com/company/acme-ltd/"
    )


def test_strips_query_and_ignores_share_only():
    assert (
        _pick_social_href(
            '<a href="https://x.com/acme?ref=footer">x</a>', _social_domains_for("Twitter")
        )
        == "https://x.com/acme"
    )
    assert (
        _pick_social_href(
            '<a href="https://linkedin.com/sharing/share-offsite/?url=x">s</a>',
            _social_domains_for("LinkedIn"),
        )
        is None
    )
    assert _pick_social_href('<a href="/about">About</a>', _social_domains_for("LinkedIn")) is None
