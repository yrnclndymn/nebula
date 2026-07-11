"""Client-kind classification: the model decision, the PATCH validation, the
client-stub heuristic query, and the commit mutation.

The pure/validation checks run anywhere; the graph checks skip without Neo4j
(like the rest of the graph layer). Fictional fixture names only (Acme, Globex…).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

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


def test_classify_as_client_sets_kind_and_skips_promoted():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        stub = "Acme __clcommit__"
        promoted = "Globex __clcommit__"  # promoted (has website) since the scan
        names = [stub, promoted]
        async with d.session() as s:
            await s.run("MATCH (c:Company) WHERE c.name IN $n DETACH DELETE c", n=names)
            await s.run(
                "MERGE (a:Company {name:$a}) "
                "MERGE (b:Company {name:$b}) SET b.website='https://b.example'",
                a=stub,
                b=promoted,
            )
        # Approve both, but the promoted one must be skipped (still gets no kind).
        classified = await er.classify_as_client(d, [stub, promoted, "No Such Co __clcommit__"])
        async with d.session() as s:
            r = await s.run(
                "MATCH (c:Company) WHERE c.name IN $n RETURN c.name AS name, c.kind AS kind",
                n=names,
            )
            kinds = {rec["name"]: rec["kind"] async for rec in r}
            await s.run("MATCH (c:Company) WHERE c.name IN $n DETACH DELETE c", n=names)
        await close_driver()
        return classified, kinds

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    classified, kinds = out
    assert classified == 1  # only the true stub; promoted + unknown skipped
    assert kinds["Acme __clcommit__"] == "client"
    assert kinds["Globex __clcommit__"] is None  # promoted node was not mislabelled
