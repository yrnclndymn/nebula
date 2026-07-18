"""Company classification: the model decision, the PATCH validation, the pure
per-name decision validation, the client-stub heuristic query, and the
kind/remove commit mutations.

The pure/validation checks run anywhere; the graph checks skip without Neo4j
(like the rest of the graph layer). Fictional fixture names only (Acme, Globex…).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agents.assistant import classification as cls
from app.graph import entity_resolution as er
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import APPLIES_TO, KINDS
from app.main import app

# --- Model decision (pure) ---------------------------------------------------


def test_client_is_a_valid_kind():
    assert "client" in KINDS


def test_client_is_not_a_custom_field_target():
    # DELIBERATE DECISION: custom research fields are ecosystem-only. A field may
    # target an ecosystem kind or "all", never "client" (end-customers aren't
    # researched, so a client-scoped field would only make dead columns).
    assert "client" not in APPLIES_TO
    for k in ("service_provider", "isv", "cloud_provider", "all"):
        assert k in APPLIES_TO


# --- Per-name decision validation (pure) -------------------------------------


def test_valid_actions_are_every_kind_plus_remove():
    assert frozenset(KINDS) | {"remove"} == cls.VALID_ACTIONS
    for k in KINDS:
        assert k in cls.VALID_ACTIONS


def test_suggested_action_flags_junk_for_removal_else_client():
    # A junk-looking name (extraction noise) pre-suggests 'remove'.
    assert cls.suggested_action("read more") == "remove"
    assert cls.suggested_action("   ") == "remove"
    # A plausible org name pre-suggests 'client' (the common end-customer case).
    assert cls.suggested_action("Acme") == "client"
    assert cls.suggested_action("Globex Bank") == "client"


def test_partition_decisions_splits_kinds_removes_and_rejects_invalid():
    kinds, removes, invalid = cls.partition_decisions(
        [
            {"name": "Acme", "action": "client"},
            {"name": "Globex", "action": "cloud_provider"},
            {"name": "Initech", "action": "remove"},
            {"name": "Hooli", "action": "nonsense"},  # unknown action → invalid
            {"name": "", "action": "client"},  # missing name → invalid
            {"action": "client"},  # no name key → invalid
        ]
    )
    assert kinds == [("Acme", "client"), ("Globex", "cloud_provider")]
    assert removes == ["Initech"]
    assert len(invalid) == 3


def test_partition_decisions_trims_names_and_tolerates_empty():
    kinds, removes, invalid = cls.partition_decisions([{"name": "  Acme  ", "action": "isv"}])
    assert kinds == [("Acme", "isv")]
    assert removes == [] and invalid == []
    assert cls.partition_decisions([]) == ([], [], [])


# --- kind PATCH validation (endpoint; no DB needed to reach validation) ------


def test_kind_validation_rejects_unknown():
    with TestClient(app) as client:
        resp = client.patch("/companies/Acme/kind", json={"kind": "nonsense"})
    assert resp.status_code == 422


def test_kind_validation_accepts_client():
    # 'client' passes KINDS validation and is not rejected as 422. (Without a DB
    # the downstream write fails; we only assert validation let it through.)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.patch("/companies/Acme/kind", json={"kind": "client"})
    assert resp.status_code != 422


# --- Client-stub heuristic query (needs Neo4j) -------------------------------

SRC = "Source Co __cltest__"  # a company that HAS_CLIENT edges point out from
CANDIDATE = "Acme __cltest__"  # only inbound HAS_CLIENT → should be proposed
PARTNERED = "Globex __cltest__"  # also has an outbound partnership → excluded
HAS_WEBSITE = "Initech __cltest__"  # has a website → excluded
DUAL_ROLE = "Hooli __cltest__"  # already a cloud_provider → excluded (keeps kind)
HAS_OWN_CLIENT = "Umbrella __cltest__"  # has a client of its own → excluded

_ALL = [SRC, CANDIDATE, PARTNERED, HAS_WEBSITE, DUAL_ROLE, HAS_OWN_CLIENT, "Vehement __cltest__"]


def test_client_heuristic_proposes_only_inbound_client_stubs():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        async with d.session() as s:
            await s.run("MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=_ALL)
            await s.run(
                """
                MERGE (src:Company {name:$src}) SET src.website='https://src.example'
                MERGE (cand:Company {name:$cand})
                MERGE (part:Company {name:$part})
                MERGE (web:Company {name:$web}) SET web.website='https://web.example'
                MERGE (dual:Company {name:$dual}) SET dual.kind='cloud_provider'
                MERGE (own:Company {name:$own})
                MERGE (owned:Company {name:$owned})
                // Every excluded/candidate stub is at least someone's client.
                MERGE (src)-[:HAS_CLIENT]->(cand)
                MERGE (src)-[:HAS_CLIENT]->(part)
                MERGE (src)-[:HAS_CLIENT]->(web)
                MERGE (src)-[:HAS_CLIENT]->(dual)
                MERGE (src)-[:HAS_CLIENT]->(own)
                // …but the excluded ones carry extra signal:
                MERGE (part)-[:PARTNERS_WITH]->(src)   // partnered → not a pure client
                MERGE (own)-[:HAS_CLIENT]->(owned)     // has a client of its own
                """,
                src=SRC,
                cand=CANDIDATE,
                part=PARTNERED,
                web=HAS_WEBSITE,
                dual=DUAL_ROLE,
                own=HAS_OWN_CLIENT,
                owned="Vehement __cltest__",
            )
        candidates = await er.list_client_stub_candidates(d)
        async with d.session() as s:
            await s.run("MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=_ALL)
        await close_driver()
        return candidates

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    names = {c["name"] for c in out}
    assert CANDIDATE in names  # only inbound HAS_CLIENT, no other signal
    assert PARTNERED not in names  # nobody's partner is required
    assert HAS_WEBSITE not in names  # a website means it's been researched
    assert DUAL_ROLE not in names  # genuine dual-role keeps its ecosystem kind
    assert HAS_OWN_CLIENT not in names  # has a client of its own
    assert SRC not in names  # the source has a website (and no inbound client edge)
    # The proposed candidate reports its inbound HAS_CLIENT count.
    cand = next(c for c in out if c["name"] == CANDIDATE)
    assert cand["inbound"] == 1


def test_remove_stub_companies_deletes_stubs_and_refuses_researched():
    """The stub-only guard: 'remove' HARD-deletes true stubs (no website, no topic)
    but REFUSES researched companies (a website or a topic tag) even when the
    reviewer approved them — a mistaken removal must never destroy real data."""

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        stub = "Acme __rmtest__"  # bare stub → removable
        has_web = "Globex __rmtest__"  # has a website → researched → refused
        has_topic = "Initech __rmtest__"  # tagged to a topic → researched → refused
        topic = "Widgets __rmtest__"
        names = [stub, has_web, has_topic]
        async with d.session() as s:
            await s.run("MATCH (c:Company) WHERE c.name IN $n DETACH DELETE c", n=names)
            await s.run("MATCH (t:Topic {name:$t}) DETACH DELETE t", t=topic)
            await s.run(
                "MERGE (a:Company {name:$a}) "
                "MERGE (b:Company {name:$b}) SET b.website='https://b.example' "
                "MERGE (c:Company {name:$c}) "
                "MERGE (t:Topic {name:$t}) "
                "MERGE (c)-[:TAGGED_AS]->(t)",
                a=stub,
                b=has_web,
                c=has_topic,
                t=topic,
            )
        # Approve all three for removal (plus an unknown name); only the true stub
        # may actually be deleted.
        removed, refused = await er.remove_stub_companies(
            d, [stub, has_web, has_topic, "No Such Co __rmtest__"]
        )
        async with d.session() as s:
            r = await s.run("MATCH (c:Company) WHERE c.name IN $n RETURN c.name AS name", n=names)
            surviving = {rec["name"] async for rec in r}
            await s.run("MATCH (c:Company) WHERE c.name IN $n DETACH DELETE c", n=names)
            await s.run("MATCH (t:Topic {name:$t}) DETACH DELETE t", t=topic)
        await close_driver()
        return removed, refused, surviving

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    removed, refused, surviving = out
    assert removed == ["Acme __rmtest__"]  # only the true stub was deleted
    assert set(refused) == {"Globex __rmtest__", "Initech __rmtest__"}  # researched refused
    assert "Acme __rmtest__" not in surviving  # stub is gone
    assert surviving == {"Globex __rmtest__", "Initech __rmtest__"}  # researched kept intact
