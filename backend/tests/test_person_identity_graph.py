"""Graph integration tests for Person re-keying on LinkedIn URL (#39).

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

All fixtures use fictional people and `linkedin.com/in/fictional-slug` URLs
(public-repo rule).
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord, Leader
from app.graph.person_identity import attach_linkedin, migrate_person_identity
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

CO_A = "Nebula Test Co A __pytest39__"
CO_B = "Nebula Test Co B __pytest39__"
CO_C = "Nebula Test Co C __pytest39__"
SLUG = "jane-placeholder-pytest39"
CANON = f"https://www.linkedin.com/in/{SLUG}"
NAME_ONLY = "John Standin __pytest39__"


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


async def _cleanup(session) -> None:
    await session.run(
        "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=[CO_A, CO_B, CO_C]
    )
    await session.run(
        "MATCH (p:Person) WHERE p.linkedin CONTAINS $slug OR p.name IN [$name, 'Jane Placeholder'] DETACH DELETE p",
        slug=SLUG,
        name=NAME_ONLY,
    )


def test_write_path_keys_person_on_linkedin(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        # Two companies list the "same" leader under different name spellings and
        # non-canonical LinkedIn URLs. Keying on the canonical URL must collapse
        # them to ONE Person with both LEADS edges — name variance no longer splits.
        await upsert_company(
            driver,
            CompanyRecord(
                name=CO_A,
                leadership=[Leader(name="Jane Placeholder", title="CEO", linkedin=CANON)],
            ),
        )
        await upsert_company(
            driver,
            CompanyRecord(
                name=CO_B,
                leadership=[
                    Leader(
                        name="J. Placeholder",
                        title="Founder",
                        # trailing slash + country subdomain + case -> same identity
                        linkedin=f"https://uk.linkedin.com/in/{SLUG.title()}/",
                    ),
                    # A leader with NO linkedin still keys by name (fallback path).
                    Leader(name=NAME_ONLY, title="CTO"),
                ],
            ),
        )

        async with driver.session() as session:
            res = await session.run(
                "MATCH (p:Person {linkedin: $canon}) "
                "OPTIONAL MATCH (p)-[:LEADS]->(c:Company) "
                "RETURN p.name AS name, count(c) AS leads",
                canon=CANON,
            )
            keyed = await res.single()
            res = await session.run(
                "MATCH (p:Person {name: $name}) RETURN p.linkedin AS linkedin", name=NAME_ONLY
            )
            fallback = await res.single()
            await _cleanup(session)
        await close_driver()
        return keyed, fallback

    keyed, fallback = event_loop.run_until_complete(scenario())
    assert keyed is not None
    assert keyed["leads"] == 2  # one Person, two companies led
    assert keyed["name"] == "Jane Placeholder"  # first-seen display name preserved
    assert fallback is not None
    assert fallback["linkedin"] is None  # name-only leader unaffected


def test_migration_merges_same_url_duplicates(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
            # Two legacy Person nodes for the same human: variant raw URLs (the
            # unique constraint on the raw value doesn't catch these) each leading
            # a different company.
            await session.run(
                """
                CREATE (a:Person {name: 'Jane Placeholder', linkedin: $u1})-[:LEADS {title:'CEO'}]->(:Company {name:$coA})
                CREATE (b:Person {name: 'J. Placeholder', linkedin: $u2})-[:LEADS {title:'Advisor'}]->(:Company {name:$coB})
                """,
                u1=f"https://www.linkedin.com/in/{SLUG}",
                u2=f"https://UK.linkedin.com/in/{SLUG.title()}/",
                coA=CO_A,
                coB=CO_B,
            )

        report = await migrate_person_identity(driver, dry_run=False)

        async with driver.session() as session:
            res = await session.run(
                "MATCH (p:Person) WHERE p.linkedin CONTAINS $slug "
                "OPTIONAL MATCH (p)-[:LEADS]->(c:Company) "
                "RETURN count(DISTINCT p) AS people, count(DISTINCT c) AS companies, "
                "collect(DISTINCT p.linkedin) AS urls",
                slug=SLUG,
            )
            after = await res.single()

        # Re-running must be a no-op (idempotent).
        rerun = await migrate_person_identity(driver, dry_run=False)

        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return report, dict(after), rerun

    report, after, rerun = event_loop.run_until_complete(scenario())
    assert len(report["merges"]) == 1
    assert after["people"] == 1  # collapsed to a single node
    assert after["companies"] == 2  # both LEADS edges preserved
    assert after["urls"] == [CANON]  # canonical form stored
    assert rerun["merges"] == []  # idempotent second run


def test_attach_linkedin_reviewable_set_and_merge(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
            # A name-only leader (no URL yet) plus an already-keyed node for the
            # SAME human at a different company — attaching should dedup onto the
            # keyed node, not create a rival. A NAMESAKE (different human, same
            # name) leading an unrelated company must survive untouched: the
            # evidence is scoped to coA (#87 review).
            await session.run(
                """
                CREATE (:Person {name: 'Jane Placeholder'})-[:LEADS {title:'CEO'}]->(:Company {name:$coA})
                CREATE (:Person {name: 'Jane Placeholder', linkedin:$canon})-[:LEADS {title:'Chair'}]->(:Company {name:$coB})
                CREATE (:Person {name: 'Jane Placeholder'})-[:LEADS {title:'CTO'}]->(:Company {name:$coC})
                """,
                coA=CO_A,
                coB=CO_B,
                coC=CO_C,
                canon=CANON,
            )

        dry = await attach_linkedin(driver, "Jane Placeholder", CANON, company=CO_A, dry_run=True)
        async with driver.session() as session:
            res = await session.run(
                "MATCH (p:Person {name:'Jane Placeholder'}) RETURN count(p) AS n"
            )
            after_dry = (await res.single())["n"]

        await attach_linkedin(driver, "Jane Placeholder", CANON, company=CO_A, dry_run=False)
        async with driver.session() as session:
            res = await session.run(
                "MATCH (p:Person {name:'Jane Placeholder'}) "
                "OPTIONAL MATCH (p)-[:LEADS]->(c:Company) "
                "RETURN count(DISTINCT p) AS people, count(DISTINCT c) AS companies"
            )
            after = dict(await res.single())
            res = await session.run(
                "MATCH (p:Person {name:'Jane Placeholder'})-[:LEADS]->(:Company {name:$coC}) "
                "RETURN p.linkedin AS url",
                coC=CO_C,
            )
            namesake = await res.single()
            await _cleanup(session)
        await close_driver()
        return dry, after_dry, after, namesake

    dry, after_dry, after, namesake = event_loop.run_until_complete(scenario())
    assert dry["action"] == "merged" and dry["dry_run"] is True
    assert after_dry == 3  # dry-run wrote nothing
    assert after["people"] == 2  # coA leader folded onto the keyed node; namesake apart
    assert namesake is not None and namesake["url"] is None  # untouched, still name-only
    assert after["companies"] == 3  # keyed node leads A+B; namesake still leads C
