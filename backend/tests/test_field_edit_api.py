"""Route wiring for the inline field-edit endpoint `PATCH /companies/{name}/field`
(#149). No database here: the write helper is monkeypatched, so the wiring and the
422/404 translations are checked deterministically. The pure validation itself is
covered in test_field_edit.py. Fictional names only (public repo)."""

from fastapi.testclient import TestClient

from app.api import routes
from app.graph.field_edit import ValidatedEdit
from app.main import app


def _patch_apply(monkeypatch, returns=True):
    seen = {}

    async def fake_apply(driver, name, edit):
        seen["name"] = name
        seen["edit"] = edit
        return returns

    monkeypatch.setattr(routes.field_edit, "apply_field_edit", fake_apply)
    return seen


def test_saves_headcount_with_source(monkeypatch):
    seen = _patch_apply(monkeypatch, returns=True)
    with TestClient(app) as client:
        resp = client.patch(
            "/companies/Acme%20Corp/field",
            json={
                "field": "headcount",
                "value": "1,200",
                "source_url": "https://acme.example/about",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "name": "Acme Corp",
        "field": "headcount",
        "value": 1200,
        "source_url": "https://acme.example/about",
    }
    assert seen["name"] == "Acme Corp"
    assert isinstance(seen["edit"], ValidatedEdit)
    assert seen["edit"].value == 1200


def test_year_founded_without_source_ok(monkeypatch):
    _patch_apply(monkeypatch, returns=True)
    with TestClient(app) as client:
        resp = client.patch(
            "/companies/Globex/field",
            json={"field": "yearFounded", "value": 1994},
        )
    assert resp.status_code == 200
    assert resp.json()["value"] == 1994
    assert resp.json()["source_url"] is None


def test_headcount_missing_source_is_422(monkeypatch):
    called = {"n": 0}

    async def fake_apply(driver, name, edit):
        called["n"] += 1
        return True

    monkeypatch.setattr(routes.field_edit, "apply_field_edit", fake_apply)
    with TestClient(app) as client:
        resp = client.patch(
            "/companies/Acme/field",
            json={"field": "headcount", "value": 100},
        )
    assert resp.status_code == 422
    assert called["n"] == 0  # never reached the write


def test_bad_source_url_is_422(monkeypatch):
    _patch_apply(monkeypatch, returns=True)
    with TestClient(app) as client:
        resp = client.patch(
            "/companies/Acme/field",
            json={"field": "funding", "value": "Series B", "source_url": "not-a-url"},
        )
    assert resp.status_code == 422


def test_off_allowlist_field_is_422(monkeypatch):
    _patch_apply(monkeypatch, returns=True)
    with TestClient(app) as client:
        resp = client.patch(
            "/companies/Acme/field",
            json={"field": "kind", "value": "isv", "source_url": "https://x.example"},
        )
    assert resp.status_code == 422


def test_unknown_company_is_404(monkeypatch):
    _patch_apply(monkeypatch, returns=False)
    with TestClient(app) as client:
        resp = client.patch(
            "/companies/Nope/field",
            json={"field": "yearFounded", "value": 2000},
        )
    assert resp.status_code == 404
