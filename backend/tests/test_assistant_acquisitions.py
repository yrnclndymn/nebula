"""Chat assistant can propose acquisitions (#126).

The assistant gains the acquisition verb it lacked in prod: a tool that starts the
SAME background acquisition-research proposal the API/SPA M&A view starts (#43),
delegating to ``app.agents.deals.proposals.propose_acquisitions``. The tool NEVER
writes an :ACQUIRED edge — it delegates to a background proposal the user commits
(human-in-the-loop). Pure wiring/delegation tests here; the deals module is mocked
so nothing researches or writes. Fictional names only.
"""

import asyncio

from app.agents.assistant import acquisitions as acq


def test_assistant_wires_up_acquisitions_tool():
    from app.agents.assistant.agent import root_agent

    names = {getattr(t, "__name__", getattr(t, "name", None)) for t in root_agent.tools}
    assert "propose_acquisitions" in names


def test_tool_delegates_to_deals_proposals_and_never_writes(monkeypatch):
    """The tool passes the named company straight to the deals proposal builder and
    returns its result verbatim — it does no research and no write of its own."""
    calls: list = []

    async def fake_propose(company, **kwargs):
        calls.append((company, kwargs))
        return {"job_id": "abc12345", "company": company, "status": "researching in the background"}

    # Mock the deals module the tool delegates to: if anything wrote to the graph it
    # would have to go through the real proposal builder, which we've replaced.
    monkeypatch.setattr(acq.deal_proposals, "propose_acquisitions", fake_propose)

    out = asyncio.run(acq.propose_acquisitions("Acme __acqtest__"))

    assert len(calls) == 1
    assert calls[0][0] == "Acme __acqtest__"  # delegated with the exact named company
    assert out["job_id"] == "abc12345"  # deals result returned verbatim
    assert out["company"] == "Acme __acqtest__"


def test_tool_surfaces_ref_in_turn_collector(monkeypatch):
    """A successful proposal appends a {job_id, company} ref to the per-turn collector
    so /chat can return an inline review card — mirroring turn_backfills/turn_merges."""

    async def fake_propose(company, **kwargs):
        return {"job_id": "abc12345", "company": company, "status": "researching in the background"}

    monkeypatch.setattr(acq.deal_proposals, "propose_acquisitions", fake_propose)

    collected: list = []
    token = acq.turn_acquisitions.set(collected)
    try:
        out = asyncio.run(acq.propose_acquisitions("Acme __acqtest__"))
    finally:
        acq.turn_acquisitions.reset(token)

    assert len(collected) == 1  # surfaced in the turn
    assert collected[0] == {"job_id": "abc12345", "company": "Acme __acqtest__"}
    assert out["job_id"] == "abc12345"  # result still returned to the model verbatim


def test_tool_error_surfaces_no_card(monkeypatch):
    """A 404-shaped error must NOT surface a card — there is no proposal to review."""

    async def fake_propose(company, **kwargs):
        return {"error": f"no company named {company!r} to research acquisitions for"}

    monkeypatch.setattr(acq.deal_proposals, "propose_acquisitions", fake_propose)

    collected: list = []
    token = acq.turn_acquisitions.set(collected)
    try:
        out = asyncio.run(acq.propose_acquisitions("Ghost __acqtest__"))
    finally:
        acq.turn_acquisitions.reset(token)

    assert collected == []  # nothing to review, so no inline card
    assert "error" in out


def test_tool_without_collector_does_not_raise(monkeypatch):
    """Outside a chat turn the collector is unset (default None); the tool must still
    delegate and return cleanly rather than blowing up on the missing collector."""

    async def fake_propose(company, **kwargs):
        return {"job_id": "def67890", "company": company, "status": "researching in the background"}

    monkeypatch.setattr(acq.deal_proposals, "propose_acquisitions", fake_propose)

    assert acq.turn_acquisitions.get() is None  # no collector set
    out = asyncio.run(acq.propose_acquisitions("Acme __acqtest__"))
    assert out["job_id"] == "def67890"


def test_tool_relays_not_found_error(monkeypatch):
    """A 404-shaped error from the deals layer (company not tracked) is relayed, not
    swallowed — the assistant tells the user rather than claiming research started."""

    async def fake_propose(company, **kwargs):
        return {"error": f"no company named {company!r} to research acquisitions for"}

    monkeypatch.setattr(acq.deal_proposals, "propose_acquisitions", fake_propose)

    out = asyncio.run(acq.propose_acquisitions("Ghost __acqtest__"))
    assert "error" in out and "no company named" in out["error"]


def test_respond_surfaces_turn_acquisitions(monkeypatch):
    """End-to-end turn wiring: a tool appending to the collector DURING the turn
    surfaces in ChatTurn.acquisitions (the /chat payload), and the collector is
    reset afterwards — the same contract the proposals/backfills/merges keep."""
    from app.agents.assistant import service

    ref = {"job_id": "abc12345", "company": "Acme __acqtest__"}

    class _FakeSessions:
        async def get_session(self, **kwargs):
            return object()  # existing session -> no memory preamble path

    class _FakeRunner:
        async def run_async(self, **kwargs):
            collected = acq.turn_acquisitions.get()
            assert collected is not None  # respond() installed the collector
            collected.append(dict(ref))
            if False:  # pragma: no cover — makes this an async generator
                yield

    monkeypatch.setattr(service, "_runner", _FakeRunner())
    monkeypatch.setattr(service, "_sessions", _FakeSessions())

    turn = asyncio.run(service.respond("sess-acqtest", "record the Acme deal"))
    assert turn.acquisitions == [ref]
    assert acq.turn_acquisitions.get() is None  # reset once the turn ended


def test_tool_has_no_direct_write_path():
    """The module only delegates — it must not import any graph write path (upsert /
    driver), which would be a way to record an acquisition without the user's commit.
    """
    import inspect

    src = inspect.getsource(acq)
    assert "upsert" not in src
    assert "get_driver" not in src


def test_instructions_route_acquisitions_and_forbid_workarounds():
    from app.agents.assistant.agent import _INSTRUCTION

    lowered = _INSTRUCTION.lower()
    # The tool is advertised so "record that X acquired Y" routes to it.
    assert "propose_acquisitions" in _INSTRUCTION
    assert "acquisition" in lowered or "acquired" in lowered
    # Steer AWAY from the prod workarounds: about-text, partner edges, custom column.
    assert "about" in lowered
    assert "partners_with" in lowered or "partner" in lowered
    # A user's asserted deal is a research LEAD, not a fact to write directly.
    assert "lead" in lowered
    # Review phrasing — a proposal appears for review, never a direct save.
    assert "review" in lowered
