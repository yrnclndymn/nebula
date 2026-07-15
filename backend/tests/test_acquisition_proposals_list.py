"""List acquisition proposals awaiting review (#133): the read-only getter behind
the SPA review card. The commit/discard write paths already have coverage in
test_acquisitions.py — this pins the *listing* used to discover pending/ready
proposals. The durable job store is mocked, so it needs neither Gemini nor Neo4j.
All fixtures use fictional companies (Acme/Globex/Initech/Umbrella).
"""

import asyncio

from app.agents.deals import proposals

SRC = "https://news.example/acme-buys-globex"


def _summaries() -> list[dict]:
    # Newest-first, as jobs.list_jobs returns them.
    return [
        {
            "id": "aq3",
            "type": "acquisition_proposal",
            "status": "committed",
            "createdAt": "2026-01-03",
        },
        {"id": "aq1", "type": "acquisition_proposal", "status": "ready", "createdAt": "2026-01-02"},
        {
            "id": "aq2",
            "type": "acquisition_proposal",
            "status": "pending",
            "createdAt": "2026-01-01",
        },
    ]


def _jobs_by_id() -> dict:
    return {
        "aq1": {
            "company": "Acme",
            "status": "ready",
            "record": {
                "company": "Acme",
                "deals": [{"acquirer": "Acme", "target": "Globex", "source": SRC}],
            },
            "diff": [{"deal": {"acquirer": "Acme", "target": "Globex"}, "status": "new"}],
            "outcome": "proposal ready for Acme (1 new/changed deal(s))",
        },
        "aq2": {"company": "Initech", "status": "pending"},  # still researching: no record yet
        "aq3": {
            "company": "Umbrella",
            "status": "committed",
            "committed": True,
            "record": {"company": "Umbrella", "deals": []},
        },
    }


def _install(monkeypatch, summaries, jobs_by_id) -> None:
    async def fake_list_jobs(driver, *, type=None, status=None, limit=50):
        assert type == "acquisition_proposal"  # scoped to the deal proposals only
        return summaries

    async def fake_get_job(job_id):
        return jobs_by_id.get(job_id)

    monkeypatch.setattr(proposals.jobs, "list_jobs", fake_list_jobs)
    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)


def test_list_returns_pending_and_ready_excludes_committed(monkeypatch):
    _install(monkeypatch, _summaries(), _jobs_by_id())

    rows = asyncio.run(proposals.list_acquisition_proposals())
    by_id = {r["job_id"]: r for r in rows}

    assert set(by_id) == {"aq1", "aq2"}  # a committed proposal is already reviewed — excluded
    assert by_id["aq1"]["company"] == "Acme"
    assert by_id["aq1"]["status"] == "ready"
    assert by_id["aq1"]["deal_count"] == 1
    assert by_id["aq1"]["new_count"] == 1  # one entry in the diff
    assert by_id["aq1"]["outcome"].startswith("proposal ready")
    # A still-researching proposal has no record yet — counts default to zero.
    assert by_id["aq2"]["status"] == "pending"
    assert by_id["aq2"]["deal_count"] == 0
    assert by_id["aq2"]["new_count"] == 0


def test_list_filters_by_company(monkeypatch):
    _install(monkeypatch, _summaries(), _jobs_by_id())

    rows = asyncio.run(proposals.list_acquisition_proposals(company="Acme"))
    assert [r["job_id"] for r in rows] == ["aq1"]

    assert asyncio.run(proposals.list_acquisition_proposals(company="Nobody")) == []


def test_list_surfaces_errored_proposals(monkeypatch):
    summaries = [
        {"id": "aqe", "type": "acquisition_proposal", "status": "error", "createdAt": "2026-01-04"},
    ]
    jobs_by_id = {
        "aqe": {"company": "Globex", "status": "error", "error": "research quota exhausted"},
    }
    _install(monkeypatch, summaries, jobs_by_id)

    rows = asyncio.run(proposals.list_acquisition_proposals())
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "research quota exhausted"


def test_ma_proposals_endpoint_requires_auth():
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    settings.require_auth = True
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/ma/proposals").status_code == 401
    finally:
        settings.require_auth = False
