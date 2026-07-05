"""Run the enrichment agent on one company, from the CLI or programmatically.

    make enrich NAME="Anthropic" WEBSITE="anthropic.com" TOPIC="AI-native engineering"

Prints the agent's trajectory (each tool call + result) as it goes — a first
taste of observability (course Day 4): you can watch what the agent decided to do.
"""

import argparse
import asyncio

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.enrichment.agent import root_agent
from app.config import ensure_gemini_env
from app.graph.driver import close_driver

APP_NAME = "nebula-enrichment"


async def enrich(name: str, website: str, topic: str, *, verbose: bool = True) -> str:
    """Research and save one company; return the agent's final summary."""
    ensure_gemini_env()
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id="cli", session_id="s1")
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    prompt = f"Research and save this company. name={name!r}, website={website!r}, topic={topic!r}."
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final = ""
    async for event in runner.run_async(user_id="cli", session_id="s1", new_message=message):
        if verbose and event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    args = dict(part.function_call.args or {})
                    print(f"  → {part.function_call.name}({args})"[:220])
                elif part.function_response:
                    print(f"  ← {str(part.function_response.response)[:180]}")
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text for p in event.content.parts if p.text)
    return final


async def _main(name: str, website: str, topic: str) -> None:
    try:
        summary = await enrich(name, website, topic)
        print("\n=== summary ===\n" + summary)
    finally:
        await close_driver()


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich one company via the ADK agent.")
    parser.add_argument("name")
    parser.add_argument("website")
    parser.add_argument("topic", nargs="?", default="AI-native engineering")
    args = parser.parse_args()
    asyncio.run(_main(args.name, args.website, args.topic))


if __name__ == "__main__":
    main()
