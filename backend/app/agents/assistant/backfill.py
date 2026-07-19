"""Back-fill a custom field across companies of its kind, with batch review.

Durable (graph-backed job via `app.graph.jobs`) so it survives scale-to-zero.
`enqueue_backfill` creates a job + enqueues it; the runner researches each company
and updates the job progressively; the user reviews and commits selected rows,
which writes the value + a provenance citation.
"""

import json
import uuid
from contextvars import ContextVar

from app.budget import BudgetExhausted, budget_for, charge_company, use_budget
from app.graph import jobs, queries
from app.graph.driver import get_driver
from app.tools.field_extract import extract_field

turn_backfills: ContextVar[list | None] = ContextVar("nebula_turn_backfills", default=None)

# --- Structured scope filter -------------------------------------------------
# A back-fill's scope decides which companies get the field filled. Beyond the
# fixed kind / country / missing / one-company scoping, the user can express an
# arbitrary condition — but only as a STRUCTURED filter over an allowlist, never
# free-text Cypher. Untrusted (model / crawled) input is allowed to steer reads,
# but a back-fill proposal leads to a WRITE, so the scope must be deterministic,
# parameterized, and auditable on the review card. This mirrors how /companies
# builds its filters: validated field + operator, parameterized value.

# Allowlisted Company scalar properties a scope may filter on, with their type.
# "number" values are coerced to numbers; anything else is treated as a string.
_SCOPE_FIELDS: dict[str, str] = {
    "headcount": "number",
    "estimatedRevenue": "number",
    "yearFounded": "number",
    "hqCountry": "string",
    "hqCity": "string",
    "hqState": "string",
    "kind": "string",
    "priority": "string",
    "funding": "string",
    "origin": "string",
}

# Allowlisted operators → the Cypher token they map to. The model never supplies
# a raw Cypher operator; it names one of these keys and we substitute the token.
_SCOPE_OPS: dict[str, str] = {
    "=": "=",
    "!=": "<>",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "contains": "CONTAINS",
    "is_null": "IS NULL",
    "is_not_null": "IS NOT NULL",
}
_NULL_OPS = {"is_null", "is_not_null"}


def _normalize_condition(cond) -> dict:
    """Validate one ``{field, op, value}`` against the allowlists and coerce it."""
    if not isinstance(cond, dict):
        raise ValueError(f"scope condition must be an object, got {cond!r}")
    field = cond.get("field")
    op = cond.get("op")
    if field not in _SCOPE_FIELDS:
        raise ValueError(
            f"unknown scope field {field!r}; allowed: {', '.join(sorted(_SCOPE_FIELDS))}"
        )
    if op not in _SCOPE_OPS:
        raise ValueError(f"unknown scope operator {op!r}; allowed: {', '.join(sorted(_SCOPE_OPS))}")
    if op in _NULL_OPS:
        return {"field": field, "op": op, "value": None}
    value = cond.get("value")
    if value is None:
        raise ValueError(f"scope condition on {field!r} with op {op!r} requires a value")
    if _SCOPE_FIELDS[field] == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"scope field {field!r} needs a numeric value, got {value!r}"
            ) from None
        value = int(num) if num.is_integer() else num
    return {"field": field, "op": op, "value": value}


