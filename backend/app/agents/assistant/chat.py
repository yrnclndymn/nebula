"""Chat with the Nebula research assistant.

    make chat                         # interactive
    make chat ARGS="one question"     # single turn (handy for testing/scripts)

Session handling + long-term memory seeding live in
`app.agents.assistant.service`, shared with the /chat API. Each CLI run is a fresh
process, so its session starts empty (short-term) but recalls graph memories.
"""

import asyncio
import sys

from app.agents.assistant.service import respond
from app.graph.driver import close_driver

SESSION_ID = "cli"


def _render(turn) -> str:
    out = turn.reply
    for p in turn.proposals:
        out += f"\n\n[proposal {p['proposal_id']}] {p['name']} — review and commit in the UI"
    return out


async def chat(one_shot: str | None = None) -> None:
    try:
        if one_shot is not None:
            print(_render(await respond(SESSION_ID, one_shot)))
            return

        print("Nebula assistant — ask about the research graph. 'exit' to quit.")
        while True:
            try:
                question = input("\n> ").strip()
            except EOFError:
                break
            if question.lower() in {"exit", "quit"}:
                break
            if question:
                print("\n" + _render(await respond(SESSION_ID, question)))
    finally:
        await close_driver()


def main() -> None:
    asyncio.run(chat(" ".join(sys.argv[1:]) or None))


if __name__ == "__main__":
    main()
