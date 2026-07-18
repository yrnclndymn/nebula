"""Pin-free person canonicalisation (#183).

The identity key ``canonical_linkedin`` moved DOWN into the graph layer
(``app.graph.linkedin``) and the committable record models now canonicalise their
``linkedin`` field in a pydantic validator — the single choke point. These tests
prove:

1. the validator on each record model reduces a NON-canonical URL to the canonical
   key (or ``None`` for a non-profile URL) at construction time; and
2. a non-canonical URL entering EACH producer path (importer / enrichment agent
   via ``CompanyRecord`` → ``upsert_company``; people agent via
   ``build_person_record`` → ``upsert_person``; capture via ``link_signal_people``)
   lands canonical — so the write paths can trust their inputs and the old
   upward-import pins stay deleted.

Fictional people and ``linkedin.com/in/fictional-slug`` URLs only (public-repo
rule). Graph tests skip when Neo4j is unreachable — CI is the arbiter; run locally
with `make db-ephemeral` then `NEO4J_URI=... make test`.
"""

import asyncio

import pytest

from app.agents.people.build import build_person_record
from app.agents.people.models import PersonResearch
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord, Leader
from app.graph.person_enrichment import upsert_person
from app.graph.person_models import PersonCitation, PersonRecord
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

# A defeating variant: http, country subdomain, mixed-case slug, trailing slash,
# tracking query — all of which must collapse to the one canonical key.
NONCANON = "HTTP://UK.LinkedIn.com/in/Jane-Placeholder-pytest183/?utm_source=x"
CANON = "https://www.linkedin.com/in/jane-placeholder-pytest183"


# --- Pure: the identity key is a single, shared choke point ------------------


def test_domain_reexports_the_graph_layer_canonicaliser():
    """`person_identity.canonical_linkedin` is the very object defined in the graph
    layer — a re-export, not a copy — so there is one canonicalisation, one key."""
    from app.agents.people.person_identity import canonical_linkedin as domain_fn
    from app.graph.linkedin import canonical_linkedin as graph_fn

    assert domain_fn is graph_fn


# --- Pure: model validators canonicalise at construction ---------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (NONCANON, CANON),  # every defeating variant collapses
        (CANON, CANON),  # already canonical -> unchanged (idempotent)
        ("https://www.linkedin.com/company/acme", None),  # company page -> name-key
        (None, None),
        ("", None),
    ],
)
def test_leader_validator_canonicalises_linkedin(raw, expected):
    assert Leader(name="Jane Placeholder", linkedin=raw).linkedin == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (NONCANON, CANON),
        (CANON, CANON),
        ("https://www.linkedin.com/school/globex", None),
        (None, None),
    ],
)
def test_person_record_validator_canonicalises_linkedin(raw, expected):
    assert PersonRecord(name="Jane", company="Acme", linkedin=raw).linkedin == expected


# --- Pure: the people-agent producer (build_person_record) -------------------


def test_build_person_record_yields_canonical_linkedin():
    """The people-agent producer keeps a cited LinkedIn — and a non-canonical URL
    lands canonical on the committable record (validator + build-time canonicalise
    agree, both idempotent)."""
    research = PersonResearch(
        name="Jane Placeholder",
        linkedin=NONCANON,
        citations=[PersonCitation(field="linkedin", value=NONCANON, source="https://co.example")],
    )
    record = build_person_record(research, company="Acme")
    assert record.linkedin == CANON


# --- Pure: the capture producer canonicalises its matching key ---------------


def test_capture_canonicalises_mention_linkedin_before_matching(monkeypatch):
    """`link_signal_people` matches a captured mention on the CANONICAL LinkedIn
    key, so a non-canonical crawled URL still resolves to the right person node."""
    from app.capture import people as cap
    from app.graph import signals
    from app.graph.models import SignalRecord

    seen: dict = {}

    def fake_extract(kind, title, summary):
        return [cap.PersonMention("Jane Placeholder", cap.QUOTED_IN, linkedin=NONCANON)]

    async def fake_candidates(driver, *, name, company, linkedin_canon):
        seen["canon"] = linkedin_canon
        return {"linkedin_eids": [], "name_company_eids": []}

    async def fake_write(driver, url, resolved, source=None):
        return {"linked": 0, "flagged": len(resolved)}

    monkeypatch.setattr(cap, "extract_people", fake_extract)
    monkeypatch.setattr(signals, "person_signal_candidates", fake_candidates)
    monkeypatch.setattr(signals, "write_person_signal_links", fake_write)

    record = SignalRecord(url="https://news.example/story", title="A talk", summary="…")
    asyncio.run(cap.link_signal_people(None, record, "Acme"))
    assert seen["canon"] == CANON


# --- Graph: producer -> write path -> canonical node -------------------------


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _neo4j_available() -> bool:
    try:
        await check_connectivity()
        return True
    except Exception:
        return False


CO = "Nebula Test Co __pytest183__"


async def _cleanup(session) -> None:
    await session.run("MATCH (c:Company) WHERE c.name = $name DETACH DELETE c", name=CO)
    await session.run(
        "MATCH (p:Person) WHERE p.linkedin CONTAINS 'pytest183' OR p.name = 'Jane Placeholder' "
        "DETACH DELETE p"
    )


def test_company_producer_lands_canonical_leader(event_loop):
    """Enrichment-agent / importer path: a Leader carrying a NON-canonical URL is
    keyed on the canonical URL in the graph (validator did the work upstream)."""
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        await upsert_company(
            driver,
            CompanyRecord(
                name=CO,
                leadership=[Leader(name="Jane Placeholder", title="CEO", linkedin=NONCANON)],
            ),
        )

        async with driver.session() as session:
            res = await session.run(
                "MATCH (:Company {name: $co})<-[:LEADS]-(p:Person) RETURN p.linkedin AS linkedin",
                co=CO,
            )
            row = await res.single()
            await _cleanup(session)
        await close_driver()
        return row

    row = event_loop.run_until_complete(scenario())
    assert row is not None
    assert row["linkedin"] == CANON  # non-canonical in, canonical on the node


def test_person_producer_lands_canonical_identity(event_loop):
    """People-agent commit path: a PersonRecord carrying a NON-canonical URL keys
    the :Person on the canonical URL via ``upsert_person``."""
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        # Seed a name-only leader (as company enrichment would have), then commit a
        # person record whose LinkedIn arrives non-canonical.
        await upsert_company(
            driver,
            CompanyRecord(name=CO, leadership=[Leader(name="Jane Placeholder", title="Chief")]),
        )
        record = PersonRecord(
            name="Jane Placeholder",
            company=CO,
            title="CEO",
            linkedin=NONCANON,
            citations=[PersonCitation(field="linkedin", value=CANON, source="https://co.example")],
        )
        action = await upsert_person(driver, record)

        async with driver.session() as session:
            res = await session.run(
                "MATCH (p:Person {linkedin: $canon})-[:LEADS]->(:Company {name: $co}) "
                "RETURN count(p) AS n",
                canon=CANON,
                co=CO,
            )
            n = (await res.single())["n"]
            await _cleanup(session)
        await close_driver()
        return action, n

    action, n = event_loop.run_until_complete(scenario())
    assert action["action"] == "written"
    assert n == 1  # the name-only leader is now keyed on the canonical URL
