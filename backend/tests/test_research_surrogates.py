"""Crawled evidence with lone UTF-16 surrogates must not crash the evidence→Gemini
serialization of the research jobs (#127).

A real acquisition_proposal run died on ``'\\udb11'`` — a lone surrogate in a
fetched page — because the Gemini client encodes the prompt to UTF-8 and lone
surrogates aren't UTF-8-encodable. These tests drive the real evidence-gather +
prompt assembly of both research paths (M&A and people) with surrogate-bearing
search snippets AND page text, mocking the network and the model, then assert the
prompt handed to the model encodes cleanly. Fictional companies/people only.
"""

import asyncio

from app.agents.deals import research as deals_research
from app.agents.deals.models import AcquisitionResearch
from app.agents.people import research as people_research
from app.agents.people.models import PersonResearch

# The exact lone high surrogate from the prod crash.
_SURROGATE = "\udb11"


class _FakeResp:
    def __init__(self, parsed):
        self.parsed = parsed


def _install_capture(monkeypatch, module, parsed):
    """Mock a research module's network + model so we capture the assembled prompt.

    Both the search snippet and the fetched page text carry a lone surrogate, so
    the test exercises every evidence source that lands in the prompt.
    """
    captured: dict[str, str] = {}

    def fake_search(query):
        return {
            "results": [
                {
                    "title": f"Deal {_SURROGATE} coverage",
                    "url": "https://news.example/x",
                    "snippet": f"Acme acquired Globex {_SURROGATE} in 2024",
                }
            ]
        }

    async def fake_fetch(url):
        return {"url": url, "text": f"Globex was acquired {_SURROGATE} by Acme", "social": {}}

    async def fake_generate(*, model, contents, config, client=None):
        captured["prompt"] = contents
        return _FakeResp(parsed)

    monkeypatch.setattr(module, "web_search", fake_search)
    monkeypatch.setattr(module, "fetch_page", fake_fetch)
    monkeypatch.setattr(module.llm, "generate", fake_generate)
    return captured


def test_acquisition_evidence_with_surrogates_serializes(monkeypatch):
    captured = _install_capture(monkeypatch, deals_research, AcquisitionResearch(company="Acme"))
    result = asyncio.run(deals_research.research_acquisitions("Acme"))
    assert isinstance(result, AcquisitionResearch)
    prompt = captured["prompt"]
    assert _SURROGATE not in prompt
    prompt.encode("utf-8")  # the prod crash: must not raise
    assert "Acme" in prompt  # evidence survived, just sanitized


def test_person_evidence_with_surrogates_serializes(monkeypatch):
    captured = _install_capture(
        monkeypatch, people_research, PersonResearch(name="Jane Roe", current_company="Acme")
    )
    result = asyncio.run(people_research.research_person("Jane Roe", "Acme"))
    assert isinstance(result, PersonResearch)
    prompt = captured["prompt"]
    assert _SURROGATE not in prompt
    prompt.encode("utf-8")  # must not raise
    assert "Jane Roe" in prompt
