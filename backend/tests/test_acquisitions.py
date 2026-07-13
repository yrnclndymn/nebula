"""Acquisition research (#43): provenance-gated build, diff, and the durable
propose→review→commit job flow.

The build/diff logic is PURE (no DB, no model, no network) so it runs anywhere and
is the real guardrail under test: no deal is committable without a valid citation,
and — the money guardrail — no amount is committable without its OWN citation. The
job-flow tests mock research + the graph so they need neither Gemini nor Neo4j.
All fixtures use fictional companies (Acme/Globex/Initech).
"""

import asyncio

from app.agents.ma.build import (
    build_acquisition_record,
    build_deal,
    diff_acquisitions,
    valid_source,
)
from app.agents.ma.models import (
    AcquisitionRecord,
    AcquisitionResearch,
    Deal,
    DealResearch,
)

SRC = "https://news.example/acme-buys-globex"
AMT_SRC = "https://filings.example/acme-globex-terms"


def _deal(**kw) -> DealResearch:
    base = dict(acquirer="Acme", target="Globex", source=SRC)
    base.update(kw)
    return DealResearch(**base)


# --- provenance gate: the deal itself (pure) ---------------------------------


def test_deal_without_source_is_dropped():
    """A deal whose existence isn't cited must never survive to the record."""
    raw = _deal(source=None)
    assert build_deal(raw) is None
    record = build_acquisition_record(AcquisitionResearch(company="Acme", deals=[raw]), "Acme")
    assert record.deals == []
    assert not record.has_facts()


def test_deal_requires_both_acquirer_and_target():
    assert build_deal(_deal(acquirer="")) is None
    assert build_deal(_deal(target="   ")) is None


def test_hostile_source_scheme_is_rejected():
    """A javascript:/data: source must not qualify a deal (sources render as links)."""
    assert build_deal(_deal(source="javascript:alert(1)")) is None
    assert valid_source("https://ok.example") and not valid_source("data:text/html,x")


def test_cited_deal_survives_with_its_dates_and_thesis():
    raw = _deal(
        announced_at="2024-01-10",
        closed_at="2024-03-01",
        thesis="Buy the ecosystem foothold.",
    )
    deal = build_deal(raw)
    assert deal is not None
    assert deal.acquirer == "Acme" and deal.target == "Globex"
    assert deal.announced_at == "2024-01-10"
    assert deal.closed_at == "2024-03-01"
    assert deal.thesis == "Buy the ecosystem foothold."
    assert deal.source == SRC


# --- the money guardrail: uncited amounts are dropped (pure) -----------------


def test_uncited_amount_is_dropped_but_the_deal_survives():
    """The core acceptance: an amount with no citation is dropped; the deal stays."""
    raw = _deal(amount="$1.2 billion", currency="USD", amount_source=None)
    deal = build_deal(raw)
    assert deal is not None  # the deal itself is cited, so it commits
    assert deal.amount is None  # ...but the uncited figure is gone
    assert deal.currency is None  # currency travels with the (dropped) amount
    assert deal.amount_source is None


def test_amount_with_hostile_source_is_dropped():
    raw = _deal(amount="$500M", currency="USD", amount_source="javascript:steal()")
    deal = build_deal(raw)
    assert deal is not None and deal.amount is None and deal.currency is None


def test_cited_amount_survives():
    raw = _deal(amount="$1.2 billion", currency="USD", amount_source=AMT_SRC)
    deal = build_deal(raw)
    assert deal.amount == "$1.2 billion"
    assert deal.currency == "USD"
    assert deal.amount_source == AMT_SRC


def test_currency_dropped_when_no_amount():
    """Currency is meaningless without a value — never commit a bare currency."""
    raw = _deal(amount=None, currency="USD")
    deal = build_deal(raw)
    assert deal.amount is None and deal.currency is None


# --- record assembly + dedup + diff (pure) -----------------------------------


def test_duplicate_deals_are_collapsed():
    research = AcquisitionResearch(
        company="Acme",
        deals=[_deal(), _deal(thesis="second mention of the same pair")],
    )
    record = build_acquisition_record(research, "Acme")
    assert len(record.deals) == 1  # keyed on (acquirer, target)


