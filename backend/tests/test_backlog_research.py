"""Backlog "research selected" trigger + website discovery (issue #31).

The endpoint validation (cap + auth) and the discovery heuristic are pure/mocked,
so they run anywhere — no Neo4j and no network. Fictional company names only.
"""

import asyncio

from fastapi.testclient import TestClient

from app.agents.assistant import proposals
from app.config import settings
from app.main import app

# --- Trigger endpoint: cap + auth (no DB reached before validation) ----------


def test_backlog_research_rejects_over_cap():
    # 11 distinct names exceeds the server-side sanity cap → rejected before any
    # job is enqueued (so no database is touched).
    names = [f"Co {i} __pytest__" for i in range(11)]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/backlog/research", json={"names": names})
    assert resp.status_code == 422
    assert "at most 10" in resp.json()["detail"]


def test_backlog_research_rejects_empty():
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/backlog/research", json={"names": ["  ", ""]})
    assert resp.status_code == 422


def test_backlog_research_dedupes_before_cap(monkeypatch):
    # 11 entries but only 10 distinct (case-insensitive) → under the cap. Mock the
    # proposal call (needs Neo4j) so we can assert the EXACT dedup outcome: 200 and
    # ten proposals — not merely "validation didn't 422" (a 500 must not pass).
    from app.api import routes

    started: list[str] = []

    async def fake_propose(
        name: str, website: str, topic: str = "", focus: str = "", enqueue_delay: float = 0.0
    ) -> dict:
        started.append(name)
        return {"proposal_id": f"p{len(started)}", "name": name, "status": "pending"}

    monkeypatch.setattr(routes, "propose_enrichment", fake_propose)
    names = [f"Co {i} __pytest__" for i in range(10)] + ["co 0 __pytest__"]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/backlog/research", json={"names": names})
    assert resp.status_code == 200
    assert len(started) == 10  # the 11th (case-variant duplicate) was deduped
    assert len(resp.json()["proposals"]) == 10


def test_backlog_research_staggers_enqueue_delays(monkeypatch):
    # The batch must not fire simultaneously: each proposal gets an enqueue_delay
    # of i * research_stagger_seconds, so the i-th starts progressively later.
    from app.api import routes

    delays: list[float] = []

    async def fake_propose(
        name: str, website: str, topic: str = "", focus: str = "", enqueue_delay: float = 0.0
    ) -> dict:
        delays.append(enqueue_delay)
        return {"proposal_id": f"p{len(delays)}", "name": name, "status": "pending"}

    monkeypatch.setattr(routes, "propose_enrichment", fake_propose)
    monkeypatch.setattr(settings, "research_stagger_seconds", 5.0)
    names = [f"Co {i} __pytest__" for i in range(4)]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/backlog/research", json={"names": names})
    assert resp.status_code == 200
    assert delays == [0.0, 5.0, 10.0, 15.0]  # staggered, first starts immediately


def test_backlog_research_requires_auth():
    settings.require_auth = True
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/backlog/research", json={"names": ["Acme __pytest__"]})
        assert resp.status_code == 401  # missing bearer token, rejected by verify_user
    finally:
        settings.require_auth = False


# --- Website discovery heuristic (search mocked; no network) -----------------


def _mock_search(results, monkeypatch):
    monkeypatch.setattr(proposals, "web_search", lambda query: {"results": results})


def test_discover_website_skips_social_takes_first_plausible(monkeypatch):
    _mock_search(
        [
            {"title": "Acme | LinkedIn", "url": "https://www.linkedin.com/company/acme"},
            {"title": "Acme - Wikipedia", "url": "https://en.wikipedia.org/wiki/Acme"},
            {"title": "Acme Corp", "url": "https://www.acme-corp.example/about"},
        ],
        monkeypatch,
    )
    assert asyncio.run(proposals.discover_website("Acme Corp")) == "acme-corp.example"


def test_discover_website_none_when_only_non_official(monkeypatch):
    _mock_search(
        [
            {"url": "https://www.linkedin.com/company/globex"},
            {"url": "https://crunchbase.com/organization/globex"},
        ],
        monkeypatch,
    )
    assert asyncio.run(proposals.discover_website("Globex")) is None


def test_discover_website_none_when_no_results(monkeypatch):
    _mock_search([], monkeypatch)
    assert asyncio.run(proposals.discover_website("Initech")) is None
