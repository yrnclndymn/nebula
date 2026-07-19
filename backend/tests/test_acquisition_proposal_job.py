"""Characterization of the acquisition proposal JOB RUNNER + listing (#140).

test_acquisitions.py already covers the pure build/diff guardrails and the happy
commit path; this file hardens the *runner's* state machine — argument threading
(the right job_id / driver / names reach each collaborator), the budget cap that
guards spend, the diff against already-stored deals, and the two failure branches
(quota exhaustion vs. any other error). The durable job store, research agent and
graph are all mocked, so it needs neither Gemini nor Neo4j.

All fixtures use fictional companies (Acme/Globex/Initech).
"""

import asyncio
from contextlib import contextmanager

from app.agents.deals import proposals
from app.agents.deals.models import AcquisitionResearch, DealResearch
from app.genai_retry import QuotaExhausted

SRC = "https://news.example/acme-deal"
AMT_SRC = "https://filings.example/acme-terms"

# A sentinel the real get_driver is replaced with, so a mutant that nulls the
# `driver` local (or passes None where the handle belongs) trips the collaborators'
# `driver is DRIVER` assertions instead of silently matching a None-returning stub.
DRIVER = object()


def _deal(**kw) -> DealResearch:
    base = dict(acquirer="Acme", target="Globex", source=SRC)
    base.update(kw)
    return DealResearch(**base)


# --- success path: state transitions + argument threading --------------------


def test_execute_threads_ids_driver_and_names_then_marks_ready(monkeypatch):
    """The runner must feed the RIGHT job_id, driver and researched names to each
    collaborator and land the job in `ready` with a record/diff/outcome. The fakes
    assert on what they receive, so any mutant that nulls or drops an argument fails
    here rather than surviving against argument-blind stubs."""
    job = {"job_id": "aq1", "status": "pending", "company": "Acme"}
    saved: dict = {}

    async def fake_get_job(job_id):
        assert job_id == "aq1"  # the polled id must be forwarded verbatim
        return job

    async def fake_update_job(job_id, data, status=None):
        assert job_id == "aq1"
        saved.update(data)
        saved["status"] = status

    async def fake_research(name):
        assert name == "Acme"
        return AcquisitionResearch(
            company=name,
            deals=[_deal(), _deal(target="Initech")],  # two distinct cited deals
        )

    async def fake_canonical(driver, names):
        assert driver is DRIVER
        # Flattened (acquirer, target) pairs, in deal order — not None, not empty.
        assert names == ["Acme", "Globex", "Acme", "Initech"]
        return {}

    async def fake_existing(driver, company):
        assert driver is DRIVER
        assert company == "Acme"
        return []

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals, "canonical_names", fake_canonical)
    monkeypatch.setattr(proposals, "get_acquisitions", fake_existing)
    monkeypatch.setattr(proposals, "get_driver", lambda: DRIVER)

    asyncio.run(proposals.execute_acquisition_proposal_job("aq1"))

    assert saved["status"] == "ready"
    assert "record" in saved and "diff" in saved and "outcome" in saved
    targets = {d["target"] for d in saved["record"]["deals"]}
    assert targets == {"Globex", "Initech"}
    assert len(saved["diff"]) == 2  # nothing stored yet -> both new
    assert saved["outcome"] == "proposal ready for Acme (2 new/changed deal(s))"


def test_execute_installs_the_acquisition_research_budget(monkeypatch):
    """Research runs inside the `acquisition_research` budget built from the job's
    own overrides — the spend guardrail. Pins the budget category name, the override
    source (`job['budget']`), and that the built budget is the one actually installed
    for the run."""
    job = {"job_id": "aq1", "status": "pending", "company": "Acme", "budget": {"max_llm_calls": 3}}
    seen: dict = {}
    SENTINEL_BUDGET = object()

    def fake_budget_for(job_type, overrides=None):
        seen["job_type"] = job_type
        seen["overrides"] = overrides
        return SENTINEL_BUDGET

    @contextmanager
    def fake_use_budget(budget):
        seen["installed"] = budget
        yield budget

    async def fake_get_job(job_id):
        return job

    async def fake_update_job(job_id, data, status=None):
        pass

    async def fake_research(name):
        return AcquisitionResearch(company=name, deals=[])

    async def fake_canonical(driver, names):
        return {}

    async def fake_existing(driver, company):
        return []

    monkeypatch.setattr(proposals, "budget_for", fake_budget_for)
    monkeypatch.setattr(proposals, "use_budget", fake_use_budget)
    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals, "canonical_names", fake_canonical)
    monkeypatch.setattr(proposals, "get_acquisitions", fake_existing)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.execute_acquisition_proposal_job("aq1"))

    assert seen["job_type"] == "acquisition_research"
    assert seen["overrides"] == {"max_llm_calls": 3}  # the job's own cap overrides
    assert seen["installed"] is SENTINEL_BUDGET  # that exact budget wrapped the run


