"""Mutation-hardening for `assistant/proposals.py` (issue #207).

Kills the survivor cluster wave-013 recorded on the propose→review→commit flow:
`propose_enrichment` (payload construction + job/enqueue/supersede threading),
`discover_website` (the weak-pick verification loop), and `_is_known_topic`
(driver threading + the fail-open log line).

These are all DB-free by design (the #140 lesson): the real graph tests for this
module SKIP when Neo4j is absent, so under `make mutate` the mutated lines run
with *no* covering test and every payload/threading mutant lives. Argument-blind
stubs are why — so the fakes here ASSERT the exact job payload fields, ids, and
argument threading (captured then asserted *after* the call, since propose wraps
supersede in a bare `except`), plus a sentinel driver for the driver-threading
mutants.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.agents.assistant import proposals

NAME = "Acme __pytest__"
WEBSITE = "acme.example"


def _install_job_fakes(monkeypatch, captured):
    """Capture create_job/enqueue payloads instead of touching the graph."""

    async def fake_create_job(job_id, jtype, data):
        captured["create"] = (job_id, jtype, dict(data))

    async def fake_enqueue(job_id, delay=0.0):
        captured["enqueue"] = (job_id, delay)

    monkeypatch.setattr(proposals.jobs, "create_job", fake_create_job)
    monkeypatch.setattr(proposals.jobs, "enqueue", fake_enqueue)


# --- propose_enrichment: payload + job/enqueue/supersede threading ------------


def test_propose_payload_and_threading(monkeypatch):
    """One asserting pass over the whole propose body: proposal_id shape, the exact
    create_job payload, the enqueue id+delay, the supersede name+focus_key, the
    turn_proposals append, and the return dict. Kills the payload-key/value and
    argument-threading survivors that argument-blind stubs left alive."""
    captured = {}
    _install_job_fakes(monkeypatch, captured)

    async def fake_supersede(name, focus_key):
        captured["supersede"] = (name, focus_key)

    monkeypatch.setattr(proposals, "_supersede_errored_proposals", fake_supersede)

    collected: list = []
    token = proposals.turn_proposals.set(collected)
    try:
        out = asyncio.run(proposals.propose_enrichment(NAME, WEBSITE, focus="headcount"))
    finally:
        proposals.turn_proposals.reset(token)

    pid = out["proposal_id"]
    # proposal_id: a real 8-char hex slug, not None and not [:9].
    assert isinstance(pid, str) and len(pid) == 8

    # Return dict is exactly these three keys/values.
    assert out == {
        "proposal_id": pid,
        "name": NAME,
        "status": "researching in the background",
    }

    # create_job: the proposal id is threaded through, the job type is "proposal".
    job_id, jtype, data = captured["create"]
    assert job_id == pid
    assert jtype == "proposal"

    # The persisted payload is exactly this shape (keys AND values).
    assert data == {
        "proposal_id": pid,
        "status": "pending",
        "name": NAME,
        "website": WEBSITE,
        "topic": proposals.DEFAULT_TOPIC,
        "focus": "headcount",
        "focus_key": "headcount",
    }

    # enqueue: the same id, default delay 0.0 (not deferred).
    assert captured["enqueue"] == (pid, 0.0)

    # supersede: the company name and the RESOLVED focus_key, in that order.
    assert captured["supersede"] == (NAME, "headcount")

    # turn_proposals gets exactly one entry with exactly these keys/values.
    assert collected == [{"proposal_id": pid, "status": "pending", "name": NAME}]


def test_propose_defaults_empty_focus_and_immediate_enqueue(monkeypatch):
    """With no focus and no delay passed, the payload's focus is "" (focus_key None)
    and the job enqueues immediately — pins the `focus=""` / `enqueue_delay=0.0`
    signature defaults."""
    captured = {}
    _install_job_fakes(monkeypatch, captured)

    async def fake_supersede(name, focus_key):
        captured["supersede"] = (name, focus_key)

    monkeypatch.setattr(proposals, "_supersede_errored_proposals", fake_supersede)

    out = asyncio.run(proposals.propose_enrichment(NAME, WEBSITE))

    _job_id, _jtype, data = captured["create"]
    assert data["focus"] == ""  # default focus, not a mutated literal
    assert data["focus_key"] is None
    assert captured["enqueue"][1] == 0.0  # default delay: start now
    assert captured["supersede"] == (NAME, None)
    assert out["status"] == "researching in the background"


def test_propose_defers_enqueue_when_delay_given(monkeypatch):
    """A non-zero enqueue_delay is threaded verbatim to jobs.enqueue (issue #65
    stagger) — pins the delay keyword on the enqueue call."""
    captured = {}
    _install_job_fakes(monkeypatch, captured)

    async def fake_supersede(name, focus_key):
        return None

    monkeypatch.setattr(proposals, "_supersede_errored_proposals", fake_supersede)

    out = asyncio.run(proposals.propose_enrichment(NAME, WEBSITE, enqueue_delay=2.5))

    assert captured["enqueue"][1] == 2.5
    assert captured["enqueue"][0] == out["proposal_id"]


def test_propose_survives_and_logs_supersede_failure(monkeypatch, caplog):
    """The supersede step is best-effort: when it raises, propose still returns the
    fresh proposal and logs a warning that names the company WITH a traceback.
    Pins the warning message text, its `%r` argument, and `exc_info=True`."""
    captured = {}
    _install_job_fakes(monkeypatch, captured)

    async def boom(name, focus_key):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(proposals, "_supersede_errored_proposals", boom)

    with caplog.at_level(logging.WARNING, logger="nebula.proposals"):
        out = asyncio.run(proposals.propose_enrichment("Gamma __pytest__", "g.example"))

    assert out["status"] == "researching in the background"  # failure is non-fatal
    rec = [r for r in caplog.records if r.name == "nebula.proposals"][-1]
    assert rec.getMessage() == "could not supersede errored proposals for 'Gamma __pytest__'"
    # Truthy (a real (type, value, tb) tuple), so exc_info=True — not None and not
    # False, both of which logging stores verbatim and read as "no traceback".
    assert rec.exc_info


def test_novel_topic_confirmation_message_is_exact(monkeypatch):
    """The new-topic guard returns a specific instruction to the model and creates
    NO job. Pins the whole message string (every fragment/case) and the `message`
    key so the guidance can't silently drift."""
    created = {"called": False}

    async def fake_create_job(job_id, jtype, data):
        created["called"] = True

    monkeypatch.setattr(proposals.jobs, "create_job", fake_create_job)

    async def fake_list_topics(driver):
        return ["AI-native engineering", "SAP ecosystem"]

    monkeypatch.setattr(proposals.queries, "list_topics", fake_list_topics)

    topic = "Novel domain __pytest__"
    out = asyncio.run(proposals.propose_enrichment(NAME, WEBSITE, topic=topic))

    assert created["called"] is False  # no job for an unconfirmed novel topic
    assert out["status"] == "needs_confirmation"
    assert out["needs_topic_confirmation"] is True
    assert out["topic"] == topic
    assert out["message"] == (
        f'"{topic}" is not an existing research topic. New topics are fine, '
        "but confirm with the user that this should be a NEW research domain "
        "(not their request phrasing mistaken for a topic) before proceeding. "
        "If they confirm, call propose_enrichment again with "
        "confirm_new_topic=True; otherwise use an existing topic or the default."
    )


