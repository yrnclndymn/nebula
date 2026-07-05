"""Assistant wiring + long-term memory round-trip (memory test needs Neo4j)."""

import asyncio

import pytest

from app.agents.assistant.memory import load_memories, remember
from app.graph.driver import check_connectivity, close_driver, get_driver

SENTINEL = "__pytest_memory__ user likes graph databases"


def test_assistant_wires_up_with_tools():
    from app.agents.assistant.agent import root_agent

    assert root_agent.name == "research_assistant"
    names = {getattr(t, "__name__", getattr(t, "name", None)) for t in root_agent.tools}
    assert {"run_cypher", "search_companies", "get_company", "remember"} <= names


def test_memory_roundtrip():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        await remember(SENTINEL)
        memories = await load_memories()
        # cleanup
        async with get_driver().session() as session:
            await session.run("MATCH (m:Memory {text: $t}) DETACH DELETE m", t=SENTINEL)
        await close_driver()
        return memories

    result = asyncio.run(scenario())
    if result == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    assert SENTINEL in result
