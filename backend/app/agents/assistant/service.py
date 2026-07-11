"""Assistant service — one persistent Runner shared by the CLI and the /chat API.

A process-wide InMemorySessionService keeps a session per `session_id` (e.g. one
per browser tab), so multi-turn context works across separate HTTP requests.
Long-term memory (graph `(:Memory)` nodes) is seeded into a session the first time
it's used.
"""

from dataclasses import dataclass, field

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.assistant.agent import root_agent
from app.agents.assistant.backfill import turn_backfills
from app.agents.assistant.memory import load_memories
from app.agents.assistant.merge import turn_merges
from app.agents.assistant.proposals import turn_proposals
from app.config import ensure_gemini_env

APP_NAME = "nebula-assistant"
USER_ID = "andy"


@dataclass
class ChatTurn:
    reply: str
    proposals: list[dict] = field(default_factory=list)
    backfills: list[dict] = field(default_factory=list)
    merges: list[dict] = field(default_factory=list)


_sessions: InMemorySessionService | None = None
_runner: Runner | None = None


def _runner_singleton() -> Runner:
    global _sessions, _runner
    if _runner is None:
        ensure_gemini_env()
        _sessions = InMemorySessionService()
        _runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=_sessions)
    return _runner


async def respond(session_id: str, message: str) -> ChatTurn:
    """Run one conversational turn; return the reply and any enrichment proposals
    the assistant prepared during it."""
    runner = _runner_singleton()
    assert _sessions is not None

    existing = await _sessions.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    text = message
    if existing is None:
        await _sessions.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        memories = await load_memories()
        if memories:
            text = (
                "Known facts from earlier sessions:\n"
                + "\n".join(f"- {m}" for m in memories)
                + "\n\n"
                + message
            )

    proposals: list[dict] = []
    backfills: list[dict] = []
    merges: list[dict] = []
    p_token = turn_proposals.set(proposals)
    b_token = turn_backfills.set(backfills)
    m_token = turn_merges.set(merges)
    content = types.Content(role="user", parts=[types.Part(text=text)])
    reply = ""
    try:
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(p.text for p in event.content.parts if p.text)
    finally:
        turn_proposals.reset(p_token)
        turn_backfills.reset(b_token)
        turn_merges.reset(m_token)
    return ChatTurn(reply=reply, proposals=proposals, backfills=backfills, merges=merges)
