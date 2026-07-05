"""Chat with the Nebula research assistant.

    make chat                         # interactive
    make chat ARGS="one question"     # single turn (handy for testing/scripts)

Short-term memory = the ADK session (multi-turn within one run). Long-term memory
= (:Memory) nodes in the graph, loaded at start and seeded into the conversation,
written by the `remember` tool — so it persists across separate runs.
"""

import asyncio
import sys

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.assistant.agent import root_agent
from app.agents.assistant.memory import load_memories
from app.config import ensure_gemini_env
from app.graph.driver import close_driver

APP_NAME = "nebula-assistant"
USER_ID = "andy"
SESSION_ID = "s1"


async def _turn(runner: Runner, text: str) -> str:
    message = types.Content(role="user", parts=[types.Part(text=text)])
    reply = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            reply = "".join(p.text for p in event.content.parts if p.text)
    return reply


async def chat(one_shot: str | None = None) -> None:
    ensure_gemini_env()
    memories = await load_memories()
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    # Seed long-term memory into the conversation as the first thing the agent sees.
    preamble = ""
    if memories:
        preamble = (
            "Known facts from earlier sessions:\n" + "\n".join(f"- {m}" for m in memories) + "\n\n"
        )

    try:
        if one_shot is not None:
            print(await _turn(runner, preamble + one_shot))
            return

        print("Nebula assistant — ask about the research graph. 'exit' to quit.")
        if memories:
            print(f"(recalled {len(memories)} memories)")
        first = True
        while True:
            try:
                question = input("\n> ").strip()
            except EOFError:
                break
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            text = (preamble + question) if first else question
            first = False
            print("\n" + await _turn(runner, text))
    finally:
        await close_driver()


def main() -> None:
    one_shot = " ".join(sys.argv[1:]) or None
    asyncio.run(chat(one_shot))


if __name__ == "__main__":
    main()
