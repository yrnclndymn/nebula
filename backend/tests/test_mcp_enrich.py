"""MCP `enrich_company` must go through propose→review→commit, not write directly.

Story #52: the MCP tool used to call the enrichment agent and save straight to the
graph, bypassing the human-in-the-loop every other write path honours. These tests
pin the new behaviour — a proposal is created, direct-write is OFF by default and
only reachable behind an explicit opt-in, and commit requires an explicit id and
respects the 'ready' gate.

Pure logic: the graph/agent calls are mocked, so this runs without Neo4j or an LLM.
"""

import asyncio

from app import mcp_server
from app.config import settings


def _run(coro):
    return asyncio.run(coro)


def test_direct_write_defaults_off():
    # The opt-in verified at the actual wiring, not just a signature: the setting
    # itself must default OFF so no MCP client can silently write to Aura.
    assert settings.mcp_enrich_direct_write is False


def test_enrich_company_proposes_instead_of_writing(monkeypatch):
    calls = {}

    async def fake_propose(name, website, topic="AI-native engineering"):
        calls["propose"] = (name, website, topic)
        return {"proposal_id": "abc123", "name": name, "status": "researching in the background"}

    async def fake_enrich(*a, **k):  # must NOT be called on the default path
        calls["enrich"] = True
        raise AssertionError("direct enrich must not run when direct-write is off")

    monkeypatch.setattr(mcp_server.settings, "mcp_enrich_direct_write", False)
    monkeypatch.setattr(mcp_server.proposals, "propose_enrichment", fake_propose)
    monkeypatch.setattr(mcp_server, "enrich", fake_enrich)

    out = _run(mcp_server.enrich_company("Acme", "acme.com", "AI-native engineering"))

    assert calls["propose"] == ("Acme", "acme.com", "AI-native engineering")
    assert "enrich" not in calls
    assert out["proposal_id"] == "abc123"
    assert out["review"] is True
    assert out["status"] == "researching in the background"


def test_enrich_company_direct_write_only_behind_optin(monkeypatch):
    seen = {}

    class Result:
        summary = "saved Acme"

    async def fake_enrich(name, website, topic, verbose=False):
        seen["enrich"] = (name, website, topic)
        return Result()

    async def fake_propose(*a, **k):  # must NOT be called on the opt-in path
        raise AssertionError("propose must not run when direct-write is explicitly on")

    monkeypatch.setattr(mcp_server.settings, "mcp_enrich_direct_write", True)
    monkeypatch.setattr(mcp_server, "enrich", fake_enrich)
    monkeypatch.setattr(mcp_server.proposals, "propose_enrichment", fake_propose)

    out = _run(mcp_server.enrich_company("Acme", "acme.com"))

    assert seen["enrich"] == ("Acme", "acme.com", "AI-native engineering")
    assert out["written"] == "Acme"
    assert out["review"] is False


def test_proposal_status_surfaces_review_fields(monkeypatch):
    async def fake_get(proposal_id):
        return {
            "status": "ready",
            "name": "Acme",
            "summary": "found HQ + headcount",
            "diff": [{"field": "headcount", "to": 42}],
            "committed": False,
        }

    monkeypatch.setattr(mcp_server.proposals, "get_proposal", fake_get)
    out = _run(mcp_server.proposal_status("abc123"))

    assert out["status"] == "ready"
    assert out["name"] == "Acme"
    assert out["diff"] == [{"field": "headcount", "to": 42}]
    assert out["proposal_id"] == "abc123"


def test_proposal_status_unknown(monkeypatch):
    async def fake_get(proposal_id):
        return None

    monkeypatch.setattr(mcp_server.proposals, "get_proposal", fake_get)
    out = _run(mcp_server.proposal_status("nope"))
    assert "error" in out


def test_commit_proposal_requires_id(monkeypatch):
    async def fake_commit(*a, **k):
        raise AssertionError("commit must not be attempted without an id")

    monkeypatch.setattr(mcp_server.proposals, "commit_proposal", fake_commit)
    out = _run(mcp_server.commit_proposal("   "))
    assert "error" in out


def test_commit_proposal_delegates_with_id(monkeypatch):
    seen = {}

    async def fake_commit(proposal_id, scope="all"):
        seen["args"] = (proposal_id, scope)
        return {"committed": "Acme", "scope": scope}

    monkeypatch.setattr(mcp_server.proposals, "commit_proposal", fake_commit)
    out = _run(mcp_server.commit_proposal(" abc123 ", "focus"))

    assert seen["args"] == ("abc123", "focus")  # trimmed, passed through
    assert out == {"committed": "Acme", "scope": "focus"}


def test_commit_proposal_not_ready_is_refused(monkeypatch):
    # The real commit_proposal returns an error for a non-ready proposal; the MCP
    # tool must surface it rather than force a write (respects the review gate).
    async def fake_commit(proposal_id, scope="all"):
        return {"error": "proposal not found or not ready"}

    monkeypatch.setattr(mcp_server.proposals, "commit_proposal", fake_commit)
    out = _run(mcp_server.commit_proposal("abc123"))
    assert out["error"] == "proposal not found or not ready"
