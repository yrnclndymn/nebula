"""Person enrichment (#40): provenance-gated build, diff, and the durable
propose→review→commit job flow.

The build/diff logic is PURE (no DB, no model, no network) so it runs anywhere and
is the real guardrail under test: no fact is committable without a valid citation.
The job-flow tests mock research + the graph so they need neither Gemini nor Neo4j.
All fixtures use fictional people (Jane Placeholder) and companies (Acme/Globex).
"""

import asyncio

from app.agents.people.build import build_person_record, diff_person, valid_source
from app.agents.people.models import PersonCitation, PersonRecord, PersonResearch, PriorRole

SLUG = "jane-placeholder-pytest40"
CANON = f"https://www.linkedin.com/in/{SLUG}"


def _cite(field, value="v", source="https://news.example/jane"):
    return PersonCitation(field=field, value=value, source=source)


# --- provenance gate (pure) ---------------------------------------------------


def test_uncited_facts_are_dropped():
    """A fact with no citation must never survive to the committable record."""
    research = PersonResearch(
        name="Jane Placeholder",
        current_title="CEO",
        bio="Builds things at Acme.",
        citations=[],  # nothing cited
    )
    record = build_person_record(research, company="Acme")
    assert record.title is None
    assert record.bio is None
    assert record.citations == []
    assert not record.has_facts()


def test_cited_facts_survive_with_only_their_citations():
    research = PersonResearch(
        name="Jane Placeholder",
        current_title="CEO",
        bio="Builds things at Acme.",
        citations=[
            _cite("title", "Chief Executive", "https://acme.example/team"),
            _cite("bio", "Builds things", "https://acme.example/about"),
        ],
    )
    record = build_person_record(research, company="Acme")
    assert record.title == "CEO"
    assert record.bio == "Builds things at Acme."
    assert {c.field for c in record.citations} == {"title", "bio"}
    assert record.has_facts()


def test_linkedin_is_canonicalised_and_requires_a_citation():
    # cited but non-canonical URL -> stored canonical
    cited = PersonResearch(
        name="Jane Placeholder",
        linkedin=f"https://UK.linkedin.com/in/{SLUG.title()}/",
        citations=[_cite("linkedin", CANON, "https://acme.example/team")],
    )
    assert build_person_record(cited, "Acme").linkedin == CANON

    # same URL but NO citation -> dropped
    uncited = PersonResearch(name="Jane Placeholder", linkedin=CANON, citations=[])
    assert build_person_record(uncited, "Acme").linkedin is None

    # a company page is not a personal identity -> dropped even if "cited"
    company_url = PersonResearch(
        name="Jane Placeholder",
        linkedin="https://www.linkedin.com/company/acme",
        citations=[_cite("linkedin", "x", "https://acme.example")],
    )
    assert build_person_record(company_url, "Acme").linkedin is None


def test_hostile_source_scheme_is_rejected():
    """A javascript:/data: source must not qualify a fact (sources render as links)."""
    research = PersonResearch(
        name="Jane Placeholder",
        bio="evil",
        citations=[PersonCitation(field="bio", value="x", source="javascript:alert(1)")],
    )
    record = build_person_record(research, "Acme")
    assert record.bio is None
    assert record.citations == []
    assert valid_source("https://ok.example") and not valid_source("data:text/html,x")


def test_talks_kept_only_when_valid_urls_and_cited():
    research = PersonResearch(
        name="Jane Placeholder",
        talks=["https://conf.example/talk", "not-a-url", "javascript:bad"],
        citations=[_cite("talks", "talk", "https://conf.example/talk")],
    )
    record = build_person_record(research, "Acme")
    assert record.talks == ["https://conf.example/talk"]  # invalid ones filtered

    # talks present but uncited -> dropped entirely
    uncited = PersonResearch(name="Jane Placeholder", talks=["https://conf.example/t"])
    assert build_person_record(uncited, "Acme").talks == []


def test_prior_roles_require_company_and_valid_source():
    research = PersonResearch(
        name="Jane Placeholder",
        prior_roles=[
            PriorRole(
                company="Globex",
                title="VP",
                from_year=2015,
                to_year=2020,
                source="https://globex.example/news",
            ),
            PriorRole(company="NoSource Inc", title="Analyst"),  # dropped: no source
            PriorRole(company="", title="Ghost", source="https://x.example"),  # dropped: no company
        ],
    )
    record = build_person_record(research, "Acme")
    assert [r.company for r in record.prior_roles] == ["Globex"]


