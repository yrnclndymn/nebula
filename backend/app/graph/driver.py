"""Neo4j driver lifecycle and connectivity check.

A single driver is created at app startup and reused for all requests (the
official driver is thread-safe and manages its own connection pool).
"""

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import settings

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    """Return the process-wide async driver, creating it on first use."""
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def check_connectivity() -> None:
    """Raise if the database is unreachable. Used by the graph health check."""
    await get_driver().verify_connectivity()
