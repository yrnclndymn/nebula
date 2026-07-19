"""Topic guard for chat enrichment (issue #148).

`propose_enrichment` must not let a company's research `topic` be coined as a
side effect of the user's request phrasing. A topic that is neither the default
nor an existing graph Topic requires explicit confirmation before a job is
created. These tests are DB-free: the graph reads and job writes are monkeypatched.
"""

import asyncio

from app.agents.assistant import proposals


def _stub_jobs(monkeypatch, created: dict):
    """Capture create_job/enqueue instead of touching the graph, and neutralise
    the best-effort supersede step so the flow stays DB-free."""

    async def fake_create_job(job_id, jtype, data):
        created["job"] = data

    async def fake_enqueue(job_id, delay=0.0):
        created["enqueued"] = True

    async def no_supersede(name, focus_key):
        return None

    monkeypatch.setattr(proposals.jobs, "create_job", fake_create_job)
    monkeypatch.setattr(proposals.jobs, "enqueue", fake_enqueue)
    monkeypatch.setattr(proposals, "_supersede_errored_proposals", no_supersede)


def _stub_topics(monkeypatch, topics):
    async def fake_list_topics(driver):
        return list(topics)

    monkeypatch.setattr(proposals.queries, "list_topics", fake_list_topics)


def test_novel_topic_needs_confirmation_and_creates_no_job(monkeypatch):
    # A topic that is neither the default nor an existing graph topic must not
    # silently become a Topic node: the tool asks the model to confirm and no
    # background job is created.
    created: dict = {}
    _stub_jobs(monkeypatch, created)
    _stub_topics(monkeypatch, ["AI-native engineering", "SAP ecosystem"])

    out = asyncio.run(
        proposals.propose_enrichment(
            "Acme __pytest__", "acme.example", topic="ISV classification update"
        )
    )

    assert out.get("needs_topic_confirmation") is True
    assert out["status"] == "needs_confirmation"
    assert "job" not in created  # nothing enqueued
    assert "ISV classification update" in out["topic"]


def test_confirmed_new_topic_proceeds(monkeypatch):
    # New topics stay possible — a second call with confirm_new_topic=True after
    # the user has agreed creates the job with the new topic.
    created: dict = {}
    _stub_jobs(monkeypatch, created)
    _stub_topics(monkeypatch, ["AI-native engineering"])

    out = asyncio.run(
        proposals.propose_enrichment(
            "Acme __pytest__",
            "acme.example",
            topic="Developer tooling",
            confirm_new_topic=True,
        )
    )

    assert out["status"] == "researching in the background"
    assert created["job"]["topic"] == "Developer tooling"
    assert created["enqueued"] is True


def test_existing_topic_never_confirms(monkeypatch):
    # A topic that already exists in the graph proceeds without confirmation.
    created: dict = {}
    _stub_jobs(monkeypatch, created)
    _stub_topics(monkeypatch, ["AI-native engineering", "SAP ecosystem"])

    out = asyncio.run(
        proposals.propose_enrichment("Acme __pytest__", "acme.example", topic="SAP ecosystem")
    )

    assert out["status"] == "researching in the background"
    assert created["job"]["topic"] == "SAP ecosystem"


def test_default_topic_never_confirms_even_when_absent(monkeypatch):
    # The default topic must never trigger confirmation, even if the graph has no
    # topics yet (fresh DB) — otherwise the common path would stall.
    created: dict = {}
    _stub_jobs(monkeypatch, created)
    _stub_topics(monkeypatch, [])

    out = asyncio.run(proposals.propose_enrichment("Acme __pytest__", "acme.example"))

    assert out["status"] == "researching in the background"
    assert created["job"]["topic"] == proposals.DEFAULT_TOPIC


def test_topic_read_failure_fails_open(monkeypatch):
    # A topics-read error must not block enrichment: fail open and create the job
    # even for an otherwise-unknown topic.
    created: dict = {}
    _stub_jobs(monkeypatch, created)

    async def boom(driver):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(proposals.queries, "list_topics", boom)

    out = asyncio.run(
        proposals.propose_enrichment("Acme __pytest__", "acme.example", topic="Something novel")
    )

    assert out["status"] == "researching in the background"
    assert created["job"]["topic"] == "Something novel"
