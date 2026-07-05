"""Long-term memory for the assistant — stored in the graph (course Day 3).

Sessions give short-term (within-conversation) memory; this gives *long-term*
memory that survives across conversations and process restarts. Kept as
(:Memory {text, createdAt}) nodes so it's persistent, inspectable, and on-brand
(everything lives in the graph). Mirrors the idea of an ADK MemoryService.
"""

from app.graph.driver import get_driver


async def remember(fact: str) -> dict:
    """Store a durable fact or preference about the user to recall in future
    conversations (e.g. "focuses on employee-owned companies"). Call this whenever
    the user states a lasting preference or asks you to remember something."""
    async with get_driver().session() as session:
        await session.run("CREATE (m:Memory {text: $text, createdAt: datetime()})", text=fact)
    return {"remembered": fact}


async def load_memories(limit: int = 25) -> list[str]:
    """Most recent remembered facts, newest first."""
    async with get_driver().session() as session:
        result = await session.run(
            "MATCH (m:Memory) RETURN m.text AS text ORDER BY m.createdAt DESC LIMIT $n",
            n=limit,
        )
        return [record["text"] async for record in result]
