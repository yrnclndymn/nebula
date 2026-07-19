"""Unit tests for the HQ tidy task's provider seam (#8 review finding).

The fire-and-forget `_run_tidy` used to construct `genai.Client()` unconditionally:
on a non-gemini deployment without a Gemini key that crashed BEFORE the per-batch
try/except, silently no-oping the whole tidy. The client must only be built on the
gemini path; the litellm path runs with `client=None` (ignored by `llm.generate`).
"""

import asyncio

from app.agents.assistant import tidy_hq
from app.agents.assistant.tidy_hq import _HQ, _HQBatch
from app.config import settings


def test_run_tidy_litellm_path_builds_no_gemini_client(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "example-provider")
    monkeypatch.setattr(settings, "llm_model", "example/model")

    class _NoClient:
        def __init__(self):
            raise AssertionError("genai.Client must not be constructed on the litellm path")

    monkeypatch.setattr(tidy_hq.genai, "Client", _NoClient)
    monkeypatch.setattr(tidy_hq, "get_driver", lambda: object())

    async def fake_companies(driver):
        return [{"name": "Acme", "hq": "London, United Kingdom"}]

    monkeypatch.setattr(tidy_hq.queries, "companies_with_hq", fake_companies)

    class _Resp:
        parsed = _HQBatch(
            items=[_HQ(name="Acme", country="United Kingdom", city="London", state="")]
        )

    async def fake_generate(*, client, model, contents, config):
        assert client is None  # litellm path: no genai client to reuse
        return _Resp()

    monkeypatch.setattr(tidy_hq.llm, "generate", fake_generate)

    written = []

    async def fake_set_hq(driver, name, country, city, state):
        written.append((name, country, city, state))

    monkeypatch.setattr(tidy_hq.queries, "set_hq", fake_set_hq)

    asyncio.run(tidy_hq._run_tidy())
    assert written == [("Acme", "United Kingdom", "London", None)]
