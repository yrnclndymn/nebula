"""The shared scan→review→commit ceremony (jobs.enqueue_scan_job /
execute_scan_job / get_ready_job / mark_committed), the two flows that collapse
onto it (resolution, classification), and the routes-level 404 guards
(_found_or_404 / _ok_or_404).

Everything runs against an in-memory job store (no Neo4j, no network); the
graph mutations the flows delegate to are mocked. Fictional company names only.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agents.assistant import classification, resolution
from app.graph import entity_resolution as er
from app.graph import jobs
from app.main import app

# --- in-memory job store ------------------------------------------------------


@pytest.fixture
def job_store(monkeypatch):
    """Replace the graph-backed job CRUD with a dict, mirroring the real
    semantics (status kept on the node, update may or may not flip it)."""
    store: dict[str, dict] = {}

    async def fake_create(job_id: str, job_type: str, data: dict) -> None:
        store[job_id] = {**data, "type": job_type, "status": data.get("status", "pending")}

    async def fake_get(job_id: str) -> dict | None:
        job = store.get(job_id)
        return dict(job) if job else None

    async def fake_update(job_id: str, data: dict, status: str | None = None) -> None:
        current = store[job_id]
        store[job_id] = {**data, "type": current["type"], "status": status or current["status"]}

    async def fake_enqueue(job_id: str, delay: float = 0.0) -> None:
        return None

    monkeypatch.setattr(jobs, "create_job", fake_create)
    monkeypatch.setattr(jobs, "get_job", fake_get)
    monkeypatch.setattr(jobs, "update_job", fake_update)
    monkeypatch.setattr(jobs, "enqueue", fake_enqueue)
    return store


# --- the ceremony helpers -----------------------------------------------------


def test_enqueue_scan_job_creates_pending_job_and_poll_handle(job_store):
    out = asyncio.run(jobs.enqueue_scan_job("resolution", {"clusters": [], "stub_count": 0}))
    assert out["status"] == "scanning in the background"
    job = job_store[out["job_id"]]
    assert job["type"] == "resolution"
    assert job["status"] == "pending"
    assert job["clusters"] == []


def test_execute_scan_job_merges_result_and_marks_ready(job_store):
    async def scenario():
        handle = await jobs.enqueue_scan_job("classification", {"candidates": []})

        async def scan(_job: dict) -> dict:
            return {"candidates": ["Acme __pytest__"], "stub_count": 1}

        await jobs.execute_scan_job(handle["job_id"], scan)
        return job_store[handle["job_id"]]

    job = asyncio.run(scenario())
    assert job["status"] == "ready"
    assert job["candidates"] == ["Acme __pytest__"]
    assert job["stub_count"] == 1


def test_execute_scan_job_stores_error_and_marks_errored(job_store):
    async def scenario():
        handle = await jobs.enqueue_scan_job("classification", {"candidates": []})

        async def scan(_job: dict) -> dict:
            raise RuntimeError("scan blew up")

        await jobs.execute_scan_job(handle["job_id"], scan)
        return job_store[handle["job_id"]]

    job = asyncio.run(scenario())
    assert job["status"] == "error"
    assert job["error"] == "scan blew up"


def test_execute_scan_job_vanished_job_is_a_noop(job_store):
    ran: list[str] = []

    async def scan(_job: dict) -> dict:
        ran.append("x")
        return {}

    asyncio.run(jobs.execute_scan_job("nope", scan))
    assert ran == []  # the scan never ran; nothing was created either
    assert "nope" not in job_store


def test_get_ready_job_gates_on_existence_and_status(job_store):
    async def scenario():
        handle = await jobs.enqueue_scan_job("resolution", {"clusters": []})
        job_id = handle["job_id"]
        results = {"unknown": await jobs.get_ready_job("nope")}
        results["pending"] = await jobs.get_ready_job(job_id)  # still pending

        async def scan(_job: dict) -> dict:
            return {"clusters": []}

        await jobs.execute_scan_job(job_id, scan)
        results["ready"] = await jobs.get_ready_job(job_id)
        await jobs.mark_committed(job_id, results["ready"])
        results["committed"] = await jobs.get_ready_job(job_id)
        return results

    results = asyncio.run(scenario())
    assert results["unknown"] is None
    assert results["pending"] is None
    assert results["ready"] is not None  # the one reviewable state
    assert results["committed"] is None  # double-POST is rejected


# --- the flows on top of the ceremony -----------------------------------------


def test_classification_flow_end_to_end(job_store, monkeypatch):
    candidates = [{"name": "Globex Bank __pytest__", "mentions": 2}]
    classified: list[list[str]] = []

    async def fake_candidates(_driver):
        return candidates

    async def fake_classify(_driver, names: list[str]) -> int:
        classified.append(names)
        return len(names)

    monkeypatch.setattr(er, "list_client_stub_candidates", fake_candidates)
    monkeypatch.setattr(er, "classify_as_client", fake_classify)
    monkeypatch.setattr(classification, "get_driver", lambda: None)

    async def scenario():
        handle = await classification.enqueue_classification()
        job_id = handle["job_id"]
        await classification.execute_classification_job(job_id)
        ready = await classification.get_classification(job_id)
        first = await classification.commit_classification(job_id, ["Globex Bank __pytest__"])
        second = await classification.commit_classification(job_id, ["Globex Bank __pytest__"])
        return ready, first, second

    ready, first, second = asyncio.run(scenario())
    assert ready["status"] == "ready"
    assert ready["candidates"] == candidates
    assert first == {"classified": 1}
    assert classified == [["Globex Bank __pytest__"]]  # only the approved names
    assert "error" in second  # committed job no longer passes the ready-guard


def test_commit_classification_requires_a_ready_job(job_store, monkeypatch):
    monkeypatch.setattr(classification, "get_driver", lambda: None)
    out = asyncio.run(classification.commit_classification("nope", ["Acme __pytest__"]))
    assert out == {"error": "classification job not found or not ready"}


def test_resolution_scan_proposes_clusters_with_edge_counts(job_store, monkeypatch):
    stubs = [
        {"name": "Acme __pytest__", "aliases": [], "edges": 0},
        {"name": "Acme Inc __pytest__", "aliases": [], "edges": 3},
        {"name": "Unrelated Co __pytest__", "aliases": [], "edges": 1},
    ]

    async def fake_stubs(_driver):
        return stubs

    monkeypatch.setattr(er, "list_stub_companies", fake_stubs)
    monkeypatch.setattr(resolution, "get_driver", lambda: None)

    async def scenario():
        handle = await resolution.enqueue_resolution()
        await resolution.execute_resolution_job(handle["job_id"])
        return await resolution.get_resolution(handle["job_id"])

    job = asyncio.run(scenario())
    assert job["status"] == "ready"
    assert job["stub_count"] == 3
    [cluster] = job["clusters"]
    # The real (pure) heuristics ran: the two Acme variants clustered, and the
    # best-connected member was promoted to canonical over the descriptive pick.
    assert cluster["canonical"] == "Acme Inc __pytest__"
    assert {m["name"] for m in cluster["members"]} == {"Acme __pytest__", "Acme Inc __pytest__"}
    assert {m["edges"] for m in cluster["members"]} == {0, 3}


def test_commit_backfill_requires_ready_and_commits_once(job_store, monkeypatch):
    from app.agents.assistant import backfill

    written: list[tuple] = []
    cited: list[tuple] = []

    async def fake_set(_driver, company, key, value):
        written.append((company, key, value))

    async def fake_cite(_driver, company, key, _value, source):
        cited.append((company, key, source))

    monkeypatch.setattr(backfill.queries, "set_custom_field", fake_set)
    monkeypatch.setattr(backfill.queries, "cite", fake_cite)
    monkeypatch.setattr(backfill, "get_driver", lambda: None)

    async def scenario():
        job_id = "bf1"
        await jobs.create_job(
            job_id,
            "backfill",
            {
                "job_id": job_id,
                "status": "pending",
                "field": {"name": "serviceLines", "label": "Service Lines", "type": "list"},
                "rows": [],
            },
        )
        results = {"pending": await backfill.commit_backfill(job_id)}

        rows = [
            {
                "company": "Acme __pytest__",
                "value": ["consulting"],
                "source": "https://acme.example/about",
                "committed": False,
            },
            {"company": "Globex __pytest__", "value": [], "source": "", "committed": False},
            {"company": "Initech __pytest__", "value": ["tools"], "source": "", "committed": False},
        ]
        job = await jobs.get_job(job_id)
        await jobs.update_job(job_id, {**job, "rows": rows}, status="ready")

        results["first"] = await backfill.commit_backfill(job_id, [r["company"] for r in rows])
        results["second"] = await backfill.commit_backfill(job_id, ["Initech __pytest__"])
        results["unknown"] = await backfill.commit_backfill("nope")
        return results

    results = asyncio.run(scenario())
    assert "error" in results["pending"]  # a still-running job is not committable
    assert results["first"] == {"committed": 2}  # the empty-value row was skipped
    assert written == [
        ("Acme __pytest__", "serviceLines", ["consulting"]),
        ("Initech __pytest__", "serviceLines", ["tools"]),
    ]
    assert cited == [("Acme __pytest__", "serviceLines", "https://acme.example/about")]
    assert "error" in results["second"]  # double-POST rejected, rows not re-written
    assert "error" in results["unknown"]


# --- local-mode enqueue triggers the executor ---------------------------------


def test_local_enqueue_runs_the_job_inline(monkeypatch):
    # NOT job_store — that fixture stubs out jobs.enqueue, which is the very
    # function under test here.
    executed: list[str] = []

    async def fake_execute(job_id: str) -> None:
        executed.append(job_id)

    monkeypatch.setattr(jobs, "execute_job", fake_execute)
    monkeypatch.setattr(jobs.settings, "job_mode", "local")

    async def scenario():
        await jobs.enqueue("j1")
        for _ in range(5):  # let the fire-and-forget task run to completion
            await asyncio.sleep(0)
        return executed

    assert asyncio.run(scenario()) == ["j1"]


# --- routes: the shared 404 guards over every review surface ------------------

# (path, module holding the backing callable, attribute, expected 404 detail)
STATUS_ENDPOINTS = [
    ("/proposals/x1", "app.api.routes", "get_proposal", "unknown proposal"),
    ("/backfill/x1", "app.api.routes", "get_backfill", "unknown back-fill job"),
    ("/resolution/x1", "app.api.routes", "get_resolution", "unknown resolution job"),
    ("/classification/x1", "app.api.routes", "get_classification", "unknown classification job"),
    ("/discovery/x1", "app.api.routes", "get_discovery", "unknown discovery job"),
    ("/signals/capture/x1", "app.api.routes", "get_signal_capture", "unknown signal-capture job"),
    ("/news/capture/x1", "app.api.routes", "get_news_capture", "unknown news-capture job"),
    (
        "/people/enrich/x1",
        "app.agents.people.proposals",
        "get_person_proposal",
        "unknown person proposal",
    ),
    (
        "/companies/acquisitions/x1",
        "app.agents.deals.proposals",
        "get_acquisition_proposal",
        "unknown acquisition proposal",
    ),
    ("/people/x1", "app.graph.person_expertise", "get_person", "unknown person"),
]


@pytest.mark.parametrize("path,module,attr,detail", STATUS_ENDPOINTS)
def test_status_endpoints_404_when_unknown_and_return_the_job(
    monkeypatch, path, module, attr, detail
):
    import importlib

    mod = importlib.import_module(module)

    async def fake_missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mod, attr, fake_missing)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["detail"] == detail

    async def fake_found(*_args, **_kwargs):
        return {"job_id": "x1", "status": "ready"}

    monkeypatch.setattr(mod, attr, fake_found)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "x1"


# (method, path, body, module, attribute)
ACTION_ENDPOINTS = [
    ("post", "/proposals/commit", {"proposal_id": "x1"}, "app.api.routes", "commit_proposal"),
    ("post", "/backfill/x1/commit", {}, "app.api.routes", "commit_backfill"),
    ("post", "/resolution/x1/commit", {"decisions": []}, "app.api.routes", "commit_resolution"),
    (
        "post",
        "/classification/x1/commit",
        {"names": []},
        "app.api.routes",
        "commit_classification",
    ),
    ("post", "/companies/Acme/discover", None, "app.api.routes", "enqueue_discovery"),
    ("post", "/discovery/x1/research", {"names": []}, "app.api.routes", "research_candidates"),
    ("post", "/companies/Acme/signals/capture", None, "app.api.routes", "enqueue_signal_capture"),
    ("post", "/companies/Acme/news/capture", None, "app.api.routes", "enqueue_news_capture"),
    (
        "post",
        "/people/enrich",
        {"name": "Jo Doe", "company": "Acme"},
        "app.agents.people.proposals",
        "propose_person",
    ),
    (
        "post",
        "/people/enrich/x1/commit",
        None,
        "app.agents.people.proposals",
        "commit_person_proposal",
    ),
    (
        "post",
        "/companies/acquisitions/research",
        {"company": "Acme"},
        "app.agents.deals.proposals",
        "propose_acquisitions",
    ),
    (
        "post",
        "/companies/acquisitions/x1/commit",
        None,
        "app.agents.deals.proposals",
        "commit_acquisition_proposal",
    ),
    (
        "post",
        "/people/x1/expertise",
        None,
        "app.graph.person_expertise",
        "enqueue_person_expertise",
    ),
]


@pytest.mark.parametrize("method,path,body,module,attr", ACTION_ENDPOINTS)
def test_action_endpoints_translate_error_results_to_404(
    monkeypatch, method, path, body, module, attr
):
    import importlib

    mod = importlib.import_module(module)

    async def fake_error(*_args, **_kwargs):
        return {"error": "it went sideways"}

    monkeypatch.setattr(mod, attr, fake_error)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = getattr(client, method)(path, json=body)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "it went sideways"

    async def fake_ok(*_args, **_kwargs):
        return {"job_id": "x1"}

    monkeypatch.setattr(mod, attr, fake_ok)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = getattr(client, method)(path, json=body)
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "x1"}


@pytest.mark.parametrize(
    "path,attr",
    [
        ("/resolution/scan", "enqueue_resolution"),
        ("/classification/scan", "enqueue_classification"),
    ],
)
def test_scan_endpoints_pass_the_poll_handle_through(monkeypatch, path, attr):
    # Scan starts never error (there is nothing to validate), so the endpoints
    # are plain passthroughs of the standard poll handle.
    from app.api import routes

    async def fake_enqueue():
        return {"job_id": "x1", "status": "scanning in the background"}

    monkeypatch.setattr(routes, attr, fake_enqueue)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(path)
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "x1"
