"""Live tracked-name list for the leak sensor (#104).

The repo is public, and the standing guardrail forbids tracked-company / client
names in code, comments, tests, or commit messages. A *static* blocklist can't
enforce that — the whole point of the product is that the tracked list grows
continuously. So the pre-push sensor (`scripts/check_names.py`) checks the
*live* list instead, pulled from the graph at check time and never written into
the repo or its history.

This module is the single source of that list: one query returning every
`:Company` node's name plus its recorded aliases, as plain strings (no
properties, no structure). It's exposed via a tiny CLI —
`uv run python -m app.graph.company_names` — that prints one name per line,
reading the same Neo4j creds the backend already uses (`app/config.py`). There
is deliberately **no HTTP endpoint**: the hook runs on the operator's machine
where backend creds already live, so a graph read here adds no new auth surface.
For a production pull, the operator exports the Aura URI/creds in the env before
invoking the CLI.

Junk-flagged stubs are excluded — they're extraction noise (UI boilerplate like
"read more"), not tracked organisations, and including them would only feed
false positives into the sensor.
"""

from neo4j import AsyncDriver


async def list_company_names(driver: AsyncDriver) -> list[str]:
    """Every tracked Company name and alias, de-duplicated and sorted.

    Strings only. Blank/whitespace entries are dropped. This is the exact
    surface the leak sensor matches added diff lines against; nothing here is
    written anywhere, and the caller is responsible for never persisting the
    returned list into the repo.
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company)
            WHERE NOT coalesce(c.junk, false)
            WITH c.name AS name, coalesce(c.aliases, []) AS aliases
            UNWIND ([name] + aliases) AS raw
            WITH trim(coalesce(raw, '')) AS n
            WHERE n <> ''
            RETURN DISTINCT n AS name
            ORDER BY name
            """
        )
        return [record["name"] async for record in result]


async def _main() -> None:
    from app.graph.driver import close_driver, get_driver

    driver = get_driver()
    try:
        names = await list_company_names(driver)
    finally:
        await close_driver()
    for name in names:
        print(name)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
