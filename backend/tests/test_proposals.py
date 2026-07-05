"""Safety test: in propose mode, save_company captures but must NOT write."""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.tools.graph_tools import proposal_sink, save_company

NAME = "__pytest_propose__ Co"


def test_propose_captures_without_writing():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"

        sink: list = []
        token = proposal_sink.set(sink)
        try:
            result = await save_company(
                name=NAME,
                topic="AI-native engineering",
                about="a test company",
                website="",
                hq_location="",
                headcount=0,
                estimated_revenue="",
                year_founded=0,
                funding="",
                notes="",
                company_types=[],
                partnerships=[],
                clients=[],
                leadership=[],
                citations=[],
            )
        finally:
            proposal_sink.reset(token)

        async with get_driver().session() as session:
            r = await session.run("MATCH (c:Company {name: $n}) RETURN count(c) AS n", n=NAME)
            count = (await r.single())["n"]
        await close_driver()
        return result, sink, count

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    result, sink, count = out
    assert result["written"] is False
    assert len(sink) == 1 and sink[0]["name"] == NAME
    assert count == 0  # nothing was written to the graph
