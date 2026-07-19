"""Own-site provenance for the company LinkedIn (issue #21).

Crawled/searched content is untrusted input that must never steer writes. The
enrichment agent gathers `linkedin` from crawled pages, so `save_company` must not
trust an agent-supplied LinkedIn as canonical unless its provenance is deterministic:
a citation whose source URL is on the company's OWN website domain (the same
own-site-scrape rule the back-fill uses). A LinkedIn lacking own-site provenance is
demoted — surfaced as a citation-only candidate for the reviewer, never the canonical
field.

These run purely in propose mode (no graph write, no DB): `proposal_sink` captures the
`CompanyRecord` that *would* be written, so we can assert on the attribution decision.
"""

import asyncio

from app.tools.graph_tools import proposal_sink, save_company
from app.tools.social import normalize_linkedin

OWN_SITE = "acme.example"
LINKEDIN = "https://www.linkedin.com/company/acme"


def _capture(**overrides) -> dict:
    """Run save_company in propose mode and return the captured record dict."""
    base = dict(
        name="Acme Co",
        topic="AI-native engineering",
        about="a test company",
        website=OWN_SITE,
        linkedin="",
        hq_location="",
        headcount=0,
        estimated_revenue="",
        year_founded=0,
        funding="",
        notes="",
        company_types=[],
        partnerships=[],
        clients=[],
        leadership=[],
        citations=[],
    )
    base.update(overrides)

    async def scenario():
        sink: list = []
        token = proposal_sink.set(sink)
        try:
            await save_company(**base)
        finally:
            proposal_sink.reset(token)
        return sink[0]

    return asyncio.run(scenario())


def _linkedin_citations(record: dict) -> list[dict]:
    return [c for c in record["citations"] if "linkedin" in c["field"].strip().lower()]


def test_own_site_cited_linkedin_is_canonical():
    """A LinkedIn cited to a page on the company's own domain is written as the
    canonical field."""
    record = _capture(
        linkedin=LINKEDIN,
        citations=[f"linkedin | {LINKEDIN} | https://{OWN_SITE}/about | 2025"],
    )
    assert record["linkedin"] == normalize_linkedin(LINKEDIN)


def test_search_derived_linkedin_is_not_canonical_but_surfaced():
    """GUARDRAIL: a LinkedIn whose only provenance is an off-site (search/article)
    page must NOT become the canonical field — untrusted content must not steer the
    write — yet it is not silently dropped: it is surfaced as a cited candidate for
    the reviewer to confirm."""
    record = _capture(
        linkedin=LINKEDIN,
        citations=[f"linkedin | {LINKEDIN} | https://search-aggregator.example/x | 2025"],
    )
    # Rejected as canonical.
    assert record["linkedin"] is None
    # Surfaced as a citation-only candidate (the CITES pattern).
    candidates = _linkedin_citations(record)
    assert len(candidates) == 1
    assert normalize_linkedin(candidates[0]["value"]) == normalize_linkedin(LINKEDIN)


def test_uncited_linkedin_is_demoted_to_candidate():
    """GUARDRAIL: a LinkedIn with no citation at all has no own-site provenance, so it
    is demoted — never canonical — but still surfaced as a candidate so the reviewer
    sees the agent's finding."""
    record = _capture(linkedin=LINKEDIN)
    assert record["linkedin"] is None
    candidates = _linkedin_citations(record)
    assert len(candidates) == 1
    assert normalize_linkedin(candidates[0]["value"]) == normalize_linkedin(LINKEDIN)


def test_absent_linkedin_unchanged():
    """No LinkedIn supplied → no canonical field and no synthesised candidate."""
    record = _capture(linkedin="")
    assert record["linkedin"] is None
    assert _linkedin_citations(record) == []


def test_own_site_match_ignores_www_prefix():
    """domain_of strips a www. prefix, so a citation on www.<site> still counts as
    own-site provenance for a bare-domain website."""
    record = _capture(
        website=f"www.{OWN_SITE}",
        linkedin=LINKEDIN,
        citations=[f"linkedin | {LINKEDIN} | https://{OWN_SITE}/company | 2025"],
    )
    assert record["linkedin"] == normalize_linkedin(LINKEDIN)


def test_no_website_cannot_authorize_linkedin():
    """With no company website there is no own domain to match, so an agent-claimed
    LinkedIn can never be canonical — only a candidate."""
    record = _capture(
        website="",
        linkedin=LINKEDIN,
        citations=[f"linkedin | {LINKEDIN} | https://{OWN_SITE}/about | 2025"],
    )
    assert record["linkedin"] is None
    assert len(_linkedin_citations(record)) == 1
