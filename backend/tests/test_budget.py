"""Per-run budget caps (app/budget.py) + backfill's graceful budget stop.

Pure unit tests — no Neo4j, no network. The backfill tests monkeypatch the graph
+ extraction seams so the runner's stop-and-keep-partial-rows logic can be
exercised deterministically.
"""

import asyncio

import pytest

from app import budget as budget_mod
from app.agents.assistant import backfill
from app.budget import (
    Budget,
    BudgetExhausted,
    budget_for,
    charge_company,
    charge_llm,
    charge_page,
    charge_search,
    current_budget,
    use_budget,
)


# --- counters + caps -----------------------------------------------------------


def test_counter_trips_at_the_limit():
    b = Budget(max_pages=2)
    b.charge_page()  # 1
    b.charge_page()  # 2 — reaches the cap
    assert b.pages == 2
    with pytest.raises(BudgetExhausted) as exc:
        b.charge_page()  # would be the 3rd — refused
    assert exc.value.limit == "pages"
    assert exc.value.cap == 2
    assert exc.value.count == 2
    assert b.pages == 2  # a refused charge does not increment


def test_none_limit_is_uncapped_but_counted():
    b = Budget(max_llm_calls=None)
    for _ in range(50):
        b.charge_llm()
    assert b.llm_calls == 50  # counted for observability, never raises


def test_each_dimension_is_independent():
    b = Budget(max_pages=1, max_searches=1, max_llm_calls=1, max_companies=1)
    b.charge_page()
    b.charge_search()
    b.charge_llm()
    b.charge_company()
    for charge in (b.charge_page, b.charge_search, b.charge_llm, b.charge_company):
        with pytest.raises(BudgetExhausted):
            charge()
    assert b.usage() == {"pages": 1, "searches": 1, "llm_calls": 1, "companies": 1}


# --- ContextVar plumbing: no budget = unlimited (interactive paths untouched) ---


def test_module_charges_are_noops_without_a_budget():
    assert current_budget() is None
    # None installed → every helper is a silent no-op (interactive chat / enrich).
    charge_page()
    charge_search()
    charge_llm()
    charge_company()
    assert current_budget() is None


def test_use_budget_installs_and_resets():
    b = Budget(max_pages=1)
    assert current_budget() is None
    with use_budget(b):
        assert current_budget() is b
        charge_page()
        with pytest.raises(BudgetExhausted):
            charge_page()
    assert current_budget() is None  # reset on exit even after a raise


def test_use_budget_none_is_explicitly_unlimited():
    with use_budget(None):
        charge_page()  # no-op
        assert current_budget() is None


# --- budget_for: config defaults + per-job override ----------------------------


def test_budget_for_reads_config_defaults():
    b = budget_for("backfill")
    assert b is not None
    # Matches the config default block (pages/llm/companies capped, no search).
    assert b.max_companies == 25
    assert b.max_pages == 60
    assert b.max_searches == 0


def test_budget_for_unknown_type_is_unlimited():
    assert budget_for("no_such_job_type") is None


def test_payload_override_wins_and_can_uncap():
    b = budget_for("backfill", {"max_companies": 3, "max_pages": None})
    assert b.max_companies == 3  # override replaces the default
    assert b.max_pages is None  # explicit None uncaps that dimension
    assert b.max_llm_calls == 40  # untouched default preserved


def test_overrides_alone_build_a_budget_for_an_unconfigured_type():
    b = budget_for("ambient_x", {"max_pages": 5})
    assert b is not None and b.max_pages == 5


# --- tool helpers charge before spending ---------------------------------------


def test_web_search_charges_before_network():
    from app.tools.web import web_search

    # A zero search cap must refuse before DDGS is ever constructed (no network).
    with use_budget(Budget(max_searches=0)), pytest.raises(BudgetExhausted) as exc:
        web_search("anything")
    assert exc.value.limit == "searches"


def test_generate_with_retry_charges_llm_before_calling_client():
    from app.genai_retry import generate_with_retry

    class _Models:
        def __init__(self):
            self.calls = 0

        async def generate_content(self, **kwargs):
            self.calls += 1
            return "ok"

    class _Client:
        def __init__(self):
            self.aio = type("Aio", (), {"models": _Models()})()

    async def scenario():
        client = _Client()
        with use_budget(Budget(max_llm_calls=1)):
            first = await generate_with_retry(client)  # charged → count 1, calls client
            raised = False
            try:
                await generate_with_retry(client)  # over cap → refused before client
            except BudgetExhausted:
                raised = True
        return first, client.aio.models.calls, raised

    first, calls, raised = asyncio.run(scenario())
    assert first == "ok"
    assert calls == 1  # the refused second call never reached the client
    assert raised is True


