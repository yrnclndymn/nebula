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


def test_backlog_research_dedupes_before_cap():
    # 11 entries but only 10 distinct (case-insensitive) → under the cap. Without a
    # DB the enqueue fails, but we only assert validation let it through (not 422).
    names = [f"Co {i} __pytest__" for i in range(10)] + ["co 0 __pytest__"]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/backlog/research", json={"names": names})
    assert resp.status_code != 422


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
