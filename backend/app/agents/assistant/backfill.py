"""Back-fill a custom field across companies of its kind, with batch review.

Like proposals, it runs in the BACKGROUND (research is slow) and the user reviews
+ commits. `start_backfill` kicks off extraction for every applicable company;
the client polls `get_backfill` (rows fill in progressively), then commits the
selected rows, which writes the custom field value + a provenance citation.
"""

import asyncio
import uuid
from contextvars import ContextVar

from app.graph import queries
from app.graph.driver import get_driver
from app.tools.field_extract import extract_field

BACKFILLS: dict[str, dict] = {}
turn_backfills: ContextVar[list | None] = ContextVar("nebula_turn_backfills", default=None)


async def _applicable_companies(driver, applies_to_kind: str, country: str | None) -> list[dict]:
    kind_clause = "" if applies_to_kind == "all" else "AND c.kind = $kind"
    country_clause = "AND c.hqCountry = $country" if country else ""
    async with driver.session() as session:
        result = await session.run(
            f"MATCH (c:Company)-[:TAGGED_AS]->(:Topic) "
            f"WHERE c.website IS NOT NULL {kind_clause} {country_clause} "
            f"RETURN DISTINCT c.name AS name, c.website AS website ORDER BY name",
            kind=applies_to_kind,
            country=country,
        )
        return [dict(record) async for record in result]


async def _run_backfill(job_id: str, field_def: dict, companies: list[dict]) -> None:
    job = BACKFILLS[job_id]
    for company in companies:
        try:
            result = await extract_field(
                company["website"], field_def["label"], field_def["description"], field_def["type"]
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "value": [] if field_def["type"] == "list" else "",
                "source": "",
                "error": str(exc),
            }
        job["rows"].append(
            {
                "company": company["name"],
                "value": result["value"],
                "source": result.get("source", ""),
                "committed": False,
            }
        )
        job["done"] = len(job["rows"])
    job["status"] = "ready"


async def start_backfill(field_name: str, country: str = "") -> dict:
    """Research a custom field for companies of its kind and prepare a batch for the
    user to review and commit. Returns immediately; runs in the background. Use when
    the user asks to fill in / research an existing field across companies. The
    field_name is the field's key (e.g. 'serviceLines'). Optionally scope to a
    country (e.g. 'United Kingdom') to only research companies headquartered there —
    use the full country name."""
    driver = get_driver()
    field_defs = {f["name"]: f for f in await queries.list_field_defs(driver)}
    field_def = field_defs.get(field_name)
    if field_def is None:
        return {"error": f"no field named {field_name!r}; add it first with add_field"}

    companies = await _applicable_companies(driver, field_def["appliesToKind"], country or None)
    job_id = uuid.uuid4().hex[:8]
    BACKFILLS[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "field": {k: field_def[k] for k in ("name", "label", "type")},
        "total": len(companies),
        "done": 0,
        "rows": [],
    }
    asyncio.create_task(_run_backfill(job_id, field_def, companies))

    collected = turn_backfills.get()
    if collected is not None:
        collected.append(
            {
                "job_id": job_id,
                "field": field_def["label"],
                "total": len(companies),
                "status": "pending",
            }
        )
    return {"job_id": job_id, "field": field_def["label"], "companies": len(companies)}


def get_backfill(job_id: str) -> dict | None:
    return BACKFILLS.get(job_id)


async def commit_backfill(job_id: str, companies: list[str] | None = None) -> dict:
    """Write selected back-fill rows (all if companies is None) with provenance."""
    job = BACKFILLS.get(job_id)
    if job is None:
        return {"error": "unknown job"}
    driver = get_driver()
    field = job["field"]
    written = 0
    for row in job["rows"]:
        if companies is not None and row["company"] not in companies:
            continue
        if not row["value"]:
            continue
        await queries.set_custom_field(driver, row["company"], field["name"], row["value"])
        if row.get("source"):
            await queries.cite(
                driver, row["company"], field["name"], str(row["value"]), row["source"]
            )
        row["committed"] = True
        written += 1
    return {"committed": written}
