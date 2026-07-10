"""Back-fill a custom field across companies of its kind, with batch review.

Durable (graph-backed job via `app.graph.jobs`) so it survives scale-to-zero.
`start_backfill` creates a job + enqueues it; the runner researches each company
and updates the job progressively; the user reviews and commits selected rows,
which writes the value + a provenance citation.
"""

import uuid
from contextvars import ContextVar

from app.graph import jobs, queries
from app.graph.driver import get_driver
from app.tools.field_extract import extract_field

turn_backfills: ContextVar[list | None] = ContextVar("nebula_turn_backfills", default=None)


async def _applicable_companies(
    driver,
    applies_to_kind: str,
    country: str | None,
    missing_key: str | None = None,
    company: str | None = None,
) -> list[dict]:
    kind_clause = "" if applies_to_kind == "all" else "AND c.kind = $kind"
    country_clause = "AND c.hqCountry = $country" if country else ""
    # Scope to a single named company when the user asked about just that one.
    company_clause = "AND c.name = $company" if company else ""
    # Only companies that don't already have the field set (its property is the key).
    missing_clause = "AND c[$missingKey] IS NULL" if missing_key else ""
    async with driver.session() as session:
        result = await session.run(
            f"MATCH (c:Company)-[:TAGGED_AS]->(:Topic) "
            f"WHERE c.website IS NOT NULL {kind_clause} {country_clause} {company_clause} {missing_clause} "
            f"RETURN DISTINCT c.name AS name, c.website AS website ORDER BY name",
            kind=applies_to_kind,
            country=country,
            company=company,
            missingKey=missing_key,
        )
        return [dict(record) async for record in result]


async def start_backfill(
    field_name: str, country: str = "", missing_only: bool = False, company: str = ""
) -> dict:
    """Research a custom field for companies of its kind and prepare a batch for the
    user to review and commit. Returns immediately; runs in the background. Use when
    the user asks to fill in / research an existing field. The field_name is the
    field's key (e.g. 'serviceLines'). Scope it to match what the user asked for:
    pass company (the company's exact name) to fill the field for JUST that one
    company when the user named a specific company; pass country (full name, e.g.
    'United Kingdom') to limit to companies HQ'd there; set missing_only=True when
    the user wants only the companies that DON'T already have a value (e.g. 'fill it
    in where it's missing'). With no scope it researches every applicable company."""
    driver = get_driver()
    field_defs = {f["name"]: f for f in await queries.list_field_defs(driver)}
    field_def = field_defs.get(field_name)
    if field_def is None:
        return {"error": f"no field named {field_name!r}; add it first with add_field"}

    missing_key = field_name if missing_only else None
    companies = await _applicable_companies(
        driver, field_def["appliesToKind"], country or None, missing_key, company or None
    )
    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "backfill",
        {
            "job_id": job_id,
            "status": "pending",
            "field": {k: field_def[k] for k in ("name", "label", "type")},
            "field_name": field_name,
            "country": country or "",
            "company": company or "",
            "missing_only": missing_only,
            "total": len(companies),
            "done": 0,
            "rows": [],
        },
    )
    await jobs.enqueue(job_id)

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


async def run_backfill_job(job_id: str) -> None:
    """Job runner: research each applicable company, updating the job progressively."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    field_defs = {f["name"]: f for f in await queries.list_field_defs(driver)}
    field_def = field_defs.get(job["field_name"])
    if field_def is None:
        await jobs.update_job(job_id, {**job, "error": "field was removed"}, status="error")
        return

    missing_key = job["field_name"] if job.get("missing_only") else None
    companies = await _applicable_companies(
        driver,
        field_def["appliesToKind"],
        job.get("country") or None,
        missing_key,
        job.get("company") or None,
    )
    rows: list[dict] = []
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
        rows.append(
            {
                "company": company["name"],
                "value": result["value"],
                "source": result.get("source", ""),
                "committed": False,
            }
        )
        await jobs.update_job(job_id, {**job, "rows": rows, "done": len(rows)})
    await jobs.update_job(job_id, {**job, "rows": rows, "done": len(rows)}, status="ready")


async def get_backfill(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_backfill(job_id: str, companies: list[str] | None = None) -> dict:
    """Write selected back-fill rows (all if companies is None) with provenance."""
    job = await jobs.get_job(job_id)
    if job is None:
        return {"error": "unknown job"}
    driver = get_driver()
    field = job["field"]
    rows = job["rows"]
    written = 0
    for row in rows:
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
    await jobs.update_job(job_id, {**job, "rows": rows})
    return {"committed": written}