# --- backfill: graceful stop keeps partial rows --------------------------------


def _wire_backfill(monkeypatch, companies, extract, budget_override):
    """Stub the graph + extraction seams so run_backfill_job runs without Neo4j.
    Returns the in-memory job dict that the runner mutates."""
    field_def = {
        "name": "serviceLines",
        "label": "Service Lines",
        "type": "list",
        "description": "the firm's service lines",
        "appliesToKind": "service_provider",
    }
    store = {
        "job_id": "jb",
        "field_name": "serviceLines",
        "field": {"name": "serviceLines", "label": "Service Lines", "type": "list"},
        "total": len(companies),
        "done": 0,
        "rows": [],
        "budget": budget_override,
    }

    async def fake_get_job(job_id):
        return dict(store)

    async def fake_update_job(job_id, data, status=None):
        store.clear()
        store.update(data)
        if status is not None:
            store["status"] = status

    async def fake_list_field_defs(driver):
        return [field_def]

    async def fake_applicable(*args, **kwargs):
        return companies

    monkeypatch.setattr(backfill.jobs, "get_job", fake_get_job)
    monkeypatch.setattr(backfill.jobs, "update_job", fake_update_job)
    monkeypatch.setattr(backfill.queries, "list_field_defs", fake_list_field_defs)
    monkeypatch.setattr(backfill, "_applicable_companies", fake_applicable)
    monkeypatch.setattr(backfill, "extract_field", extract)
    monkeypatch.setattr(backfill, "get_driver", lambda: object())
    return store


def test_backfill_stops_at_companies_cap_keeping_rows(monkeypatch):
    companies = [{"name": f"Co{i}", "website": f"https://co{i}.example"} for i in range(5)]

    async def extract(website, label, description, field_type):
        return {"value": ["x"], "source": website}

    store = _wire_backfill(monkeypatch, companies, extract, {"max_companies": 2})
    asyncio.run(backfill.run_backfill_job("jb"))

    assert store["status"] == "ready"  # graceful stop, not an error
    assert len(store["rows"]) == 2  # only the companies within budget
    assert [r["company"] for r in store["rows"]] == ["Co0", "Co1"]
    marker = store["budget_exhausted"]
    assert marker["limit"] == "companies"
    assert marker["cap"] == 2
    assert marker["done"] == 2
    assert marker["total"] == 5


def test_backfill_stops_on_midloop_page_cap_keeping_prior_rows(monkeypatch):
    companies = [{"name": f"Co{i}", "website": f"https://co{i}.example"} for i in range(5)]
    calls = {"n": 0}

    async def extract(website, label, description, field_type):
        calls["n"] += 1
        if calls["n"] == 2:  # the 2nd company trips a page cap deep in the tools
            raise BudgetExhausted("pages", 60, 60)
        return {"value": ["x"], "source": website}

    # High companies cap so the page cap is what stops the run.
    store = _wire_backfill(monkeypatch, companies, extract, {"max_companies": 25})
    asyncio.run(backfill.run_backfill_job("jb"))

    assert store["status"] == "ready"
    assert len(store["rows"]) == 1  # the completed first company is kept
    assert store["rows"][0]["company"] == "Co0"
    assert store["budget_exhausted"]["limit"] == "pages"


def test_backfill_unlimited_when_budget_uncapped(monkeypatch):
    """All-None caps = unlimited: backwards compatible, every company processed."""
    companies = [{"name": f"Co{i}", "website": f"https://co{i}.example"} for i in range(5)]

    async def extract(website, label, description, field_type):
        return {"value": ["x"], "source": website}

    uncapped = {
        "max_pages": None,
        "max_searches": None,
        "max_llm_calls": None,
        "max_companies": None,
    }
    store = _wire_backfill(monkeypatch, companies, extract, uncapped)
    asyncio.run(backfill.run_backfill_job("jb"))

    assert store["status"] == "ready"
    assert len(store["rows"]) == 5
    assert "budget_exhausted" not in store


def test_budget_module_importable():
    # Guards the import path used by the tool helpers.
    assert budget_mod.LIMITS == ("pages", "searches", "llm_calls", "companies")