def test_personal_site_must_be_url_and_cited():
    research = PersonResearch(
        name="Jane Placeholder",
        personal_site="https://jane.example",
        citations=[_cite("personal_site", "site", "https://jane.example")],
    )
    assert build_person_record(research, "Acme").personal_site == "https://jane.example"


def test_diff_surfaces_only_changed_cited_facts():
    record = PersonRecord(
        name="Jane Placeholder",
        company="Acme",
        title="CEO",
        bio="New bio.",
        prior_roles=[PriorRole(company="Globex", title="VP", source="https://x.example")],
    )
    existing = {"title": "CEO", "bio": "Old bio.", "prior_roles": []}
    changes = {c["field"]: c for c in diff_person(existing, record)}
    assert "title" not in changes  # unchanged
    assert changes["bio"]["old"] == "Old bio." and changes["bio"]["new"] == "New bio."
    assert changes["prior_roles"]["old"] == 0

    assert diff_person(None, PersonRecord(name="X", company="Acme")) == []  # nothing to say


# --- durable job flow (mocked research + graph; no Gemini, no Neo4j) -----------


def test_run_person_proposal_job_stores_cited_facts(monkeypatch):
    from app.agents.people import proposals

    job = {
        "job_id": "pj1",
        "status": "pending",
        "name": "Jane Placeholder",
        "company": "Acme",
        "linkedin": None,
    }
    saved: dict = {}

    async def fake_get_job(job_id):
        return job

    async def fake_update_job(job_id, data, status=None):
        saved.update(data)
        saved["status"] = status

    async def fake_research(name, company, linkedin=None):
        return PersonResearch(
            name=name,
            current_title="CEO",
            bio="Leads Acme.",
            citations=[
                _cite("title", "CEO", "https://acme.example/team"),
                _cite("bio", "Leads Acme", "https://acme.example/about"),
            ],
        )

    async def fake_existing(driver, name, company):
        return {"title": None, "bio": None, "prior_roles": []}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_person", fake_research)
    monkeypatch.setattr(proposals, "get_person_scoped", fake_existing)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.run_person_proposal_job("pj1"))

    assert saved["status"] == "ready"
    assert saved["record"]["title"] == "CEO"
    assert saved["record"]["bio"] == "Leads Acme."
    assert {c["field"] for c in saved["record"]["citations"]} == {"title", "bio"}
    assert saved["exists"] is True


def test_commit_refuses_when_not_ready(monkeypatch):
    from app.agents.people import proposals

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "pending"}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    res = asyncio.run(proposals.commit_person_proposal("pj1"))
    assert "error" in res


def test_commit_refuses_when_no_cited_facts(monkeypatch):
    from app.agents.people import proposals

    empty_record = PersonRecord(name="Jane Placeholder", company="Acme").model_dump()

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "ready", "record": empty_record}

    called = {"upsert": False}

    async def fake_upsert(driver, record):
        called["upsert"] = True
        return {"action": "written"}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "upsert_person", fake_upsert)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    res = asyncio.run(proposals.commit_person_proposal("pj1"))
    assert "error" in res
    assert called["upsert"] is False  # nothing written


def test_commit_writes_and_flips_status(monkeypatch):
    from app.agents.people import proposals

    record = PersonRecord(
        name="Jane Placeholder",
        company="Acme",
        bio="Leads Acme.",
        citations=[_cite("bio", "Leads Acme", "https://acme.example")],
    ).model_dump()
    updates: dict = {}

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "ready", "record": record}

    async def fake_update_job(job_id, data, status=None):
        updates.update(data)
        updates["status"] = status

    async def fake_upsert(driver, rec):
        return {"name": rec.name, "action": "written"}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "upsert_person", fake_upsert)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    res = asyncio.run(proposals.commit_person_proposal("pj1"))
    assert res["committed"] == "Jane Placeholder"
    assert updates["committed"] is True
    assert updates["status"] == "committed"  # prunable past retention after commit