def test_direction_both_made_and_received():
    """Given a subject, deals it made AND deals where it was the target both survive."""
    research = AcquisitionResearch(
        company="Acme",
        deals=[
            DealResearch(acquirer="Acme", target="Globex", source=SRC),  # made
            DealResearch(acquirer="Initech", target="Acme", source=SRC),  # received
        ],
    )
    record = build_acquisition_record(research, "Acme")
    pairs = {(d.acquirer, d.target) for d in record.deals}
    assert pairs == {("Acme", "Globex"), ("Initech", "Acme")}


def test_diff_marks_new_and_changed_deals():
    record = AcquisitionRecord(
        company="Acme",
        deals=[
            Deal(acquirer="Acme", target="Globex", amount="$2B", source=SRC),  # amount changed
            Deal(acquirer="Acme", target="Initech", source=SRC),  # brand new
        ],
    )
    existing = [
        {"acquirer": "Acme", "target": "Globex", "amount": "$1B"},  # already stored, diff amount
    ]
    by_target = {c["deal"]["target"]: c for c in diff_acquisitions(existing, record)}
    assert by_target["Globex"]["status"] == "update"
    assert by_target["Globex"]["old_amount"] == "$1B"
    assert by_target["Initech"]["status"] == "new"


def test_diff_hides_unchanged_deals():
    record = AcquisitionRecord(
        company="Acme",
        deals=[Deal(acquirer="Acme", target="Globex", amount="$1B", source=SRC)],
    )
    existing = [{"acquirer": "Acme", "target": "Globex", "amount": "$1B"}]
    assert diff_acquisitions(existing, record) == []
    assert diff_acquisitions(None, AcquisitionRecord(company="Acme")) == []


# --- durable job flow (mocked research + graph; no Gemini, no Neo4j) ----------


def test_run_acquisition_proposal_job_stores_cited_deals(monkeypatch):
    from app.agents.ma import proposals

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
                _deal(amount="$1.2B", currency="USD", amount_source=AMT_SRC),  # cited amount
                _deal(target="Initech", amount="$9B", amount_source=None),  # amount dropped
            ],
        )

    async def fake_existing(driver, company):
        return []

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "research_acquisitions", fake_research)
    monkeypatch.setattr(proposals, "get_acquisitions", fake_existing)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    asyncio.run(proposals.run_acquisition_proposal_job("aq1"))

    assert saved["status"] == "ready"
    deals = {d["target"]: d for d in saved["record"]["deals"]}
    assert deals["Globex"]["amount"] == "$1.2B"  # cited amount survived
    assert deals["Initech"]["amount"] is None  # uncited amount dropped, deal kept
    assert len(saved["diff"]) == 2  # both are new


def test_commit_refuses_when_not_ready(monkeypatch):
    from app.agents.ma import proposals

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "pending"}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    res = asyncio.run(proposals.commit_acquisition_proposal("aq1"))
    assert "error" in res


def test_commit_refuses_when_no_cited_deals(monkeypatch):
    from app.agents.ma import proposals

    empty = AcquisitionRecord(company="Acme").model_dump()

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "ready", "record": empty}

    called = {"upsert": False}

    async def fake_upsert(driver, record):
        called["upsert"] = True
        return {"action": "written", "deals": 0}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals, "upsert_acquisitions", fake_upsert)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    res = asyncio.run(proposals.commit_acquisition_proposal("aq1"))
    assert "error" in res
    assert called["upsert"] is False  # nothing written


def test_commit_writes_and_flips_status(monkeypatch):
    from app.agents.ma import proposals

    record = AcquisitionRecord(
        company="Acme",
        deals=[Deal(acquirer="Acme", target="Globex", source=SRC)],
    ).model_dump()
    updates: dict = {}

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "ready", "record": record}

    async def fake_update_job(job_id, data, status=None):
        updates.update(data)
        updates["status"] = status

    async def fake_upsert(driver, rec):
        return {"company": rec.company, "action": "written", "deals": len(rec.deals)}

    monkeypatch.setattr(proposals.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(proposals.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(proposals, "upsert_acquisitions", fake_upsert)
    monkeypatch.setattr(proposals, "get_driver", lambda: None)

    res = asyncio.run(proposals.commit_acquisition_proposal("aq1"))
    assert res["committed"] == "Acme" and res["deals"] == 1
    assert updates["committed"] is True
    assert updates["status"] == "committed"  # prunable past retention after commit
