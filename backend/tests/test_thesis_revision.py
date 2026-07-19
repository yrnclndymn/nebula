"""Thesis evidence loop (#196): the pure change-building logic + the scan→commit
ceremony, all in memory (no Neo4j, no LLM).

The LLM call (`propose_revisions`) and the graph reads/writes are mocked; everything
here exercises the deterministic filtering, the guardrails (no uncited confidence
move, no agent direct-write), and the durable-job lifecycle. Fictional company names
only (public-repo rule).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agents.deals import thesis_revision as tr
from app.agents.deals.thesis_revision import (
    LlmChange,
    ThesisRevisionProposal,
    apply_confidence_delta,
    build_reviewable_changes,
    change_evidence_sources,
    change_to_rule,
    partition_revision_decisions,
)
from app.graph import jobs
from app.main import app

# A couple of current rules + a small evidence set, reused across the pure tests.
RULES = [
    {
        "rule_key": "cloud_provider>service_provider",
        "acquirer_kind": "cloud_provider",
        "target_kind": "service_provider",
        "qualifier": "",
        "statement": "Cloud providers acquire services companies.",
        "confidence": 0.75,
    },
    {
        "rule_key": "service_provider>service_provider",
        "acquirer_kind": "service_provider",
        "target_kind": "service_provider",
        "qualifier": "",
        "statement": "Services companies acquire other services companies.",
        "confidence": 0.7,
    },
]
EVIDENCE = [
    {
        "deal_id": "d0",
        "acquirer": "Acme Cloud",
        "target": "Globex Consulting",
        "acquirer_kind": "cloud_provider",
        "target_kind": "service_provider",
        "thesis": "Bolt on delivery capacity.",
        "source": "https://news.example/acme-globex",
    },
    {
        "deal_id": "d1",
        "acquirer": "Initech Services",
        "target": "Umbrella Advisory",
        "acquirer_kind": "service_provider",
        "target_kind": "service_provider",
        "thesis": "Consolidation.",
        "source": "",  # UNCITED — must never back a change
    },
]


# --- pure: confidence delta ---------------------------------------------------


def test_apply_confidence_delta_bumps_and_weakens_and_clamps():
    assert apply_confidence_delta(0.75, "support") == 0.8
    assert apply_confidence_delta(0.7, "weaken") == 0.65
    assert apply_confidence_delta(0.98, "support") == 1.0  # clamps at 1
    assert apply_confidence_delta(0.02, "weaken") == 0.0  # clamps at 0
    assert apply_confidence_delta(0.5, "refine") == 0.5  # only support/weaken move it


# --- pure: build_reviewable_changes ------------------------------------------


def test_support_change_bumps_confidence_and_carries_cited_evidence():
    proposal = ThesisRevisionProposal(
        changes=[
            LlmChange(
                change_kind="support",
                rule_key="cloud_provider>service_provider",
                rationale="Acme→Globex fits.",
                evidence_ids=["d0"],
            )
        ]
    )
    [change] = build_reviewable_changes(proposal, RULES, EVIDENCE)
    assert change["change_kind"] == "support"
    assert change["old_confidence"] == 0.75
    assert change["new_confidence"] == 0.8
    assert change["change_id"] == "c0"
    assert [e["source"] for e in change["evidence"]] == ["https://news.example/acme-globex"]


def test_change_with_only_uncited_evidence_is_dropped():
    # A weaken that only cites d1 (no http source) has no checkable provenance.
    proposal = ThesisRevisionProposal(
        changes=[
            LlmChange(
                change_kind="weaken",
                rule_key="service_provider>service_provider",
                evidence_ids=["d1"],
            )
        ]
    )
    assert build_reviewable_changes(proposal, RULES, EVIDENCE) == []


def test_support_on_unknown_rule_is_dropped():
    proposal = ThesisRevisionProposal(
        changes=[LlmChange(change_kind="support", rule_key="isv>isv", evidence_ids=["d0"])]
    )
    assert build_reviewable_changes(proposal, RULES, EVIDENCE) == []


def test_unknown_change_kind_and_unknown_evidence_id_are_dropped():
    proposal = ThesisRevisionProposal(
        changes=[
            LlmChange(
                change_kind="delete",
                rule_key="cloud_provider>service_provider",
                evidence_ids=["d0"],
            ),
            LlmChange(
                change_kind="support",
                rule_key="cloud_provider>service_provider",
                evidence_ids=["d99"],
            ),
        ]
    )
    assert build_reviewable_changes(proposal, RULES, EVIDENCE) == []


def test_new_rule_change_validates_and_computes_rule_key():
    proposal = ThesisRevisionProposal(
        changes=[
            LlmChange(
                change_kind="new",
                acquirer_kind="Service Provider",  # normalised by ThesisRule
                target_kind="isv",
                qualifier="domain-focused",
                statement="Services firms acquire domain ISVs.",
                confidence=0.55,
                evidence_ids=["d0"],
            )
        ]
    )
    [change] = build_reviewable_changes(proposal, RULES, EVIDENCE)
    assert change["change_kind"] == "new"
    assert change["rule_key"] == "service_provider>isv|domain-focused"
    assert change["acquirer_kind"] == "service_provider"
    assert change["old_confidence"] is None
    assert change["new_confidence"] == 0.55


def test_new_rule_with_blank_statement_is_dropped():
    proposal = ThesisRevisionProposal(
        changes=[
            LlmChange(
                change_kind="new",
                acquirer_kind="isv",
                target_kind="isv",
                statement="   ",  # ThesisRule requires a non-empty statement
                evidence_ids=["d0"],
            )
        ]
    )
    assert build_reviewable_changes(proposal, RULES, EVIDENCE) == []


# --- pure: commit-side reconstruction + decision partition -------------------


def test_change_to_rule_is_reviewer_origin_with_new_confidence():
    change = {
        "acquirer_kind": "cloud_provider",
        "target_kind": "service_provider",
        "qualifier": "",
        "statement": "Cloud providers acquire services companies.",
        "new_confidence": 0.8,
    }
    rule = change_to_rule(change)
    assert rule.origin == "reviewer"
    assert rule.confidence == 0.8
    assert rule.rule_key == "cloud_provider>service_provider"


def test_change_evidence_sources_dedupes_and_keeps_http_only():
    change = {
        "evidence": [
            {"source": "https://a.example/x"},
            {"source": "https://a.example/x"},  # dupe
            {"source": "ftp://b.example/y"},  # not http(s)
            {"source": None},
        ]
    }
    assert change_evidence_sources(change) == ["https://a.example/x"]


def test_partition_revision_decisions_splits_approved_and_invalid():
    valid = {"c0", "c1", "c2"}
    approved, invalid = partition_revision_decisions(
        [
            {"change_id": "c0", "action": "approve"},
            {"change_id": "c1", "action": "skip"},  # valid, drops out
            {"change_id": "c2", "action": "bogus"},  # invalid action
            {"change_id": "cX", "action": "approve"},  # unknown id
            {"action": "approve"},  # missing id
        ],
        valid,
    )
    assert approved == ["c0"]
    assert len(invalid) == 3


# --- the flow on the in-memory ceremony --------------------------------------


@pytest.fixture
def job_store(monkeypatch):
    """Graph-backed job CRUD → a dict, mirroring the real status semantics."""
    store: dict[str, dict] = {}

    async def fake_create(job_id, job_type, data):
        store[job_id] = {**data, "type": job_type, "status": data.get("status", "pending")}

    async def fake_get(job_id):
        job = store.get(job_id)
        return dict(job) if job else None

    async def fake_update(job_id, data, status=None):
        current = store[job_id]
        store[job_id] = {**data, "type": current["type"], "status": status or current["status"]}

    async def fake_enqueue(job_id, delay=0.0):
        return None

    monkeypatch.setattr(jobs, "create_job", fake_create)
    monkeypatch.setattr(jobs, "get_job", fake_get)
    monkeypatch.setattr(jobs, "update_job", fake_update)
    monkeypatch.setattr(jobs, "enqueue", fake_enqueue)
    return store


def test_thesis_revision_flow_end_to_end(job_store, monkeypatch):
    """Scan proposes changes; commit writes ONLY the approved one, as a reviewer-origin
    rule with its cited Source; a double-commit is refused."""
    writes: list[tuple] = []

    async def fake_since(_driver):
        return None

    async def fake_evidence(_driver, _since):
        return EVIDENCE

    async def fake_rules(_driver):
        return RULES

    async def fake_propose(rules, evidence):
        return ThesisRevisionProposal(
            changes=[
                LlmChange(
                    change_kind="support",
                    rule_key="cloud_provider>service_provider",
                    evidence_ids=["d0"],
                ),
                LlmChange(
                    change_kind="weaken",
                    rule_key="service_provider>service_provider",
                    evidence_ids=["d0"],
                ),
            ]
        )

    async def fake_upsert(_driver, rule, evidence):
        writes.append((rule.rule_key, rule.origin, rule.confidence, tuple(evidence)))
        return {"rule_key": rule.rule_key}

    monkeypatch.setattr(tr, "last_committed_revision_at", fake_since)
    monkeypatch.setattr(tr, "gather_acquisition_evidence", fake_evidence)
    monkeypatch.setattr(tr, "get_thesis_rules", fake_rules)
    monkeypatch.setattr(tr, "propose_revisions", fake_propose)
    monkeypatch.setattr(tr, "upsert_thesis_rule", fake_upsert)
    monkeypatch.setattr(tr, "get_driver", lambda: None)

    async def scenario():
        handle = await tr.enqueue_thesis_revision()
        job_id = handle["job_id"]
        await tr.execute_thesis_revision_job(job_id)
        ready = await tr.get_thesis_revision(job_id)
        # Approve only the support change; skip the weaken.
        decisions = [
            {"change_id": "c0", "action": "approve"},
            {"change_id": "c1", "action": "skip"},
        ]
        first = await tr.commit_thesis_revision(job_id, decisions)
        second = await tr.commit_thesis_revision(job_id, decisions)
        return ready, first, second

    ready, first, second = asyncio.run(scenario())
    assert ready["status"] == "ready"
    assert ready["deal_count"] == 2
    assert len(ready["changes"]) == 2  # both cited d0 (http source)
    assert first == {"applied": 1, "rules": ["cloud_provider>service_provider"]}
    # ONLY the approved change wrote — reviewer origin, bumped confidence, cited source.
    assert writes == [
        (
            "cloud_provider>service_provider",
            "reviewer",
            0.8,
            ("https://news.example/acme-globex",),
        )
    ]
    assert "error" in second  # committed job no longer passes the ready-guard


def test_commit_rejects_malformed_batch_before_writing(job_store, monkeypatch):
    writes: list = []

    async def fake_upsert(_driver, rule, evidence):
        writes.append(rule.rule_key)

    monkeypatch.setattr(tr, "upsert_thesis_rule", fake_upsert)
    monkeypatch.setattr(tr, "get_driver", lambda: None)

    async def scenario():
        job_id = "trX"
        await jobs.create_job(
            job_id,
            "thesis_revision",
            {
                "job_id": job_id,
                "status": "pending",
                "changes": [
                    {
                        "change_id": "c0",
                        "change_kind": "support",
                        "acquirer_kind": "cloud_provider",
                        "target_kind": "service_provider",
                        "qualifier": "",
                        "statement": "x",
                        "new_confidence": 0.8,
                        "evidence": [{"source": "https://n.example/d"}],
                    }
                ],
            },
        )
        job = await jobs.get_job(job_id)
        await jobs.update_job(job_id, {**job}, status="ready")
        # An unknown action makes the whole batch invalid.
        return await tr.commit_thesis_revision(job_id, [{"change_id": "c0", "action": "nope"}])

    out = asyncio.run(scenario())
    assert out == {"error": "invalid thesis revision decisions"}
    assert writes == []  # nothing written when the batch is rejected


def test_commit_requires_a_ready_job(job_store, monkeypatch):
    monkeypatch.setattr(tr, "get_driver", lambda: None)
    out = asyncio.run(tr.commit_thesis_revision("nope", [{"change_id": "c0", "action": "approve"}]))
    assert out == {"error": "thesis revision job not found or not ready"}


def test_propose_revisions_skips_llm_when_no_evidence():
    # No deals → no LLM call, empty proposal (nothing to weigh).
    out = asyncio.run(tr.propose_revisions(RULES, []))
    assert out.changes == []


# --- routes: the shared 404 guards over the new surface ----------------------


def test_status_endpoint_404s_then_returns_the_job(monkeypatch):
    from app.api import routes

    async def missing(*_a, **_k):
        return None

    monkeypatch.setattr(routes, "get_thesis_revision", missing)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/thesis/revision/x1")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown thesis revision job"

    async def found(*_a, **_k):
        return {"job_id": "x1", "status": "ready"}

    monkeypatch.setattr(routes, "get_thesis_revision", found)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/thesis/revision/x1")
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "x1"


def test_scan_endpoint_passes_the_poll_handle_through(monkeypatch):
    from app.api import routes

    async def fake_enqueue():
        return {"job_id": "x1", "status": "scanning in the background"}

    monkeypatch.setattr(routes, "enqueue_thesis_revision", fake_enqueue)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/thesis/revision/scan")
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "x1"


def test_commit_endpoint_translates_error_to_404(monkeypatch):
    from app.api import routes

    async def fake_error(*_a, **_k):
        return {"error": "thesis revision job not found or not ready"}

    monkeypatch.setattr(routes, "commit_thesis_revision", fake_error)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/thesis/revision/x1/commit", json={"decisions": []})
    assert resp.status_code == 404
