"""Guard test for the read-only Cypher tool (no DB needed — rejection happens
before the driver is touched)."""

import asyncio

import pytest

from app.graph.queries import run_read_cypher


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DETACH DELETE n",
        "MERGE (c:Company {name:'x'})",
        "MATCH (c:Company) SET c.headcount = 0",
        "MATCH (c) REMOVE c.name",
    ],
)
def test_run_read_cypher_rejects_writes(query):
    with pytest.raises(ValueError):
        asyncio.run(run_read_cypher(None, query))
