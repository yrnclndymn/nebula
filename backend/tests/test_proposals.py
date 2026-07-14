"""Safety test: in propose mode, save_company captures but must NOT write."""

import asyncio
import json

import pytest

from app.agents.assistant import proposals
from app.graph import jobs
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.tools.graph_tools import proposal_sink, save_company

NAME = "__pytest_propose__ Co"


def test_propose_captures_without_writing():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"

        sink: list = []
        token = proposal_sink.set(sink)
        try:
            result = await save_company(
                name=NAME,
                topic="AI-native engineering",
                about="a test company",
                website="",
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
        finally:
            proposal_sink.reset(token)

        async with get_driver().session() as session:
            r = await session.run("MATCH (c:Company {name: $n}) RETURN count(c) AS n", n=NAME)
            count = (await r.single())["n"]
        await close_driver()
        return result, sink, count

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    _result, sink, count = out
    assert len(sink) == 1 and sink[0]["name"] == NAME  # captured for review
    assert count == 0  # the invariant that matters: nothing written to the graph


# --- Scope-aware supersede of stale errored proposals (issue #102) ------------
# A backlog proposal errors, a retry succeeds and is committed, yet on the next
# panel mount the old error card returned: the errored :Job was never superseded.
# The fix marks older errored proposals superseded on propose — but scope-aware,
# so a focused success never clears a full-enrichment error.


def test_supersedes_error_scope_rule():
    """The predicate is pure (no DB): a full attempt clears any older error; a
    focused attempt clears only an older error at the SAME field, never a full one."""
    s = proposals._supersedes_error
    # A full attempt (None focus) supersedes any older errored proposal.
    assert s(None, None) is True
    assert s(None, "headcount") is True
    assert s(None, "hq_location") is True
    # A focused attempt supersedes only an older error at the SAME field...
    assert s("headcount", "headcount") is True
    assert s("headcount", "hq_location") is False
    # ...and never a full-enrichment error (the company still isn't researched).
    assert s("headcount", None) is False


SUP_PREFIX = "__pytest_supersede__"


def _seed_error(session, job_id: str, name: str, focus: str):
    return session.run(
        "CREATE (j:Job {id: $id, type: 'proposal', status: 'error', "
        "dataJson: $data, createdAt: datetime()})",
        id=job_id,
        data=json.dumps({"name": name, "focus": focus, "error": "boom"}),
    )


def _seed_committed(session, job_id: str, name: str, focus: str):
    # A committed proposal keeps node status "ready" on purpose (two-step commit).
    return session.run(
        "CREATE (j:Job {id: $id, type: 'proposal', status: 'ready', "
        "dataJson: $data, createdAt: datetime()})",
        id=job_id,
        data=json.dumps(
            {
                "name": name,
                "focus": focus,
                "focus_key": focus or None,
                "committed": True,
                "record": {"name": name},
            }
        ),
    )


def _run_supersede_scenario(seed, name, new_focus_key):
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        async with driver.session() as session:
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=SUP_PREFIX
            )
            await seed(session)
        await proposals._supersede_errored_proposals(name, new_focus_key)
        listed = await jobs.list_jobs(driver, type="proposal")
        async with driver.session() as session:
            res = await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p "
                "RETURN j.id AS id, coalesce(j.superseded, false) AS superseded",
                p=SUP_PREFIX,
            )
            flags = {rec["id"]: rec["superseded"] async for rec in res}
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=SUP_PREFIX
            )
        await close_driver()
        return listed, flags

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    return out


def test_full_retry_supersedes_old_error_and_listing_drops_it():
    """Regression: error → retry (full) → commit → the rehydrated listing shows
    NO error for that name. The committed retry stays; the errored job is
    superseded (kept as history) and excluded from the listing."""
    err_id = f"{SUP_PREFIX}_err"
    ok_id = f"{SUP_PREFIX}_ok"

    async def seed(session):
        await _seed_error(session, err_id, "Acme __pytest__", "")
        await _seed_committed(session, ok_id, "Acme __pytest__", "")

    listed, flags = _run_supersede_scenario(seed, "Acme __pytest__", None)

    assert flags[err_id] is True  # errored job marked superseded (node kept)
    assert flags[ok_id] is False  # the committed retry is NOT touched (HITL)
    ids = {j["id"] for j in listed}
    assert err_id not in ids  # listing excludes the superseded error...
    assert ok_id in ids  # ...but still carries the successful attempt
    assert not any(j["status"] == "error" for j in listed if j["id"].startswith(SUP_PREFIX))


def test_focused_success_keeps_full_enrichment_error_visible():
    """Regression: a focused success (e.g. just headcount) after a full-enrichment
    error must NOT clear that error — the company still hasn't been researched."""
    err_id = f"{SUP_PREFIX}_fullerr"

    async def seed(session):
        await _seed_error(session, err_id, "Globex __pytest__", "")  # full error

    listed, flags = _run_supersede_scenario(seed, "Globex __pytest__", "headcount")

    assert flags[err_id] is False  # focused attempt leaves the full error alone
    assert err_id in {j["id"] for j in listed}  # still visible in the listing


def test_focused_retry_supersedes_same_field_error_only():
    """A focused retry clears an older error at the SAME field, but leaves an error
    at a different field (and the whole thing is per-name)."""
    same_id = f"{SUP_PREFIX}_hc"
    other_id = f"{SUP_PREFIX}_hq"

    async def seed(session):
        await _seed_error(session, same_id, "Initech __pytest__", "headcount")
        await _seed_error(session, other_id, "Initech __pytest__", "hq")

    listed, flags = _run_supersede_scenario(seed, "Initech __pytest__", "headcount")

    assert flags[same_id] is True  # same focused field → superseded
    assert flags[other_id] is False  # different field → left visible
    ids = {j["id"] for j in listed}
    assert same_id not in ids and other_id in ids


def test_supersede_skips_other_names():
    # An errored proposal for a DIFFERENT company is never touched (covers the
    # name-mismatch continue — the diff-coverage gate flagged it untested).
    err_id = f"{SUP_PREFIX}other-name"

    async def seed(session):
        await _seed_error(session, err_id, "Hooli __pytest__", "")

    listed, flags = _run_supersede_scenario(seed, "Vandelay __pytest__", None)
    assert flags[err_id] is False  # untouched: different name
    assert err_id in {j["id"] for j in listed}  # still listed (not superseded)


def test_propose_survives_supersede_failure(monkeypatch):
    # The supersede step is best-effort housekeeping: if it raises, the propose
    # call must still succeed with the fresh proposal id (covers the except
    # branch + the focus_key resolution in propose_enrichment; DB-free).
    created = {}

    async def fake_create_job(job_id, jtype, data):
        created.update(data)

    async def fake_enqueue(job_id, delay=0.0):
        created["enqueued"] = True

    async def boom(name, focus_key):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(proposals.jobs, "create_job", fake_create_job)
    monkeypatch.setattr(proposals.jobs, "enqueue", fake_enqueue)
    monkeypatch.setattr(proposals, "_supersede_errored_proposals", boom)

    out = asyncio.run(
        proposals.propose_enrichment("Acme __pytest__", "acme.example", focus="headcount")
    )

    assert out["proposal_id"] == created["proposal_id"]
    assert created["focus_key"] == "headcount"
    assert created["enqueued"] is True
