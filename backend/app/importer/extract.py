"""LLM extraction of the messy freeform sheet columns.

The sheet's Notes, Leadership, Partnerships, and Clients columns are freeform
prose. One structured Gemini call per row turns them into clean fields — same
cost as parsing Notes alone, far more robust than regex. Deterministic columns
(name, website, headcount, …) are mapped without the LLM; see `csv_import.py`.

This extractor is intentionally standalone (a plain google-genai structured call,
no tools), so the later ADK enrichment agent can reuse the same schema.
"""

import asyncio

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

from app.config import settings
from app.graph.company_types import canonical_company_types
from app.graph.models import Leader

# Transient statuses worth retrying: rate limit + server-side unavailability.
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 8

_PROMPT = """You are cleaning up messy spreadsheet cells for a company research \
database. Given the raw freeform text below for a single company, extract the \
structured fields. Rules:
- Only use information present in the text. Never invent or infer beyond it.
- Use null / empty lists when something is not present.
- year_founded: the 4-digit founding year as an integer, if stated.
- funding: a short normalized summary of funding/investment if mentioned \
(e.g. "Series B, $40M"), else null.
- company_types: ONLY notable ownership/structure certifications, from this \
exact controlled list: "B-Corp", "ESOP", "employee-owned", "co-operative", \
"non-profit", "PBC", "foundation-owned". Do NOT include generic incorporation or \
legal forms (Ltd, LLC, Inc, GmbH, Pty Ltd), nor ownership status like "privately \
held" or "public". If none of the controlled types clearly apply, return [].
- notes: the residual notes text with the year/funding/company-type facts \
removed, or null if nothing meaningful remains.
- leadership: people with their title/role.
- partnerships / clients: clean lists of organization names.

Company: {company}

Notes: {notes}
Leadership: {leadership}
Partnerships: {partnerships}
Clients: {clients}
"""


class ExtractedFields(BaseModel):
    year_founded: int | None = None
    funding: str | None = None
    company_types: list[str] = Field(default_factory=list)
    notes: str | None = None
    leadership: list[Leader] = Field(default_factory=list)
    partnerships: list[str] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)


def new_client() -> genai.Client:
    """A genai client. Reads GEMINI_API_KEY / GOOGLE_API_KEY from the env.

    Create once and reuse across rows. Callers use the async API (`client.aio`)
    via `extract_fields`, because the sync client misbehaves inside an event loop.
    """
    return genai.Client()


async def extract_fields(
    *,
    company: str,
    notes: str = "",
    leadership: str = "",
    partnerships: str = "",
    clients: str = "",
    client: genai.Client | None = None,
) -> ExtractedFields:
    """Parse one company's freeform cells into structured fields."""
    if not any([notes, leadership, partnerships, clients]):
        return ExtractedFields()

    client = client or new_client()
    contents = _PROMPT.format(
        company=company,
        notes=notes or "(none)",
        leadership=leadership or "(none)",
        partnerships=partnerships or "(none)",
        clients=clients or "(none)",
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ExtractedFields,
        temperature=0,
    )

    # Retry transient 429/5xx with exponential backoff; Gemini's shared models
    # throw 503 "high demand" often enough to sink a batch otherwise.
    delay = 1.0
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.aio.models.generate_content(
                model=settings.gemini_model, contents=contents, config=config
            )
            parsed = resp.parsed
            if isinstance(parsed, ExtractedFields):
                parsed.company_types = canonical_company_types(parsed.company_types)
                return parsed
            return ExtractedFields()
        except errors.APIError as exc:
            if exc.code in _RETRYABLE and attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise

    return ExtractedFields()  # unreachable; keeps type checkers happy
