"""Graph schema: uniqueness constraints and indexes.

Idempotent (every statement is IF NOT EXISTS). Apply with:

    make db-init            # or: uv run python -m app.graph.schema
"""

import asyncio

from neo4j import AsyncDriver

# Controlled-vocabulary nodes get a UNIQUE constraint so MERGE-by-name dedupes.
# Company is the human key from the sheet; Topic/CompanyType/Tool are tags.
# Person is only INDEXed (not unique): people are deduped by name for now, which
# is imperfect — the enrichment agent will re-key them by LinkedIn URL later.
SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT company_name IF NOT EXISTS FOR (c:Company) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT topic_name IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT companytype_name IF NOT EXISTS FOR (ct:CompanyType) REQUIRE ct.name IS UNIQUE",
    "CREATE CONSTRAINT tool_name IF NOT EXISTS FOR (t:Tool) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT source_url IF NOT EXISTS FOR (s:Source) REQUIRE s.url IS UNIQUE",
    "CREATE CONSTRAINT page_url IF NOT EXISTS FOR (p:Page) REQUIRE p.url IS UNIQUE",
    "CREATE CONSTRAINT siteclients_domain IF NOT EXISTS FOR (sc:SiteClients) REQUIRE sc.domain IS UNIQUE",
    "CREATE CONSTRAINT fielddef_name IF NOT EXISTS FOR (fd:FieldDef) REQUIRE fd.name IS UNIQUE",
    "CREATE CONSTRAINT job_id IF NOT EXISTS FOR (j:Job) REQUIRE j.id IS UNIQUE",
    # Signal.url stores the *canonical* URL (see models.canonicalise_url); the
    # uniqueness constraint is what dedupes the same story captured twice.
    "CREATE CONSTRAINT signal_url IF NOT EXISTS FOR (s:Signal) REQUIRE s.url IS UNIQUE",
    "CREATE INDEX company_website IF NOT EXISTS FOR (c:Company) ON (c.website)",
    "CREATE INDEX company_hqcountry IF NOT EXISTS FOR (c:Company) ON (c.hqCountry)",
    "CREATE INDEX person_name IF NOT EXISTS FOR (p:Person) ON (p.name)",
]


async def apply_schema(driver: AsyncDriver) -> None:
    async with driver.session() as session:
        for stmt in SCHEMA_STATEMENTS:
            await session.run(stmt)


async def _main() -> None:
    from app.graph.driver import close_driver, get_driver

    await apply_schema(get_driver())
    print(f"Applied {len(SCHEMA_STATEMENTS)} schema statements.")
    await close_driver()


if __name__ == "__main__":
    asyncio.run(_main())
