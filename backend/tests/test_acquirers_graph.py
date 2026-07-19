"""Graph integration for potential-acquirer analysis (#44).

Seeds a small M&A graph — a target company, candidate acquirers with ACQUIRED
history of same-topic / same-kind companies, and partner/client overlap — then
checks the ranked candidates and the space-level most-active view read back
correctly through the Cypher gatherers.

Skips when Neo4j is unreachable so `make test` stays green without a database; CI
(with its own Neo4j service) is the arbiter. Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`. Fictional names only.
"""

import asyncio

import pytest

from app.graph.acquirers import most_active_acquirers, potential_acquirers
from app.graph.acquisitions import upsert_acquisitions
from app.graph.deal_models import AcquisitionRecord, Deal
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema
from app.graph.thesis import ThesisRule, upsert_thesis_rule

TOPIC = "__pytest44_topic__"

# A test-only thesis rule (#194): unqualified service_provider -> isv so its ruleKey
# ("service_provider>isv") never collides with the seed's *qualified* domain-focused
# variant, keeping cleanup from clobbering a real seeded rule. Exercises the gatherer's
# new acquirer_kind column + the once-per-ranking get_thesis_rules threading; the
# qualifier composition itself is covered exhaustively in the DB-free test_acquirers.py.
THESIS_STMT = "__pytest194__ services acquire ISVs."
THESIS_RULE = ThesisRule(
    acquirer_kind="service_provider",
    target_kind="isv",
    statement=THESIS_STMT,
    confidence=0.6,
    origin="user",
)
SRC = "https://news.example/pytest44"

TARGET = "Target Co __pytest44__"
# Candidates:
ACTIVE = "Active Acquirer __pytest44__"  # acquired 2 same-topic cos + shares a partner
KINDLY = "Kindly Acquirer __pytest44__"  # acquired a same-kind (not same-topic) co
PARTNER = "Partner Acquirer __pytest44__"  # directly partners with the target, 1 deal
UNRELATED = "Unrelated Acquirer __pytest44__"  # acquires, but nothing ties to target

# Acquired targets / neighbours (stubs seeded as researched where needed).
INTOPIC_1 = "In Topic One __pytest44__"
INTOPIC_2 = "In Topic Two __pytest44__"
SAMEKIND = "Same Kind Co __pytest44__"
OFFTOPIC = "Off Topic Co __pytest44__"
SHARED_P = "Shared Partner __pytest44__"

