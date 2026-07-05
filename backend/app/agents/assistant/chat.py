"""Chat with the Nebula research assistant.

    make chat                         # interactive
    make chat ARGS="one question"     # single turn (handy for testing/scripts)

Session handling + long-term memory seeding live in
`app.agents.assistant.service`, shared with the /chat API. Each CLI run is a fresh
process, so its session starts empty (short-term) but recalls graph memories.
"""

import asyncio
import sys

from app.agents.assistant.proposals import get_proposal
from app.agents.assistant.service import respond
from app.graph.driver import close_driver

SESSION_ID = "cli"


async def _render(turn) -> str:
    out = turn.reply
    for pending in turn.proposals:
        pid = pending["proposal_id"]
        for _ in range(180):  # background research runs on this same loop
            proposal = get_proposal(pid)
            if proposal and proposal.get("status") != "pending":
                break
            await asyncio.sleep(1)
        proposal = get_proposal(pid) or {}
        if proposal.get("status") == "ready":
            record = proposal.get("record", {})
            out += (
                f"\n\n[proposal {pid}] {proposal['name']} — "
                f"{len(record.get('clients', []))} clients, "
                f"{len(record.get('leadership', []))} leaders. Review/commit in the UI."
            )
        elif proposal.get("status") == "error":
            out += f"\n\n[proposal {pid}] research failed: {proposal.get('error')}"
    for bf in turn.backfills:
        out += (
            f"\n\n[backfill {bf['job_id']}] researching {bf['field']} for "
            f"{bf['total']} companies — review/commit in the app."
        )
    return out


async def chat(one_shot: str | None = None) -> None:
    try:
        if one_shot is not None:
            print(await _render(await respond(SESSION_ID, one_shot)))
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
                print("\n" + await _render(await respond(SESSION_ID, question)))
    finally:
        await close_driver()


def main() -> None:
    asyncio.run(chat(" ".join(sys.argv[1:]) or None))


if __name__ == "__main__":
    main()
