"""Smoke test: the app boots and liveness responds without a database."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_ok():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_graph_size_metrics(monkeypatch):
    """The size-metrics endpoint (#37) reports node/rel totals against the Aura
    node cap plus the signal breakdown; 503 when the graph is unreachable. Shape
    is asserted only when Neo4j is up (CI is the arbiter)."""
    with TestClient(app) as client:
        resp = client.get("/health/graph/size")
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        body = resp.json()
        assert body["nodeCap"] == 200_000
        assert isinstance(body["nodes"], int)
        assert isinstance(body["relationships"], int)
        assert "byKind" in body["signals"]