# --- _is_known_topic: driver threading + fail-open logging ---------------------


def test_is_known_topic_threads_driver_and_returns_membership(monkeypatch):
    """The topics read is passed the live driver (not None), and the result is a
    membership test against what it returns."""
    sentinel = object()
    monkeypatch.setattr(proposals, "get_driver", lambda: sentinel)
    seen = {}

    async def fake_list_topics(driver):
        seen["driver"] = driver
        return ["AI-native engineering", "SAP ecosystem"]

    monkeypatch.setattr(proposals.queries, "list_topics", fake_list_topics)

    assert asyncio.run(proposals._is_known_topic("SAP ecosystem")) is True
    assert asyncio.run(proposals._is_known_topic("Not a topic __pytest__")) is False
    assert seen["driver"] is sentinel  # the driver was threaded through, not None


def test_is_known_topic_fails_open_and_logs(monkeypatch, caplog):
    """A topics-read error returns True (fail open) and logs a warning naming the
    topic WITH a traceback. Pins the message text, the `%r` topic argument, and
    exc_info=True."""
    monkeypatch.setattr(proposals, "get_driver", lambda: object())

    async def boom(driver):
        raise RuntimeError("db down")

    monkeypatch.setattr(proposals.queries, "list_topics", boom)

    with caplog.at_level(logging.WARNING, logger="nebula.proposals"):
        result = asyncio.run(proposals._is_known_topic("Mystery topic __pytest__"))

    assert result is True  # fail open: a read glitch never blocks enrichment
    rec = [r for r in caplog.records if r.name == "nebula.proposals"][-1]
    assert rec.getMessage() == (
        "could not read topics to validate 'Mystery topic __pytest__'; allowing"
    )
    assert rec.exc_info  # truthy tuple → exc_info=True (not None and not False)


# --- discover_website: search query + weak-pick verification loop --------------


def _install_discovery_fakes(monkeypatch, *, web_search, rank_hosts, fetch_page, mentions):
    monkeypatch.setattr(proposals, "web_search", web_search)
    monkeypatch.setattr(proposals, "rank_hosts", rank_hosts)
    monkeypatch.setattr(proposals, "fetch_page", fetch_page)
    monkeypatch.setattr(proposals, "page_mentions_name", mentions)