ALL_NAMES = [
    TARGET,
    ACTIVE,
    KINDLY,
    PARTNER,
    UNRELATED,
    INTOPIC_1,
    INTOPIC_2,
    SAMEKIND,
    OFFTOPIC,
    SHARED_P,
]


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
    await session.run("MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=ALL_NAMES)
    await session.run("MATCH (t:Topic {name: $t}) DETACH DELETE t", t=TOPIC)
    await session.run(
        "MATCH (tr:ThesisRule {ruleKey: $key}) DETACH DELETE tr", key=THESIS_RULE.rule_key
    )


async def _seed(driver) -> None:
    # Target: an isv tagged with TOPIC, partnered with SHARED_P, headcount 100 (so the
    # #165 size signals have a target side to compare against).
    await upsert_company(
        driver,
        CompanyRecord(
            name=TARGET, kind="isv", topics=[TOPIC], partnerships=[SHARED_P], headcount=100
        ),
    )
    # In-topic acquired companies (researched: TAGGED_AS TOPIC). Headcounts 80/120
    # bracket the target's 100 -> ACTIVE's historical range is a size-fit.
    for n, hc in ((INTOPIC_1, 80), (INTOPIC_2, 120)):
        await upsert_company(
            driver, CompanyRecord(name=n, kind="isv", topics=[TOPIC], headcount=hc)
        )
    # Same-kind but off-topic acquired company.
    await upsert_company(driver, CompanyRecord(name=SAMEKIND, kind="isv"))
    await upsert_company(driver, CompanyRecord(name=OFFTOPIC, kind="service_provider"))
    # ACTIVE also partners with SHARED_P (overlap with target); 5000 people -> >=3x the
    # target, a size-plausible ("larger") acquirer. Kind service_provider so the #194
    # thesis rule (service_provider -> isv, the target's kind) matches it.
    await upsert_company(
        driver,
        CompanyRecord(
            name=ACTIVE, kind="service_provider", partnerships=[SHARED_P], headcount=5000
        ),
    )
    # Seed the test thesis rule so get_thesis_rules returns it for the ranking.
    await upsert_thesis_rule(driver, THESIS_RULE)
    await upsert_company(driver, CompanyRecord(name=PARTNER, partnerships=[TARGET]))

    deals = [
        # ACTIVE acquired two same-topic companies -> strongest. One carries a cited
        # amount (amount_source set) so past_target_amounts surfaces in size-fit.
        Deal(
            acquirer=ACTIVE,
            target=INTOPIC_1,
            source=SRC,
            announced_at="2024-01-01",
            amount="$100M",
            currency="USD",
            amount_source=SRC,
        ),
        Deal(acquirer=ACTIVE, target=INTOPIC_2, source=SRC, announced_at="2024-06-01"),
        # KINDLY acquired a same-kind (off-topic) company.
        Deal(acquirer=KINDLY, target=SAMEKIND, source=SRC, announced_at="2023-01-01"),
        # PARTNER acquired an off-topic, off-kind company but partners with the target.
        Deal(acquirer=PARTNER, target=OFFTOPIC, source=SRC, announced_at="2022-01-01"),
        # UNRELATED acquired an off-topic, off-kind company; no tie to the target.
        Deal(acquirer=UNRELATED, target=OFFTOPIC, source=SRC, announced_at="2021-01-01"),
    ]
    await upsert_acquisitions(driver, AcquisitionRecord(company=TARGET, deals=deals))


def test_potential_acquirers_ranks_by_signal(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
        await _seed(driver)
        ranked = await potential_acquirers(driver, TARGET)
        # limit is threaded into the pure ranker: 3 candidates tie to the target, so a
        # limit of 1 must return exactly one (guards the wrapper passing limit through).
        limited = await potential_acquirers(driver, TARGET, limit=1)
        missing = await potential_acquirers(driver, "No Such Co __pytest44__")
        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return ranked, limited, missing

    ranked, limited, missing = event_loop.run_until_complete(scenario())
    assert missing is None  # unknown company -> None (route 404s)
    assert len(limited) == 1  # limit threaded through to the ranker

    by_name = {r["acquirer"] for r in ranked}
    # ACTIVE / KINDLY / PARTNER all tie to the target; UNRELATED does not.
    assert {ACTIVE, KINDLY, PARTNER} <= by_name
    assert UNRELATED not in by_name

    active = next(r for r in ranked if r["acquirer"] == ACTIVE)
    signals = {w["signal"] for w in active["why"]}
    assert "acquired-in-topic" in signals and "shared-partners" in signals
    topic_reason = next(w for w in active["why"] if w["signal"] == "acquired-in-topic")
    assert topic_reason["detail"]["count"] == 2
    assert all(d["source"] == SRC for d in topic_reason["detail"]["deals"])

    # #165 size facts gathered in the same single query: ACTIVE is >=3x the target and
    # its past targets (80/120) bracket the target's 100 headcount, with a cited amount.
    assert "size-plausible" in signals and "size-fit" in signals
    plausible = next(w for w in active["why"] if w["signal"] == "size-plausible")
    assert plausible["detail"]["direction"] == "larger"
    assert plausible["detail"]["acquirer_headcount"] == 5000
    assert plausible["detail"]["target_headcount"] == 100
    fit = next(w for w in active["why"] if w["signal"] == "size-fit")
    assert (fit["detail"]["low"], fit["detail"]["high"], fit["detail"]["n"]) == (80, 120, 2)
    assert fit["detail"]["amounts"] == ["$100M"]

    # #194 thesis match gathered off the same single query (ACTIVE is a service_provider,
    # the target an isv): the once-fetched rule matched and cited its statement. Tolerant
    # of any additional seed rules also present in a shared DB.
    assert "thesis-match" in signals
    thesis = next(
        w
        for w in active["why"]
        if w["signal"] == "thesis-match" and w["detail"]["statement"] == THESIS_STMT
    )
    assert thesis["detail"]["acquirer_kind"] == "service_provider"
    assert thesis["detail"]["target_kind"] == "isv"
    assert thesis["detail"]["confidence"] == 0.6
    assert thesis["detail"]["evidence"] == 0  # freshly seeded, no SUPPORTED_BY yet

    kindly = next(r for r in ranked if r["acquirer"] == KINDLY)
    assert {w["signal"] for w in kindly["why"]} == {"acquired-same-kind"}

    partner = next(r for r in ranked if r["acquirer"] == PARTNER)
    assert "direct-partner" in {w["signal"] for w in partner["why"]}

    # ACTIVE (2 topic deals + shared partner) outranks the single-signal candidates.
    assert ranked[0]["acquirer"] == ACTIVE


def test_most_active_acquirers_counts_and_topic_filter(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
        await _seed(driver)
        overall = await most_active_acquirers(driver, limit=50)
        in_topic = await most_active_acquirers(driver, topic=TOPIC, limit=50)
        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return overall, in_topic

    overall, in_topic = event_loop.run_until_complete(scenario())

    overall_by = {r["acquirer"]: r for r in overall}
    assert overall_by[ACTIVE]["deal_count"] == 2
    # Recent deals carry sources and are ordered announced-date desc.
    recent = overall_by[ACTIVE]["recent_deals"]
    assert [d["target"] for d in recent] == [INTOPIC_2, INTOPIC_1]
    assert all(d["source"] == SRC for d in recent)

    # Topic filter keeps only acquirers whose deal targets are tagged with TOPIC.
    topic_names = {r["acquirer"] for r in in_topic}
    assert ACTIVE in topic_names
    assert KINDLY not in topic_names and PARTNER not in topic_names
