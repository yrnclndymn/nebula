"""Route wiring for the Signals UI endpoints (#38): the per-company timeline
`GET /companies/{name}/signals` and the filterable What's-new feed `GET /signals`.

These assert the endpoints pass their query params through to the graph read
helpers and return the shaped list — the helpers themselves are covered (with a
real Neo4j) in test_signals.py. No database here: the helpers are monkeypatched,
so the wiring is checked deterministically. Fictional names only (public repo).
"""

from fastapi.testclient import TestClient

from app.api import routes
from app.main import app

_SIGNAL = {
    "url": "https://news.example.com/story",
    "title": "Acme raises a round",
    "kind": "news",
    "summary": "s",
    "publishedAt": "2026-05-01T00:00:00+00:00",
    "publishedAtRaw": None,
    "capturedAt": "2026-05-02T00:00:00+00:00",
    "companies": ["Acme"],
    "sources": ["https://src.example.com"],
}


def test_company_signals_passes_name_and_limit(monkeypatch):
    seen = {}

    async def fake_for_company(driver, name, limit=20):
        seen["name"] = name
        seen["limit"] = limit
        return [_SIGNAL]

    monkeypatch.setattr(routes.signals, "signals_for_company", fake_for_company)
    with TestClient(app) as client:
        resp = client.get("/companies/Acme%20Corp/signals?limit=5")
    assert resp.status_code == 200
    assert resp.json() == [_SIGNAL]
    assert seen == {"name": "Acme Corp", "limit": 5}


def test_recent_signals_passes_kind_and_topic(monkeypatch):
    seen = {}

    async def fake_recent(driver, limit=40, kind=None, topic=None):
        seen.update(limit=limit, kind=kind, topic=topic)
        return [_SIGNAL]

    monkeypatch.setattr(routes.signals, "recent_signals_filtered", fake_recent)
    with TestClient(app) as client:
        resp = client.get("/signals?kind=news&topic=Cloud&limit=10")
    assert resp.status_code == 200
    assert resp.json() == [_SIGNAL]
    assert seen == {"limit": 10, "kind": "news", "topic": "Cloud"}


def test_recent_signals_defaults_no_filters(monkeypatch):
    seen = {}

    async def fake_recent(driver, limit=40, kind=None, topic=None):
        seen.update(limit=limit, kind=kind, topic=topic)
        return []

    monkeypatch.setattr(routes.signals, "recent_signals_filtered", fake_recent)
    with TestClient(app) as client:
        resp = client.get("/signals")
    assert resp.status_code == 200
    assert resp.json() == []
    assert seen == {"limit": 40, "kind": None, "topic": None}


def test_signals_limit_bounds_enforced(monkeypatch):
    async def fake_recent(driver, limit=40, kind=None, topic=None):
        return []

    monkeypatch.setattr(routes.signals, "recent_signals_filtered", fake_recent)
    with TestClient(app) as client:
        assert client.get("/signals?limit=0").status_code == 422
        assert client.get("/signals?limit=999").status_code == 422