def test_discover_website_strong_pick_skips_verification(monkeypatch):
    """A strong (>= 0.9) host match is trusted directly: return it WITHOUT fetching
    the landing page. Also pins the search query string. Kills the `>=`→`>`
    boundary mutant (which would drop a 0.9 into the fetch loop) and the
    None-query mutant."""
    calls = {}

    def fake_web_search(query):
        calls["query"] = query
        return {"results": [{"url": "https://acme.com"}]}

    def fake_rank_hosts(name, results):
        return [SimpleNamespace(host="acme.com", score=0.9)]

    async def fake_fetch_page(url):
        calls["fetched"] = url
        return {"text": ""}

    def fake_mentions(name, text):
        return True

    _install_discovery_fakes(
        monkeypatch,
        web_search=fake_web_search,
        rank_hosts=fake_rank_hosts,
        fetch_page=fake_fetch_page,
        mentions=fake_mentions,
    )

    out = asyncio.run(proposals.discover_website("Acme"))

    assert out == "acme.com"
    assert "fetched" not in calls  # 0.9 is a STRONG match: no verification fetch
    assert calls["query"] == "Acme official website"


def test_discover_website_missing_results_defaults_to_empty_list(monkeypatch):
    """A search response with no `results` key degrades to an empty list (not None)
    so ranking runs on [] and returns None — pins the `.get("results", [])`
    default."""
    seen = {}

    def fake_web_search(query):
        return {}  # no "results" key

    def fake_rank_hosts(name, results):
        seen["results"] = results
        return []

    async def fake_fetch_page(url):  # pragma: no cover - never reached
        raise AssertionError("should not fetch when nothing ranks")

    def fake_mentions(name, text):  # pragma: no cover - never reached
        raise AssertionError("should not check mentions when nothing ranks")

    _install_discovery_fakes(
        monkeypatch,
        web_search=fake_web_search,
        rank_hosts=fake_rank_hosts,
        fetch_page=fake_fetch_page,
        mentions=fake_mentions,
    )

    out = asyncio.run(proposals.discover_website("Acme"))

    assert out is None
    assert seen["results"] == []  # default is [], not None


def test_discover_website_verification_loop_skips_bad_candidates(monkeypatch):
    """Weak picks: iterate the top candidates, CONTINUE past a fetch error and past
    an error-flagged page, and return the first that actually names the company.
    Pins the `https://` URL prefix, both `continue`s (vs `break`), and the
    `page.get("error")` key/value."""
    fetched: list = []

    def fake_web_search(query):
        return {"results": [{"url": "x"}]}

    def fake_rank_hosts(name, results):
        # All weak (< 0.9) so the whole verification loop runs; c1 is ranked[0]
        # (the fallback "best").
        return [
            SimpleNamespace(host="c1.example", score=0.5),
            SimpleNamespace(host="c2.example", score=0.4),
            SimpleNamespace(host="c3.example", score=0.3),
        ]

    async def fake_fetch_page(url):
        fetched.append(url)
        if url.endswith("c1.example"):
            raise RuntimeError("network")  # fetch error → continue
        if url.endswith("c2.example"):
            return {"error": "boom", "text": "acme corp"}  # error page → continue
        return {"text": "acme official site"}

    def fake_mentions(name, text):
        return "acme" in text.lower()

    _install_discovery_fakes(
        monkeypatch,
        web_search=fake_web_search,
        rank_hosts=fake_rank_hosts,
        fetch_page=fake_fetch_page,
        mentions=fake_mentions,
    )

    out = asyncio.run(proposals.discover_website("Acme"))

    # c1 errored on fetch, c2's page was error-flagged (its "acme corp" text is
    # never consulted) — only c3 is returned.
    assert out == "c3.example"
    assert fetched == ["https://c1.example", "https://c2.example", "https://c3.example"]


def test_discover_website_missing_page_text_defaults_to_empty(monkeypatch):
    """A verified page with no `text` key checks mentions against "" (not None or a
    mutated literal), then falls through to the ranked best on the miss — pins the
    `page.get("text", "")` default."""
    got = {}

    def fake_web_search(query):
        return {"results": [{"url": "x"}]}

    def fake_rank_hosts(name, results):
        return [SimpleNamespace(host="solo.example", score=0.5)]

    async def fake_fetch_page(url):
        return {}  # no "text", no "error"

    def fake_mentions(name, text):
        got["text"] = text
        return False

    _install_discovery_fakes(
        monkeypatch,
        web_search=fake_web_search,
        rank_hosts=fake_rank_hosts,
        fetch_page=fake_fetch_page,
        mentions=fake_mentions,
    )

    out = asyncio.run(proposals.discover_website("Acme"))

    assert got["text"] == ""  # missing text defaults to empty string
    assert out == "solo.example"  # no page named it → fall through to ranked best


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
