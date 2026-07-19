"""Route wiring for the thesis surface endpoint (#195, epic #192): `GET /thesis`.

The read-only surface behind the M&A page's Market-thesis panel. This asserts the
endpoint delegates to `app.graph.thesis.get_thesis_rules` and passes its shaped list
straight through — the read helper itself (with a real Neo4j) is covered in
test_thesis_graph.py. No database here: the helper is monkeypatched, so the wiring is
checked deterministically. Abstract kinds + a fictional source URL only (public repo).
"""

from fastapi.testclient import TestClient

from app.graph import thesis
from app.main import app

_RULE = {
    "rule_key": "cloud_provider>service_provider",
    "acquirer_kind": "cloud_provider",
    "target_kind": "service_provider",
    "qualifier": "",
    "statement": "Cloud providers are currently acquiring services companies.",
    "confidence": 0.75,
    "origin": "user",
    "updated_at": "2026-07-19T00:00:00Z",
    "evidence_count": 1,
    "sources": ["https://news.example/deal"],
}


def test_thesis_endpoint_returns_rules(monkeypatch):
    seen = {}

    async def fake_get_rules(driver):
        seen["called"] = True
        return [_RULE]

    # The handler imports the module function-locally (append-only routes block),
    # so patch the source module attribute — the lookup happens at call time.
    monkeypatch.setattr(thesis, "get_thesis_rules", fake_get_rules)
    with TestClient(app) as client:
        resp = client.get("/thesis")
    assert resp.status_code == 200
    assert resp.json() == [_RULE]
    assert seen == {"called": True}


def test_thesis_endpoint_empty(monkeypatch):
    async def fake_get_rules(driver):
        return []

    monkeypatch.setattr(thesis, "get_thesis_rules", fake_get_rules)
    with TestClient(app) as client:
        resp = client.get("/thesis")
    assert resp.status_code == 200
    assert resp.json() == []


def test_thesis_endpoint_requires_auth():
    from app.config import settings

    settings.require_auth = True
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/thesis").status_code == 401
    finally:
        settings.require_auth = False