def parse_scope(raw: str | list | None) -> list[dict]:
    """Validate a structured scope filter, returning normalized conditions.

    `raw` is a JSON array string (as the assistant tool passes it) or an already
    decoded list of ``{"field", "op", "value"}`` dicts. Each condition is checked
    against the field/operator allowlists and numeric values are coerced, so a
    hostile field or operator (or Cypher smuggled into either) is rejected with a
    ``ValueError`` before it can reach the graph. Null-check operators drop the
    value. Returns ``[]`` for empty input.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"scope must be a JSON array of conditions: {exc}") from exc
    else:
        data = raw
    if not isinstance(data, list):
        raise ValueError("scope must be a JSON array of {field, op, value} conditions")
    return [_normalize_condition(cond) for cond in data]


def scope_to_cypher(conditions: list[dict], prefix: str = "scope") -> tuple[str, dict]:
    """Translate normalized scope conditions into a parameterized Cypher fragment.

    Returns ``(clause, params)`` where `clause` is a ``AND``-joined boolean over
    ``c`` (no leading ``AND``; the caller composes it) and every value AND field
    name is a bound parameter — the field goes through dynamic property access
    ``c[$key]`` so even an allowlisted-but-untrusted string can't reshape the
    query. Pass already-validated conditions (see :func:`parse_scope`).
    """
    clauses: list[str] = []
    params: dict = {}
    for i, cond in enumerate(conditions):
        fkey = f"{prefix}_f{i}"
        params[fkey] = cond["field"]
        prop = f"c[${fkey}]"
        op = cond["op"]
        token = _SCOPE_OPS[op]
        if op in _NULL_OPS:
            clauses.append(f"{prop} {token}")
            continue
        vkey = f"{prefix}_v{i}"
        params[vkey] = cond["value"]
        if op == "contains":
            clauses.append(f"toLower(toString({prop})) CONTAINS toLower(${vkey})")
        else:
            clauses.append(f"{prop} {token} ${vkey}")
    return " AND ".join(clauses), params


async def _applicable_companies(
    driver,
    applies_to_kind: str,
    country: str | None,
    missing_key: str | None = None,
    company: str | None = None,
    scope: list[dict] | None = None,
) -> list[dict]:
    kind_clause = "" if applies_to_kind == "all" else "AND c.kind = $kind"
    country_clause = "AND c.hqCountry = $country" if country else ""
    # Scope to a single named company when the user asked about just that one.
    company_clause = "AND c.name = $company" if company else ""
    # Only companies that don't already have the field set (its property is the key).
    missing_clause = "AND c[$missingKey] IS NULL" if missing_key else ""
    # Arbitrary structured condition (validated field/operator, parameterized) —
    # composes on top of the fixed scoping above.
    scope_frag, scope_params = scope_to_cypher(scope or [])
    scope_clause = f"AND ({scope_frag})" if scope_frag else ""
    async with driver.session() as session:
        result = await session.run(
            f"MATCH (c:Company)-[:TAGGED_AS]->(:Topic) "
            f"WHERE c.website IS NOT NULL {kind_clause} {country_clause} {company_clause} "
            f"{missing_clause} {scope_clause} "
            f"RETURN DISTINCT c.name AS name, c.website AS website ORDER BY name",
            kind=applies_to_kind,
            country=country,
            company=company,
            missingKey=missing_key,
            **scope_params,
        )
        return [dict(record) async for record in result]


async def enqueue_backfill(
    field_name: str,
    country: str = "",
    missing_only: bool = False,
    company: str = "",
    conditions: str = "",
) -> dict:
    """Research a custom field for companies of its kind and prepare a batch for the
    user to review and commit. Returns immediately; runs in the background. Use when
    the user asks to fill in / research an existing field. The field_name is the
    field's key (e.g. 'serviceLines'). Scope it to match what the user asked for:
    pass company (the company's exact name) to fill the field for JUST that one
    company when the user named a specific company; pass country (full name, e.g.
    'United Kingdom') to limit to companies HQ'd there; set missing_only=True when
    the user wants only the companies that DON'T already have a value (e.g. 'fill it
    in where it's missing').

    For an arbitrary condition — 'companies with more than 200 employees',
    'founded before 2010', 'HQ'd in Germany' — pass conditions as a JSON array of
    {"field","op","value"} objects; every listed condition must hold (AND). Do NOT
    write Cypher: the ONLY valid fields are headcount, estimatedRevenue,
    yearFounded, hqCountry, hqCity, hqState, kind, priority, funding, origin, and
    the ONLY valid ops are =, !=, <, <=, >, >=, contains, is_null, is_not_null
    (is_null / is_not_null take no value). Example: '>200 employees' →
    conditions='[{"field":"headcount","op":">","value":200}]'. Anything outside
    those allowlists is rejected. conditions composes with country / missing_only.
    With no scope it researches every applicable company."""
    driver = get_driver()
    # Validate the structured scope up front (pure, no DB) so a hostile field or
    # operator is rejected before anything is enqueued.
    try:
        scope = parse_scope(conditions)
    except ValueError as exc:
        return {"error": f"invalid scope: {exc}"}
    field_defs = {f["name"]: f for f in await queries.list_field_defs(driver)}
    field_def = field_defs.get(field_name)
    if field_def is None:
        return {"error": f"no field named {field_name!r}; add it first with add_field"}

    missing_key = field_name if missing_only else None
    companies = await _applicable_companies(
        driver,
        field_def["appliesToKind"],
        country or None,
        missing_key,
        company or None,
        scope,
    )
    # Scoping to one named company makes a zero match common (typo, wrong kind, no
    # website/topic). Surface that instead of enqueuing an empty job the user is
    # told to wait on.
    if not companies:
        scope = f" matching {company!r}" if company else ""
        return {"companies": 0, "note": f"no applicable company{scope}"}
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
            # The structured condition that produced this batch, kept verbatim so
            # the reviewer sees exactly which scope selected these companies.
            "scope": scope,
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
                "scope": scope,
                "status": "pending",
            }
        )
    return {"job_id": job_id, "field": field_def["label"], "companies": len(companies)}


async def execute_backfill_job(job_id: str) -> None:
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
        job.get("scope") or None,
    )
    # Per-run budget: defaults for "backfill" from settings, overridable by the
    # job's payload ("budget" dict). None = unlimited (backwards compatible). The
    # tool helpers (fetch_page / web_search / generate_with_retry) charge it as
    # they spend; the companies cap is charged here, per iteration.
    budget = budget_for("backfill", job.get("budget"))
    rows: list[dict] = []
    exhausted: BudgetExhausted | None = None
    with use_budget(budget):
        for company in companies:
            try:
                # Charge the company BEFORE its work: on the cap, stop cleanly
                # with the rows done so far kept (never lose completed work).
                charge_company()
                result = await extract_field(
                    company["website"],
                    field_def["label"],
                    field_def["description"],
                    field_def["type"],
                )
            except BudgetExhausted as exc:
                # A page/LLM cap tripped mid-company (or the companies cap above):
                # the partial company is dropped, prior rows stay reviewable.
                exhausted = exc
                break
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

    total = job.get("total", len(companies))
    # Human-readable completion line for the activity page (#49): "researched X of
    # Y companies". X counts rows attempted (a per-company research error still
    # produces a row); the review step is where the user judges each value.
    final = {
        **job,
        "rows": rows,
        "done": len(rows),
        "outcome": f"researched {len(rows)} of {total} companies",
    }
    if exhausted is not None:
        # Record which cap stopped the run and how far it got. Status stays
        # "ready" — the completed rows are still reviewable/committable; a budget
        # cap is a graceful stop, not an error.
        final["budget_exhausted"] = {
            "limit": exhausted.limit,
            "cap": exhausted.cap,
            "reached": exhausted.count,
            "done": len(rows),
            "total": job.get("total", len(companies)),
        }
        if budget is not None:
            final["budget_usage"] = budget.usage()
    await jobs.update_job(job_id, final, status="ready")


async def get_backfill(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_backfill(job_id: str, companies: list[str] | None = None) -> dict:
    """Write selected back-fill rows (all if companies is None) with provenance.

    Ready-guarded like the sibling flows: a still-running, errored, or already
    committed job is rejected, so rows land exactly once and never mid-scan.
    """
    job = await jobs.get_ready_job(job_id)
    if job is None:
        return {"error": "back-fill job not found or not ready"}
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
    await jobs.mark_committed(job_id, {**job, "rows": rows})
    return {"committed": written}
