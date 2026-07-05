"""LLM extraction of the messy freeform sheet columns.

The sheet's Notes, Leadership, Partnerships, and Clients columns are freeform
prose. One structured Gemini call per row turns them into clean fields — same
cost as parsing Notes alone, far more robust than regex. Deterministic columns
(name, website, headcount, …) are mapped without the LLM; see `csv_import.py`.

This extractor is intentionally standalone (a plain google-genai structured call,
no tools), so the later ADK enrichment agent can reuse the same schema.
"""

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.config import settings
from app.graph.models import Leader

_PROMPT = """You are cleaning up messy spreadsheet cells for a company research \
database. Given the raw freeform text below for a single company, extract the \
structured fields. Rules:
- Only use information present in the text. Never invent or infer beyond it.
- Use null / empty lists when something is not present.
- year_founded: the 4-digit founding year as an integer, if stated.
- funding: a short normalized summary of funding/investment if mentioned \
(e.g. "Series B, $40M"), else null.
- company_types: legal/structural classifications like "B-Corp", "ESOP", \
"non-profit" — NOT industry descriptions.
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

    resp = await (client or new_client()).aio.models.generate_content(
        model=settings.gemini_model,
        contents=_PROMPT.format(
            company=company,
            notes=notes or "(none)",
            leadership=leadership or "(none)",
            partnerships=partnerships or "(none)",
            clients=clients or "(none)",
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExtractedFields,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    return parsed if isinstance(parsed, ExtractedFields) else ExtractedFields()
