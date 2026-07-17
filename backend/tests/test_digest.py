"""Weekly digest (#51).

Pure tests (window maths, delta grouping/shaping, rendering, LLM fail-safe) run
without a DB. A graph-integration test exercises the delta queries + storage +
the runner end to end, skip-guarded on Neo4j (CI is the arbiter — see CLAUDE.md).
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.graph import digest, jobs
from app.graph.driver import check_connectivity, close_driver, get_driver

PREFIX = "__pytest_digest__"
COMPANIES = ["Acme __pytest_dg__", "Globex __pytest_dg__"]


# --- pure: window ------------------------------------------------------------


def test_week_window_is_trailing_seven_days():
    now = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
    w = digest.week_window(now)
    assert w.end == now
    assert (w.end - w.start).days == 7
    assert w.week_of == "2026-07-05"


# --- pure: build_payload grouping/totals/caps --------------------------------


def _window():
    return digest.week_window(datetime(2026, 7, 12, tzinfo=timezone.utc))


def test_build_payload_groups_signals_by_company_and_counts():
    signal_rows = [
        {
            "title": "A raises",
            "url": "https://x/1",
            "kind": "news",
            "capturedAt": "2026-07-10",
            "companies": [COMPANIES[0]],
        },
        {
            "title": "A ships",
            "url": "https://x/2",
            "kind": "blog",
            "capturedAt": "2026-07-09",
            "companies": [COMPANIES[0], COMPANIES[1]],
        },
    ]
    payload = digest.build_payload(_window(), signal_rows, [], [])
    groups = {g["company"]: g for g in payload["newSignalsByCompany"]}
    assert groups[COMPANIES[0]]["count"] == 2
    assert groups[COMPANIES[1]]["count"] == 1
    # totals: two signal rows, two companies with new signals.
    assert payload["totals"]["newSignals"] == 2
    assert payload["totals"]["companiesWithNewSignals"] == 2


def test_build_payload_caps_listed_signals_but_keeps_true_count():
    signal_rows = [
        {
            "title": f"s{i}",
            "url": f"https://x/{i}",
            "kind": "news",
            "capturedAt": "2026-07-10",
            "companies": [COMPANIES[0]],
        }
        for i in range(digest._MAX_SIGNALS_PER_COMPANY + 5)
    ]
    payload = digest.build_payload(_window(), signal_rows, [], [])
    group = payload["newSignalsByCompany"][0]
    assert group["count"] == digest._MAX_SIGNALS_PER_COMPANY + 5  # true total
    assert len(group["signals"]) == digest._MAX_SIGNALS_PER_COMPANY  # listed detail capped


def test_build_payload_unattributed_signal_gets_a_bucket():
    payload = digest.build_payload(
        _window(),
        [{"title": "t", "url": None, "kind": "news", "capturedAt": "2026-07-10", "companies": []}],
        [],
        [],
    )
    assert payload["newSignalsByCompany"][0]["company"] == "(unattributed)"


def test_build_payload_notable_changes_only_include_jobs_with_outcome():
    job_rows = [
        {"type": "signal_capture", "outcome": "found 3 items", "createdAt": "2026-07-10"},
        {"type": "proposal", "outcome": None, "createdAt": "2026-07-11"},
    ]
    payload = digest.build_payload(_window(), [], [], job_rows)
    assert payload["totals"]["notableChanges"] == 1
    assert payload["notableChanges"][0]["outcome"] == "found 3 items"


def test_housekeeping_types_exclude_all_scheduled_orchestrators():
    """Every scheduled job type whose outcome is self-referential housekeeping —
    including #36's signal_refresh fan-out orchestrator — must be excluded from
    "notable changes" (#93 review): the per-company capture outcomes are the news."""
    from app.graph.schedules import SCHEDULES

    for sched in SCHEDULES:
        assert sched.job_type in digest._HOUSEKEEPING_TYPES


def test_has_deltas_false_on_empty_week():
    payload = digest.build_payload(_window(), [], [], [])
    assert digest.has_deltas(payload) is False
    assert "quiet week" in digest.render_summary(payload)


def test_render_summary_names_counts_when_deltas_present():
    payload = digest.build_payload(
        _window(),
        [
            {
                "title": "t",
                "url": "https://x/1",
                "kind": "news",
                "capturedAt": "2026-07-10",
                "companies": [COMPANIES[0]],
            }
        ],
        [{"name": COMPANIES[1], "topics": ["AI"], "updatedAt": "2026-07-10"}],
        [],
    )
    text = digest.render_summary(payload)
    assert "1 new signal" in text
    assert "newly-researched" in text


def test_summary_prompt_is_grounded_in_the_facts():
    payload = digest.build_payload(
        _window(),
        [
            {
                "title": "secret crawled title",
                "url": "https://x/1",
                "kind": "news",
                "capturedAt": "2026-07-10",
                "companies": [COMPANIES[0]],
            }
        ],
        [],
        [],
    )
    prompt = digest.summary_prompt(payload)
    # Company name + count are handed to the model; the untrusted crawled TITLE is not.
    assert COMPANIES[0] in prompt
    assert "secret crawled title" not in prompt


# --- LLM fail-safe (optional garnish must never fail the digest) -------------


def test_summarise_deltas_falls_back_to_rendering_on_llm_error(monkeypatch):
    payload = digest.build_payload(
        _window(),
        [
            {
                "title": "t",
                "url": "https://x/1",
                "kind": "news",
                "capturedAt": "2026-07-10",
                "companies": [COMPANIES[0]],
            }
        ],
        [],
        [],
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(digest, "generate_with_retry", boom)
    out = asyncio.run(digest.summarise_deltas(payload))
    assert out == digest.render_summary(payload)


def test_summarise_deltas_uses_model_text_on_success(monkeypatch):
    payload = digest.build_payload(
        _window(),
        [
            {
                "title": "t",
                "url": "https://x/1",
                "kind": "news",
                "capturedAt": "2026-07-10",
                "companies": [COMPANIES[0]],
            }
        ],
        [],
        [],
    )

    class _Resp:
        text = "One company had news this week."

    async def fake(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr(digest.genai, "Client", lambda: object())
    monkeypatch.setattr(digest, "generate_with_retry", fake)
    out = asyncio.run(digest.summarise_deltas(payload))
    assert out == "One company had news this week."


# --- graph integration (skip-guarded) ----------------------------------------


def test_digest_collects_deltas_stores_and_lists(monkeypatch):
    """Seed a week's deltas, run the digest job, and confirm it collects/groups
    them, stores a browsable Digest node, and the LLM step fails safe."""

    # No LLM in the integration test: force the deterministic rendering.
    async def no_llm(payload):
        return digest.render_summary(payload)

    monkeypatch.setattr(digest, "summarise_deltas", no_llm)

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        acme, globex = COMPANIES
        async with driver.session() as session:
            await _clean(session)
            # New signals this week (capturedAt inside the window).
            await session.run(
                "UNWIND $rows AS r CREATE (s:Signal {url: r.url, title: r.title, kind: r.kind, "
                "capturedAt: datetime() - duration({days: r.age})})",
                rows=[
                    {"url": f"{PREFIX}n0", "title": "A raises", "kind": "news", "age": 1},
                    {"url": f"{PREFIX}n1", "title": "A ships", "kind": "blog", "age": 2},
                    {
                        "url": f"{PREFIX}old",
                        "title": "stale",
                        "kind": "news",
                        "age": 40,
                    },  # outside window
                ],
            )
            await session.run(
                "MATCH (s:Signal) WHERE s.url IN [$a, $b] "
                "MERGE (c:Company {name: $acme}) MERGE (c)-[:MENTIONED_IN]->(s)",
                a=f"{PREFIX}n0",
                b=f"{PREFIX}n1",
                acme=acme,
            )
            # A newly-researched company (tagged to a topic, updated this week).
            await session.run(
                "MERGE (c:Company {name: $globex}) SET c.updatedAt = datetime() "
                "MERGE (t:Topic {name: $topic}) MERGE (c)-[:TAGGED_AS]->(t)",
                globex=globex,
                topic=f"{PREFIX}topic",
            )
            # A notable completed job this week (carries an outcome).
            await session.run(
                "CREATE (:Job {id: $id, type: 'signal_capture', status: 'done', "
                "dataJson: $data, createdAt: datetime()})",
                id=f"{PREFIX}job",
                data=json.dumps({"outcome": "found 2 items about A"}),
            )

        due = await digest.digest_due(driver)

        job_id = f"{PREFIX}run"
        await jobs.create_job(job_id, "digest", {"status": "pending"})
        await digest.execute_digest_job(job_id)
        execute_job = await jobs.get_job(job_id)

        listed = await digest.list_digests(driver, limit=10)
        mine = [d for d in listed if d["id"] == execute_job.get("digestId")]
        detail = await digest.get_digest(driver, execute_job.get("digestId"))

        async with driver.session() as session:
            await session.run(
                "MATCH (d:Digest {id: $id}) DETACH DELETE d", id=execute_job.get("digestId")
            )
            await _clean(session)
            await session.run("MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=PREFIX)
        await close_driver()
        return due, execute_job, mine, detail

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    due, execute_job, mine, detail = out

    assert due is True
    assert execute_job["status"] == "done"
    assert execute_job["digestId"]
    # Stored + browsable in the history list.
    assert len(mine) == 1
    payload = detail["payload"]
    # Two in-window signals grouped under Acme; the stale one is excluded.
    groups = {g["company"]: g for g in payload["newSignalsByCompany"]}
    assert groups[COMPANIES[0]]["count"] == 2
    assert payload["totals"]["newSignals"] == 2
    # The newly-researched company and the notable job both surfaced.
    assert any(r["name"] == COMPANIES[1] for r in payload["newlyResearched"])
    assert any("found 2 items" in c["outcome"] for c in payload["notableChanges"])


async def _clean(session):
    await session.run("MATCH (s:Signal) WHERE s.url STARTS WITH $p DETACH DELETE s", p=PREFIX)
    await session.run("MATCH (t:Topic) WHERE t.name STARTS WITH $p DETACH DELETE t", p=PREFIX)
    await session.run("MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=COMPANIES)


def test_digest_budget_configured():
    """The digest job type is budgeted: no crawling/searching, a small LLM cap."""
    b = settings.job_budgets["digest"]
    assert b["max_pages"] == 0
    assert b["max_searches"] == 0
    assert b["max_llm_calls"] >= 1