def test_execute_diffs_against_already_stored_deals(monkeypatch):
    """The stored proposal's diff is computed against what's ALREADY in the graph:
    a re-proposed deal at an unchanged amount is hidden, only the genuinely new one
    surfaces. Guards the runner passing the real `existing` list into the diff (a
    mutant that diffs against nothing would mark the repeat deal 'new')."""
    job = {"job_id": "aq1", "status": "pending", "company": "Acme"}
    saved: dict = {}

    async def fake_get_job(job_id):
        return job

    async def fake_update_job(job_id, data, status=None):
        saved.update(data)
        saved["status"] = status

    async def fake_research(name):
        return AcquisitionResearch(
            company=name,
            deals=[
                _deal(amount="$1B", currency="USD", amount_source=AMT_SRC),  # already stored
                _deal(target="Initech"),  # brand new
            ],
        )

    async def fake_canonical(driver, names):
        return {}

    async def fake_existing(driver, company):
        return [{"acquirer": "Acme", "target": "Globex", "amount": "$1B"}]

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals, "canonical_names", fake_canonical)
    monkeypatch.setattr(proposals, "get_acquisitions", fake_existing)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.execute_acquisition_proposal_job("aq1"))

    assert saved["status"] == "ready"
    assert len(saved["diff"]) == 1  # the unchanged Globex deal is hidden
    assert saved["diff"][0]["deal"]["target"] == "Initech"
    assert saved["diff"][0]["status"] == "new"
    assert saved["outcome"] == "proposal ready for Acme (1 new/changed deal(s))"


def test_execute_reports_no_cited_acquisitions_branch(monkeypatch):
    """When research turns up no CITED deal, the record has no facts and the job
    still lands `ready` — but with the 'no cited acquisitions' outcome (the else
    arm of the outcome expression, otherwise entirely uncovered)."""
    job = {"job_id": "aq1", "status": "pending", "company": "Acme"}
    saved: dict = {}

    async def fake_get_job(job_id):
        return job

    async def fake_update_job(job_id, data, status=None):
        saved.update(data)
        saved["status"] = status

    async def fake_research(name):
        # A deal with no source URL fails the provenance gate and is dropped.
        return AcquisitionResearch(company=name, deals=[_deal(source=None)])

    async def fake_canonical(driver, names):
        return {}

    async def fake_existing(driver, company):
        return []

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals, "canonical_names", fake_canonical)
    monkeypatch.setattr(proposals, "get_acquisitions", fake_existing)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.execute_acquisition_proposal_job("aq1"))

    assert saved["status"] == "ready"
    assert saved["record"]["deals"] == []
    assert saved["outcome"] == "no cited acquisitions found for Acme"


def test_execute_records_quota_exhaustion_on_the_job(monkeypatch):
    """A `QuotaExhausted` from research is recorded on the job as an `error` with the
    friendly message + raw detail, status flipped to `error` — the same job_id, and
    the message/detail under their own keys."""
    job = {"job_id": "aq1", "status": "pending", "company": "Acme"}
    saved: dict = {}
    exc = QuotaExhausted(RuntimeError("429 RESOURCE_EXHAUSTED: slow down"))

    async def fake_get_job(job_id):
        return job

    async def fake_update_job(job_id, data, status=None):
        assert job_id == "aq1"
        saved.update(data)
        saved["status"] = status

    async def fake_retry(factory, **kw):
        raise exc

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "run_with_quota_retry", fake_retry)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.execute_acquisition_proposal_job("aq1"))

    assert saved["status"] == "error"
    assert saved["error"] == exc.message
    assert saved["error_detail"] == exc.detail


def test_execute_records_generic_failure_on_the_job(monkeypatch):
    """Any non-quota failure is surfaced on the job too: `error` = the stringified
    exception, status `error`, same job_id. Distinct from the quota branch (no
    `error_detail`), so both arms of the try/except are pinned."""
    job = {"job_id": "aq1", "status": "pending", "company": "Acme"}
    saved: dict = {}

    async def fake_get_job(job_id):
        return job

    async def fake_update_job(job_id, data, status=None):
        assert job_id == "aq1"
        saved.update(data)
        saved["status"] = status

    async def fake_research(name):
        raise ValueError("crawl blew up")

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.execute_acquisition_proposal_job("aq1"))

    assert saved["status"] == "error"
    assert saved["error"] == "crawl blew up"  # str(exc), not str(None)


def test_execute_returns_early_for_unknown_job(monkeypatch):
    """A vanished job_id short-circuits before any research or write — the guard on
    a missing job."""
    calls = {"research": 0, "update": 0}

    async def fake_get_job(job_id):
        return None

    async def fake_research(name):
        calls["research"] += 1
        return AcquisitionResearch(company=name, deals=[])

    async def fake_update_job(job_id, data, status=None):
        calls["update"] += 1

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.execute_acquisition_proposal_job("ghost"))
    assert calls == {"research": 0, "update": 0}  # nothing ran


# --- commit write path: id + driver threading ---------------------------------


