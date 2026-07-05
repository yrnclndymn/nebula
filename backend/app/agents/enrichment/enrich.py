"""Run the enrichment agent on one company, from the CLI or programmatically.

    make enrich NAME="Anthropic" WEBSITE="anthropic.com" TOPIC="AI-native engineering"

Returns an `EnrichResult` capturing the final summary, the record the agent chose
to save (the `save_company` args), and the tool-call trajectory — which the eval
harness grades (course Day 4). Prints the trajectory live when verbose.
"""

import argparse
import asyncio
from dataclasses import dataclass, field

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.enrichment.agent import root_agent
from app.config import ensure_gemini_env
from app.graph.driver import close_driver

APP_NAME = "nebula-enrichment"


@dataclass
class EnrichResult:
    summary: str = ""
    saved: dict | None = None  # the save_company args the agent produced
    tool_calls: list[str] = field(default_factory=list)  # names, in order


async def enrich(name: str, website: str, topic: str, *, verbose: bool = True) -> EnrichResult:
    """Research and save one company; return the agent's result + trajectory."""
    ensure_gemini_env()
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id="cli", session_id="s1")
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    prompt = f"Research and save this company. name={name!r}, website={website!r}, topic={topic!r}."
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    result = EnrichResult()
    async for event in runner.run_async(user_id="cli", session_id="s1", new_message=message):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    result.tool_calls.append(part.function_call.name)
                    args = dict(part.function_call.args or {})
                    if part.function_call.name == "save_company":
                        result.saved = args
                    if verbose:
                        print(f"  → {part.function_call.name}({args})"[:220])
                elif part.function_response and verbose:
                    print(f"  ← {str(part.function_response.response)[:180]}")
        if event.is_final_response() and event.content and event.content.parts:
            result.summary = "".join(p.text for p in event.content.parts if p.text)
    return result


async def _main(name: str, website: str, topic: str) -> None:
    try:
        result = await enrich(name, website, topic)
        print("\n=== summary ===\n" + result.summary)
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