def test_commit_threads_job_id_and_driver_to_the_write(monkeypatch):
    """The human-approved commit must read/write the SAME job it was handed and run
    the graph write against the real driver handle. Argument-blind stubs let a
    mutant null any of these silently; these fakes assert on what they receive."""
    from app.graph.deal_models import AcquisitionRecord, Deal

    record = AcquisitionRecord(
        company="Acme", deals=[Deal(acquirer="Acme", target="Globex", source=SRC)]
    ).model_dump()
    job = {"job_id": "aq1", "status": "ready", "record": record}
    updated: dict = {}

    async def fake_get_job(job_id):
        assert job_id == "aq1"
        return job

    async def fake_update_job(job_id, data, status=None):
        assert job_id == "aq1"
        updated.update(data)
        updated["status"] = status

    async def fake_upsert(driver, rec):
        assert driver is DRIVER  # the write runs against the real handle
        return {"company": rec.company, "action": "written", "deals": len(rec.deals)}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "upsert_acquisitions", fake_upsert)
    monkeypatch.setattr(proposals, "get_driver", lambda: DRIVER)

    res = asyncio.run(proposals.commit_acquisition_proposal("aq1"))
    assert res["committed"] == "Acme" and res["deals"] == 1
    assert updated["committed"] is True and updated["status"] == "committed"


# --- listing: limit boundary, skip-vs-stop, and the row contract --------------


def _ready(job_id: str, company: str, created_at: str) -> dict:
    return {
        "id": job_id,
        "type": "acquisition_proposal",
        "status": "ready",
        "createdAt": created_at,
    }


def test_list_honors_the_limit_boundary(monkeypatch):
    """`limit` caps the rows returned exactly (inclusive break, not off-by-one) and
    the accumulated rows are returned — not discarded — when the cap is hit."""
    summaries = [
        _ready("aq3", "Acme", "2026-01-03"),
        _ready("aq2", "Globex", "2026-01-02"),
        _ready("aq1", "Initech", "2026-01-01"),
    ]
    jobs_by_id = {s["id"]: {"company": "X", "status": "ready"} for s in summaries}

    async def fake_list_jobs(driver, *, type=None, status=None, limit=50):
        return summaries

    async def fake_get_job(job_id):
        return jobs_by_id[job_id]

    monkeypatch.setattr(proposals.jobs, "list_jobs", fake_list_jobs)
    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    rows = asyncio.run(proposals.list_acquisition_proposals(limit=2))
    assert isinstance(rows, list)  # not None (the break returns the list, not nothing)
    assert len(rows) == 2  # exactly the limit, no third row
    assert [r["job_id"] for r in rows] == ["aq3", "aq2"]


def test_list_skips_nonmatching_company_without_stopping(monkeypatch):
    """The company filter SKIPS non-matching jobs and keeps scanning — it must not
    stop at the first mismatch, or a matching proposal sitting behind an unrelated
    one in the scan would silently vanish from the drawer's per-company section."""
    summaries = [
        _ready("other", "Globex", "2026-01-02"),  # scanned first, does NOT match
        _ready("wanted", "Acme", "2026-01-01"),  # the match, later in the scan
    ]
    jobs_by_id = {
        "other": {"company": "Globex", "status": "ready"},
        "wanted": {"company": "Acme", "status": "ready"},
    }

    async def fake_list_jobs(driver, *, type=None, status=None, limit=50):
        return summaries

    async def fake_get_job(job_id):
        return jobs_by_id[job_id]

    monkeypatch.setattr(proposals.jobs, "list_jobs", fake_list_jobs)
    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    rows = asyncio.run(proposals.list_acquisition_proposals(company="Acme"))
    assert [r["job_id"] for r in rows] == ["wanted"]  # found past the mismatch


def test_list_row_contract_and_scan_driver(monkeypatch):
    """A listed row carries the exact compact contract the SPA review card reads,
    including `committed` and `created_at` (sourced from the summary's `createdAt`),
    and the scan runs against the real driver handle."""
    summaries = [_ready("aq1", "Acme", "2026-01-02")]
    job = {
        "company": "Acme",
        "status": "ready",
        "record": {"company": "Acme", "deals": [{"acquirer": "Acme", "target": "Globex"}]},
        "diff": [{"deal": {"target": "Globex"}, "status": "new"}],
        "outcome": "proposal ready for Acme (1 new/changed deal(s))",
        "error": None,
    }

    async def fake_list_jobs(driver, *, type=None, status=None, limit=50):
        assert driver is DRIVER  # the real handle, not a nulled arg
        return summaries

    async def fake_get_job(job_id):
        return job

    monkeypatch.setattr(proposals.jobs, "list_jobs", fake_list_jobs)
    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "get_driver", lambda: DRIVER)

    rows = asyncio.run(proposals.list_acquisition_proposals())
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "job_id",
        "company",
        "status",
        "deal_count",
        "new_count",
        "outcome",
        "error",
        "committed",
        "created_at",
    }
    assert row["job_id"] == "aq1"
    assert row["company"] == "Acme"
    assert row["status"] == "ready"
    assert row["deal_count"] == 1
    assert row["new_count"] == 1
    assert row["committed"] is False
    assert row["created_at"] == "2026-01-02"  # mapped from the summary's createdAt
